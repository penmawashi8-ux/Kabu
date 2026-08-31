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

from .errors import KabuError

log = logging.getLogger(__name__)

_TIMEOUT = 20
_USER_AGENT = "kabu/0.1 (personal trading simulator)"
_CACHE_MAX_AGE_SEC = 6 * 3600


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

def parse_stooq_csv(text: str) -> list[Bar]:
    """stooq の日足 CSV を読む。

    Date,Open,High,Low,Close,Volume
    2026-08-28,2600,2650,2590,2640,12345600
    """
    rows = list(csv.DictReader(io.StringIO(text.strip())))
    if not rows:
        raise MarketDataError("株価データが空でした")

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
            "株価データを 1 日ぶんも読めませんでした。銘柄コードが正しいか確認してください"
        )
    return sorted(bars, key=lambda b: b.day)


def parse_yahoo_json(payload: str) -> list[Bar]:
    """Yahoo Finance のチャート API の応答を読む。"""
    try:
        data = json.loads(payload)
        result = data["chart"]["result"][0]
        stamps = result["timestamp"]
        quote = result["indicators"]["quote"][0]
    except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
        raise MarketDataError(f"株価データの形式が想定と違います: {exc}") from exc

    bars: list[Bar] = []
    for i, stamp in enumerate(stamps):
        values = [quote[k][i] for k in ("open", "high", "low", "close")]
        if any(v is None for v in values):
            continue  # 立会がなかった日
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
        raise MarketDataError("株価データを 1 日ぶんも読めませんでした")
    return sorted(bars, key=lambda b: b.day)


# --------------------------------------------------------------------- 取得

def _stooq_url(symbol: str) -> str:
    # "7203.T" → "7203.jp"
    code = symbol.split(".")[0].lower()
    return f"https://stooq.com/q/d/l/?s={code}.jp&i=d"


def _yahoo_url(symbol: str) -> str:
    code = symbol if "." in symbol else f"{symbol}.T"
    return (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{code}"
        "?range=2y&interval=1d"
    )


PROVIDERS = {
    "stooq": (_stooq_url, parse_stooq_csv),
    "yahoo": (_yahoo_url, parse_yahoo_json),
}


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
) -> list[Bar]:
    """1 銘柄の日足を取ってくる。取得できたらディスクに残して使い回す。"""
    if provider not in PROVIDERS:
        raise MarketDataError(
            f"知らない取得元です: {provider}（使えるのは {', '.join(PROVIDERS)}）"
        )
    url_of, parse = PROVIDERS[provider]

    cache = Path(cache_dir) / f"{symbol.replace('.', '_')}-{provider}.txt"
    if cache.exists() and time.time() - cache.stat().st_mtime < max_age_sec:
        try:
            return parse(cache.read_text(encoding="utf-8"))
        except MarketDataError:
            pass  # 壊れたキャッシュは取り直す

    raw = _download(url_of(symbol))
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
) -> dict[str, list[Bar]]:
    """複数銘柄ぶん。1 銘柄でも取れれば返す（取れなかった銘柄は含めない）。"""
    out: dict[str, list[Bar]] = {}
    errors: list[str] = []
    for symbol in symbols:
        try:
            out[symbol] = fetch_bars(symbol, provider=provider, cache_dir=cache_dir)
            log.info("%s: %d 日ぶんの実データを読み込みました", symbol, len(out[symbol]))
        except MarketDataError as exc:
            errors.append(f"{symbol}: {exc}")
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
