"""実際の株価データの取得と再生。

シミュレーターで「本物の値段」を使うための仕組み。無料で手に入る日本株の
株価は基本的に **日足（引け値ベース）** で、リアルタイムではない。
リアルタイムが要るなら楽天証券のマーケットスピードII RSS を使うこと
（`kabu play --source rss`）。

取得元は 2 つ用意してある。片方が落ちても、もう片方で動く。
    stooq  : CSV を返すだけの単純な提供元。既定。
    yahoo  : Yahoo Finance のチャート API。

ネットワークに出られない環境では取得に失敗するので、その場合は
擬似株価（GameFeed）に自動で切り替わる。
"""

from __future__ import annotations

import csv
import io
import json
import logging
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Callable

from .errors import KabuError

log = logging.getLogger(__name__)

_TIMEOUT = 20
_USER_AGENT = "kabu/0.1 (personal trading simulator)"
_CACHE_MAX_AGE_SEC = 6 * 3600

# 手法の検証には、上げ相場だけでなく下げ相場も要る。2 年では足りない。
DEFAULT_YEARS = 10


class MarketDataError(KabuError):
    """株価データを取得・解釈できなかった。"""


@dataclass(frozen=True)
class Bar:
    """1 日ぶんの四本値。"""

    day: date
    open: float
    high: float
    low: float
    close: float

    def walk(self) -> list[float]:
        """その日の値動きを 4 点で表す。

        日足しか手に入らないので、寄り → 高値 → 安値 → 引け の順になぞる。
        「その日ほんとうに付いた 4 つの値段」なので、終値だけを繋ぐより
        高値・安値のトリガーに当たるかどうかを正しく再現できる。
        安値を先に付けた日と順序が入れ替わることはある。
        """
        return [self.open, self.high, self.low, self.close]


# --------------------------------------------------------------------- 解析

def _snippet(text: str, limit: int = 200) -> str:
    """応答の冒頭を 1 行にして返す（エラーに何が返ってきたかを添えるため）。

    CSV のつもりが「レート制限に達しました」やログイン用の HTML だった、
    という失敗を、利用者がその場で判断できるようにする。
    """
    flat = " ".join(text.split())
    if len(flat) > limit:
        flat = flat[:limit] + "…"
    return flat or "（空）"


def parse_stooq_csv(text: str) -> list[Bar]:
    """stooq の日足 CSV を読む。

    Date,Open,High,Low,Close,Volume
    2026-08-28,2600,2650,2590,2640,12345600
    """
    rows = list(csv.DictReader(io.StringIO(text.strip())))
    if not rows:
        raise MarketDataError(f"株価データが空でした（応答: {_snippet(text)}）")

    bars: list[Bar] = []
    for row in rows:
        try:
            bars.append(
                Bar(
                    day=datetime.strptime(row["Date"].strip(), "%Y-%m-%d").date(),
                    open=float(row["Open"]),
                    high=float(row["High"]),
                    low=float(row["Low"]),
                    close=float(row["Close"]),
                )
            )
        except (KeyError, TypeError, ValueError):
            # 銘柄が無いと "N/D" などが混ざる。読めた行だけ使う。
            continue
    if not bars:
        raise MarketDataError(
            "株価データを 1 日ぶんも読めませんでした。"
            "銘柄コードが正しいか、取得元が制限をかけていないか確認してください\n"
            f"    取得元の応答: {_snippet(text)}"
        )
    return sorted(bars, key=lambda b: b.day)


def parse_yahoo_quote(payload: str) -> tuple[float, datetime | None]:
    """Yahoo Finance の応答から「いまの値段」を取り出す。

    無料で取れる日本株の値段は取引所の生値ではなく、20 分ほど遅れた配信。
    リアルタイムが必要なら証券会社の配信（RSS）を使うこと。
    """
    try:
        meta = json.loads(payload)["chart"]["result"][0]["meta"]
    except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
        raise MarketDataError(
            f"株価データの形式が想定と違います: {exc}\n"
            f"    取得元の応答: {_snippet(payload)}"
        ) from exc

    price = meta.get("regularMarketPrice")
    if not isinstance(price, (int, float)) or isinstance(price, bool) or price <= 0:
        raise MarketDataError("現在値が取得できませんでした")

    stamp = meta.get("regularMarketTime")
    asof = datetime.fromtimestamp(stamp) if isinstance(stamp, (int, float)) else None
    return float(price), asof


def parse_stooq_quote(text: str) -> tuple[float, datetime | None]:
    """stooq の 1 行 CSV から「いまの値段」を取り出す。

    Symbol,Date,Time,Open,High,Low,Close,Volume
    7203.JP,2026-08-31,15:00:00,2600,2650,2590,2640,12345600
    """
    rows = list(csv.DictReader(io.StringIO(text.strip())))
    if not rows:
        raise MarketDataError(f"株価データが空でした（応答: {_snippet(text)}）")
    row = rows[0]
    try:
        price = float(row["Close"])
    except (KeyError, TypeError, ValueError) as exc:
        raise MarketDataError(f"現在値が取得できませんでした: {row}") from exc
    if price <= 0:
        raise MarketDataError("現在値が 0 以下でした")

    asof = None
    try:
        asof = datetime.strptime(f"{row['Date']} {row['Time']}", "%Y-%m-%d %H:%M:%S")
    except (KeyError, TypeError, ValueError):
        pass
    return price, asof


def parse_yahoo_json(payload: str) -> list[Bar]:
    """Yahoo Finance のチャート API の応答を読む。

    **株式分割・配当で調整した値段を使う。** 生の値段（indicators.quote）を
    そのまま読むと、1:3 の分割が「1 日で -67% の暴落」として記録される。
    日本株は分割が多いので、検証結果が丸ごと狂う。

    Yahoo は調整後の終値（adjclose）を別に返してくるので、
    「調整後終値 ÷ 生の終値」の比率を四本値すべてに掛けて揃える。
    """
    try:
        data = json.loads(payload)
        result = data["chart"]["result"][0]
        stamps = result["timestamp"]
        quote = result["indicators"]["quote"][0]
    except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
        raise MarketDataError(
            f"株価データの形式が想定と違います: {exc}\n"
            f"    取得元の応答: {_snippet(payload)}"
        ) from exc

    try:
        adjusted = result["indicators"]["adjclose"][0]["adjclose"]
    except (KeyError, IndexError, TypeError):
        adjusted = None
        log.warning("調整後の株価が取得できませんでした。"
                    "株式分割が暴落として記録される可能性があります")

    bars: list[Bar] = []
    for i, stamp in enumerate(stamps):
        values = [quote[k][i] for k in ("open", "high", "low", "close")]
        if any(v is None for v in values):
            continue  # 立会がなかった日
        if adjusted is not None:
            adj = adjusted[i] if i < len(adjusted) else None
            if adj is not None and values[3]:
                ratio = adj / values[3]
                values = [v * ratio for v in values]
        bars.append(
            Bar(
                day=datetime.fromtimestamp(stamp).date(),
                open=float(values[0]),
                high=float(values[1]),
                low=float(values[2]),
                close=float(values[3]),
            )
        )
    if not bars:
        raise MarketDataError(
            "株価データを 1 日ぶんも読めませんでした\n"
            f"    取得元の応答: {_snippet(payload)}"
        )
    return sorted(bars, key=lambda b: b.day)


# --------------------------------------------------------------------- 取得

def _stooq_url(symbol: str, years: int = DEFAULT_YEARS) -> str:
    # "7203.T" → "7203.jp"。stooq は全期間を返すので years は使わない。
    code = symbol.split(".")[0].lower()
    return f"https://stooq.com/q/d/l/?s={code}.jp&i=d"


def _yahoo_url(symbol: str, years: int = DEFAULT_YEARS) -> str:
    code = symbol if "." in symbol else f"{symbol}.T"
    return (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{code}"
        f"?range={max(1, years)}y&interval=1d"
    )


def _stooq_quote_url(symbol: str) -> str:
    code = symbol.split(".")[0].lower()
    return f"https://stooq.com/q/l/?s={code}.jp&f=sd2t2ohlcv&h&e=csv"


def _yahoo_quote_url(symbol: str) -> str:
    code = symbol if "." in symbol else f"{symbol}.T"
    return f"https://query1.finance.yahoo.com/v8/finance/chart/{code}?range=1d&interval=1m"


PROVIDERS = {
    "stooq": (_stooq_url, parse_stooq_csv),
    "yahoo": (_yahoo_url, parse_yahoo_json),
}

QUOTE_PROVIDERS = {
    "stooq": (_stooq_quote_url, parse_stooq_quote),
    "yahoo": (_yahoo_quote_url, parse_yahoo_quote),
}


def fetch_quote(symbol: str, *, provider: str = "yahoo") -> tuple[float, datetime | None]:
    """1 銘柄の「いまの値段」を 1 回取る（遅延あり）。"""
    if provider not in QUOTE_PROVIDERS:
        raise MarketDataError(
            f"知らない取得元です: {provider}（使えるのは {', '.join(QUOTE_PROVIDERS)}）"
        )
    url_of, parse = QUOTE_PROVIDERS[provider]
    return parse(_download(url_of(symbol)))


def _download(url: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT) as response:
            return response.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as exc:
        raise MarketDataError(f"株価データを取得できませんでした: {exc}") from exc


def fetch_bars(
    symbol: str,
    *,
    provider: str = "stooq",
    cache_dir: str | Path = ".kabu_cache",
    max_age_sec: float = _CACHE_MAX_AGE_SEC,
    years: int = DEFAULT_YEARS,
) -> list[Bar]:
    """1 銘柄の日足を取ってくる。取得できたらディスクに残して使い回す。"""
    if provider not in PROVIDERS:
        raise MarketDataError(
            f"知らない取得元です: {provider}（使えるのは {', '.join(PROVIDERS)}）"
        )
    url_of, parse = PROVIDERS[provider]

    # 期間が違えば中身も違うので、キャッシュも分ける。
    cache = Path(cache_dir) / f"{symbol.replace('.', '_')}-{provider}-{years}y.txt"
    if cache.exists() and time.time() - cache.stat().st_mtime < max_age_sec:
        try:
            return parse(cache.read_text(encoding="utf-8"))
        except MarketDataError:
            pass  # 壊れたキャッシュは取り直す

    raw = _download(url_of(symbol, years))
    bars = parse(raw)   # 解釈できたものだけ保存する
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(raw, encoding="utf-8")
    except OSError as exc:
        log.warning("株価データのキャッシュを保存できませんでした: %s", exc)
    return bars


def fetch_many(
    symbols: list[str],
    *,
    provider: str = "stooq",
    cache_dir: str | Path = ".kabu_cache",
    years: int = DEFAULT_YEARS,
    pause: float = 0.0,
    on_progress: Callable[[int, int, str], None] | None = None,
) -> dict[str, list[Bar]]:
    """複数銘柄ぶん。1 銘柄でも取れれば返す（取れなかった銘柄は含めない）。

    pause: 1 銘柄ごとに空ける秒数。100 銘柄を一気に取ると取得元に負荷をかけ、
           制限をかけられることがあるので、まとめて取るときは間を置く。
    """
    out: dict[str, list[Bar]] = {}
    errors: list[str] = []
    for i, symbol in enumerate(symbols, start=1):
        if on_progress:
            on_progress(i, len(symbols), symbol)
        try:
            out[symbol] = fetch_bars(symbol, provider=provider,
                                     cache_dir=cache_dir, years=years)
            log.info("%s: %d 日ぶんの実データを読み込みました", symbol, len(out[symbol]))
        except MarketDataError as exc:
            errors.append(f"{symbol}: {exc}")
        if pause and i < len(symbols):
            time.sleep(pause)
    if not out:
        raise MarketDataError("どの銘柄も取得できませんでした。\n  " + "\n  ".join(errors))
    for message in errors:
        log.warning("実データを取得できませんでした — %s", message)
    return out


# --------------------------------------------------------------------- 再生

class ReplayFeed:
    """取得した実データを、時間を早送りしながら流す。

    GameFeed と同じ形（read_quotes / set_auto / reset）で使えるようにしてあるので、
    Runner から見れば擬似株価と区別がつかない。
    """

    def __init__(self, bars: dict[str, list[Bar]], *, speed: int = 4) -> None:
        self._lock = threading.Lock()
        self._bars = bars
        # 銘柄ごとに「その日の 4 点」を平らに並べておく
        self._points: dict[str, list[tuple[date, float]]] = {
            symbol: [(bar.day, price) for bar in series for price in bar.walk()]
            for symbol, series in bars.items()
        }
        self._length = max((len(p) for p in self._points.values()), default=0)
        self._cursor = 0
        self._speed = max(1, int(speed))
        self._playing = False

    # ---- GameFeed と同じ操作面 ----------------------------------------

    @property
    def auto(self) -> bool:
        with self._lock:
            return self._playing

    def set_auto(self, flag: bool) -> None:
        with self._lock:
            self._playing = bool(flag)

    def set_price(self, symbol: str, price: float) -> None:
        """実データ再生中は手で値段を動かせない（動かしたら実データではなくなる）。"""

    def reset(self) -> None:
        with self._lock:
            self._cursor = 0

    def read_quotes(self, symbols: list[str]) -> dict[str, float]:
        with self._lock:
            if self._playing and self._length:
                self._cursor = (self._cursor + self._speed) % self._length
            cursor = self._cursor
            out: dict[str, float] = {}
            for symbol in symbols:
                points = self._points.get(symbol)
                if points:
                    out[symbol] = round(points[min(cursor, len(points) - 1)][1], 1)
            return out

    # ---- 表示用 --------------------------------------------------------

    @property
    def manual_allowed(self) -> bool:
        return False

    def describe(self) -> dict[str, object]:
        with self._lock:
            cursor = self._cursor
            day: date | None = None
            for points in self._points.values():
                if points:
                    day = points[min(cursor, len(points) - 1)][0]
                    break
            span = ""
            for series in self._bars.values():
                if series:
                    span = f"{series[0].day:%Y-%m-%d} 〜 {series[-1].day:%Y-%m-%d}"
                    break
            return {
                "kind": "replay",
                "label": "実データ再生",
                "asof": day.isoformat() if day else None,
                "span": span,
                "progress": (cursor / self._length) if self._length else 0.0,
                "speed": self._speed,
            }


class LiveFeed:
    """遅延ありの「いまの値段」を定期的に取り直して流す。

    無料の配信は取引所の生値ではなく 20 分ほど遅れたもの。値段そのものは本物だが、
    リアルタイムではない。それを画面でも隠さない（describe() が delayed を返す）。

    ほんとうのリアルタイムが要るなら証券会社の配信を使うこと:
        kabu play --source rss   （マーケットスピードII）
    """

    def __init__(
        self,
        symbols: list[str],
        *,
        provider: str = "yahoo",
        interval: float = 30.0,
        auto: bool = False,
    ) -> None:
        self._lock = threading.Lock()
        self._symbols = list(symbols)
        self._provider = provider
        # 取得元に迷惑をかけないよう、あまり短い間隔は許さない。
        self._interval = max(10.0, float(interval))
        self._playing = bool(auto)
        self._prices: dict[str, float] = {}
        self._asof: datetime | None = None
        self._last_error: str = ""
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ---- 起動と停止 ----------------------------------------------------

    def prime(self) -> None:
        """最初の 1 回を同期で取る。ここで失敗したら呼び出し側が諦められる。"""
        self._poll_once()
        if not self._prices:
            raise MarketDataError(self._last_error or "現在値を取得できませんでした")

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="kabu-live", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.wait(self._interval):
            if self.auto:
                self._poll_once()

    def _poll_once(self) -> None:
        prices: dict[str, float] = {}
        asof: datetime | None = None
        errors: list[str] = []
        for symbol in self._symbols:
            try:
                price, stamp = fetch_quote(symbol, provider=self._provider)
                prices[symbol] = price
                asof = stamp or asof
            except MarketDataError as exc:
                errors.append(f"{symbol}: {exc}")

        with self._lock:
            # 取れた銘柄だけ差し替える。取れなかった銘柄は直前の値段のまま。
            self._prices.update(prices)
            if asof:
                self._asof = asof
            self._last_error = "; ".join(errors)
        for message in errors:
            log.warning("現在値を取得できませんでした — %s", message)

    # ---- GameFeed と同じ操作面 ------------------------------------------

    @property
    def auto(self) -> bool:
        with self._lock:
            return self._playing

    def set_auto(self, flag: bool) -> None:
        should_poll = bool(flag)
        with self._lock:
            was = self._playing
            self._playing = should_poll
        if should_poll and not was:
            self._poll_once()   # 押した直後に反映されてほしい

    def set_price(self, symbol: str, price: float) -> None:
        """実際の値段を流している最中は手で動かせない。"""

    def reset(self) -> None:
        self._poll_once()

    def read_quotes(self, symbols: list[str]) -> dict[str, float]:
        with self._lock:
            return {s: self._prices[s] for s in symbols if s in self._prices}

    # ---- 表示用 ---------------------------------------------------------

    @property
    def manual_allowed(self) -> bool:
        return False

    def describe(self) -> dict[str, object]:
        with self._lock:
            return {
                "kind": "delayed",
                "label": "実際の株価（遅延）",
                "asof": self._asof.strftime("%H:%M") if self._asof else None,
                "span": f"{self._provider} · {self._interval:.0f} 秒ごとに取得"
                        + (f" · {self._last_error}" if self._last_error else ""),
                "progress": 0.0,
                "speed": 1,
            }
