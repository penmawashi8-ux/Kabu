"""`kabu play` — ブラウザで動きが見えるシミュレーター。

本物の Runner をバックグラウンドで回し、その状態を JSON で配る。
つまり画面に出ているのは飾りではなく、実弾モードで動くのと同じ売買ロジックの結果。
発注先だけが PaperBroker に差し替わっている。
"""

from __future__ import annotations

import itertools
import json
import logging
import random
import threading
import time
from collections import deque
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .broker import PaperBroker
from .clock import MarketClock
from .config import Config, Rule, save_rules
from .errors import ConfigError
from .journal import Journal
from .runner import Runner

log = logging.getLogger(__name__)

_HISTORY_POINTS = 240
_MAX_EVENTS = 60
_MAX_RULES = 12

# dashboard.html は Artifact としても公開できるよう、doctype や <head> を持たない
# 断片として書いてある。ブラウザに直接食わせるときはここで包む。
# charset は必ず先頭に置くこと（日本語が化ける）。
_PAGE_SKELETON = """<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  html {{ color-scheme: light dark; }}
  body {{ margin: 0; }}
  img {{ max-width: 100%; }}
  [hidden] {{ display: none !important; }}
</style>
{body}
</head>
<body></body>
</html>
"""


def dashboard_source() -> str:
    """画面本体（Artifact 用の断片）。"""
    return (Path(__file__).parent / "dashboard.html").read_text(encoding="utf-8")


def render_standalone_page(body: str | None = None) -> str:
    """断片を、単体で開ける 1 枚の HTML に包む。"""
    return _PAGE_SKELETON.format(body=body if body is not None else dashboard_source())


class GameFeed:
    """手で動かせる株価。自動モードでは中心へ引き戻しつつランダムに揺れる。"""

    # auto は既定でオフ。開いた瞬間に勝手に売買が始まると、利用者が自分の
    # ルールを確かめる前に画面が動いてしまう。動かすかどうかは本人に選ばせる。
    def __init__(self, config: Config, *, auto: bool = False) -> None:
        self._lock = threading.Lock()
        self._auto = auto
        self._rng = random.Random()
        self._centre: dict[str, float] = {}
        self._span: dict[str, float] = {}
        self._price: dict[str, float] = {}
        for rule in config.rules:
            centre = (rule.buy_at + rule.sell_at) / 2
            self._centre[rule.symbol] = centre
            self._span[rule.symbol] = max(rule.sell_at - rule.buy_at, 1.0)
            self._price[rule.symbol] = centre

    @property
    def auto(self) -> bool:
        with self._lock:
            return self._auto

    def set_auto(self, flag: bool) -> None:
        with self._lock:
            self._auto = flag

    def set_price(self, symbol: str, price: float) -> None:
        with self._lock:
            if symbol in self._price and price > 0:
                self._price[symbol] = float(price)

    def reset(self) -> None:
        with self._lock:
            for symbol, centre in self._centre.items():
                self._price[symbol] = centre

    @property
    def manual_allowed(self) -> bool:
        return True

    def describe(self) -> dict[str, Any]:
        return {"kind": "sim", "label": "擬似株価", "asof": None, "span": "", "progress": 0.0}

    def read_quotes(self, symbols: list[str]) -> dict[str, float]:
        with self._lock:
            if self._auto:
                for symbol in symbols:
                    if symbol not in self._price:
                        continue
                    # 平均回帰つきランダムウォーク。振れ幅は「買い〜売りの値幅を
                    # 数十秒に一度は跨ぐ」ように決めてある（跨がないと何も起きない）。
                    span = self._span[symbol]
                    drift = (self._centre[symbol] - self._price[symbol]) * 0.05
                    shock = self._rng.uniform(-0.5, 0.5) * span * 0.45
                    self._price[symbol] = max(1.0, self._price[symbol] + drift + shock)
            return {s: round(self._price[s], 1) for s in symbols if s in self._price}


class UiJournal(Journal):
    """CSV への記録に加えて、画面表示用のイベントを保持する。"""

    def __init__(self, path, mode, now_provider, sink: deque, counter) -> None:
        super().__init__(path, mode, now_provider)
        self._sink = sink
        self._counter = counter

    def record(self, **kwargs: Any) -> None:
        super().record(**kwargs)
        at = kwargs.get("at") or self._now()
        self._sink.appendleft(
            {
                "seq": next(self._counter),
                "t": int(at.timestamp() * 1000),
                "event": kwargs.get("event", ""),
                "rule_id": kwargs.get("rule_id", ""),
                "symbol": kwargs.get("symbol", ""),
                "side": kwargs.get("side", ""),
                "quantity": kwargs.get("quantity", ""),
                "price": kwargs.get("price", ""),
                "note": kwargs.get("note", ""),
            }
        )


class GameSession:
    """Runner をバックグラウンドで回し、UI 用のスナップショットを作る。"""

    def __init__(self, config: Config, feed_factory=None, config_path: Path | None = None) -> None:
        self.config = config
        # 画面の「保存」がどのファイルに書き戻すか。None なら保存ボタンは出さない
        # （`kabu play` は必ず -c で読んだファイルのパスを渡してくる）。
        self.config_path = config_path
        # 株価をどこから取るか。既定は擬似株価、実データ再生や RSS も差せる。
        self._feed_factory = feed_factory or (lambda cfg: GameFeed(cfg))
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._events: deque[dict[str, Any]] = deque(maxlen=_MAX_EVENTS)
        self._history: dict[str, deque[tuple[int, float]]] = {}
        self._orders = 0
        self._fills = 0
        self._realized = 0.0
        self._entry: dict[str, float] = {}
        self._seq = itertools.count(1)
        self._last_seq = 0
        self._clock = MarketClock(config.market)
        self._rebuild()

    def _rebuild(self) -> None:
        """現在の config.rules に合わせて、株価・履歴・Runner を作り直す。"""
        self._history = {
            rule.symbol: deque(maxlen=_HISTORY_POINTS) for rule in self.config.rules
        }
        self.feed = self._feed_factory(self.config)
        self._build_runner()

    def _build_runner(self) -> None:
        self.broker = PaperBroker()
        self.runner = Runner(
            self.config,
            self.broker,
            self.feed.read_quotes,
            clock=self._clock,
            journal=UiJournal(
                self.config.journal_file, self.config.mode, self._clock.now,
                self._events, self._seq,
            ),
        )

    # ------------------------------------------------------------------ 制御

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="kabu-play", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._step()
            except Exception:
                log.exception("シミュレーションの周回でエラーが発生しました")
            self._stop.wait(self.config.poll_interval_sec)

    def _step(self) -> None:
        with self._lock:
            self.runner.tick()
            now_ms = int(time.time() * 1000)
            for symbol, price in self.runner.state.last_price.items():
                if symbol in self._history:
                    self._history[symbol].append((now_ms, price))
            # seq の大きい順（新しい順）に並ぶので、未処理ぶんだけを古い順に数える。
            fresh = [e for e in self._events if e["seq"] > self._last_seq]
            for event in reversed(fresh):
                self._account(event)
                self._last_seq = event["seq"]

    def _account(self, event: dict[str, Any]) -> None:
        """発注・約定の件数と実現損益を数える。"""
        if event["event"] == "ORDER":
            self._orders += 1
            return
        if event["event"] != "FILL":
            return
        self._fills += 1
        try:
            price = float(event["price"])
            qty = int(event["quantity"])
        except (TypeError, ValueError):
            return
        if event["side"] == "BUY":
            self._entry[event["rule_id"]] = price
        else:
            entry = self._entry.pop(event["rule_id"], None)
            if entry is not None:
                self._realized += (price - entry) * qty

    def reset(self) -> None:
        with self._lock:
            self._clear_locked()
            self.feed.reset()
            self._build_runner()

    def _clear_locked(self) -> None:
        """集計・履歴・保存済み状態を消す。呼び出し側でロックを取っていること。"""
        for hist in self._history.values():
            hist.clear()
        self._events.clear()
        self._orders = 0
        self._fills = 0
        self._realized = 0.0
        self._entry.clear()
        self._last_seq = 0
        Path(self.config.state_file).unlink(missing_ok=True)

    def apply_rules(self, raw_rules: list[Any]) -> None:
        """画面で編集されたルールを適用する。

        検証は設定ファイルとまったく同じ Rule.from_dict を通す。おかしなルールは
        本番と同じ日本語のメッセージで弾かれるので、シミュレーターで通ったルールは
        そのまま config.yaml に書ける。
        """
        if not isinstance(raw_rules, list) or not raw_rules:
            raise ConfigError("ルールを 1 つ以上指定してください")
        if len(raw_rules) > _MAX_RULES:
            raise ConfigError(f"ルールは {_MAX_RULES} 件までにしてください")

        rules = tuple(Rule.from_dict(r, i) for i, r in enumerate(raw_rules))
        seen: set[str] = set()
        for rule in rules:
            if rule.id in seen:
                raise ConfigError(f"ルール名 '{rule.id}' が重複しています")
            seen.add(rule.id)

        with self._lock:
            self.config = replace(self.config, rules=rules)
            self._clear_locked()
            self._rebuild()

    def save_to_file(self) -> None:
        """今の画面のルールを config.yaml（`-c` で指定したファイル）に書き戻す。

        rules: 以外のキーやコメントには触れない（config.save_rules 参照）。
        `-c` を指定していない/ファイルが無い場合は ConfigError で弾く。
        """
        if self.config_path is None:
            raise ConfigError("保存先の設定ファイルが分かりません（kabu play を -c 付きで起動してください）")
        with self._lock:
            rules = self.config.rules
        save_rules(self.config_path, rules)

    # ------------------------------------------------------------- スナップ

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            rules = []
            for rule in self.config.rules:
                runtime = self.runner.state.rule(rule.id)
                rules.append(
                    {
                        "id": rule.id,
                        "symbol": rule.symbol,
                        "quantity": rule.quantity,
                        "buy_at": rule.buy_at,
                        "sell_at": rule.sell_at,
                        "stop_loss": rule.stop_loss,
                        "max_cycles": rule.max_cycles,
                        "cycles_done": runtime.cycles_done,
                        "state": runtime.state.value,
                        "entry_price": runtime.entry_price,
                        "position_qty": runtime.position_qty,
                        "note": runtime.last_note,
                        "price": self.runner.state.last_price.get(rule.symbol),
                    }
                )
            return {
                "kind": "engine",
                "now": self.runner.clock.now().isoformat(),
                "config_path": str(self.config_path) if self.config_path else None,
                "auto": self.feed.auto,
                "source": self.feed.describe(),
                "manual": getattr(self.feed, "manual_allowed", True),
                "orders": self._orders,
                "fills": self._fills,
                "realized": round(self._realized, 1),
                "rules": rules,
                "history": {s: list(h) for s, h in self._history.items()},
                "events": list(self._events),
            }


def _load_page() -> str:
    return render_standalone_page()


class _Handler(BaseHTTPRequestHandler):
    session: GameSession
    page: str

    def log_message(self, fmt: str, *args: Any) -> None:  # アクセスログは出さない
        pass

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload: dict[str, Any], status: int = 200) -> None:
        self._send(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if path in ("/", "/index.html"):
            self._send(200, self.page.encode("utf-8"), "text/html; charset=utf-8")
        elif path == "/api/state":
            self._json(self.session.snapshot())
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self) -> None:
        path = self.path.split("?", 1)[0].rstrip("/")
        try:
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError):
            self._json({"error": "bad json"}, 400)
            return

        if path == "/api/price":
            symbol = str(payload.get("symbol", ""))
            try:
                price = float(payload.get("price"))
            except (TypeError, ValueError):
                self._json({"error": "price が数値ではありません"}, 400)
                return
            self.session.feed.set_price(symbol, price)
            self._json({"ok": True})
        elif path == "/api/auto":
            self.session.feed.set_auto(bool(payload.get("auto")))
            self._json({"ok": True, "auto": self.session.feed.auto})
        elif path == "/api/reset":
            self.session.reset()
            self._json({"ok": True})
        elif path == "/api/rules":
            try:
                self.session.apply_rules(payload.get("rules"))
            except ConfigError as exc:
                # 設定ファイルと同じ検証なので、そのまま画面に出せば意味が通る。
                self._json({"ok": False, "error": str(exc)}, 400)
                return

            if not payload.get("save"):
                self._json({"ok": True, "saved": False})
                return
            try:
                self.session.save_to_file()
            except (ConfigError, OSError) as exc:
                # ルールの適用（画面上の反映）は既に成功しているので、ここで失敗しても
                # ok は真のまま。「保存だけ」失敗したことを別に伝える。
                self._json({"ok": True, "saved": False, "save_error": str(exc)})
                return
            self._json({"ok": True, "saved": True, "path": str(self.session.config_path)})
        else:
            self._json({"error": "not found"}, 404)


def _lan_address() -> str | None:
    """同じ Wi-Fi の別端末から届くアドレスを調べる（実際には送信しない）。"""
    import socket

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.settimeout(0.2)
            probe.connect(("192.0.2.1", 9))   # 到達しない前提のテスト用アドレス
            return probe.getsockname()[0]
    except OSError:
        return None


def serve(config: Config, host: str = "127.0.0.1", port: int = 8765,
          feed_factory=None, banner: str = "", config_path: Path | None = None) -> None:
    session = GameSession(config, feed_factory, config_path=config_path)
    session.start()

    handler = type("Handler", (_Handler,), {"session": session, "page": _load_page()})
    httpd = ThreadingHTTPServer((host, port), handler)
    url = f"http://{host}:{port}/"

    print()
    print("  シミュレーターを起動しました（お金は動きません）")
    if banner:
        print(f"  {banner}")
    print(f"  このPCから    →  {url}")

    if host not in ("127.0.0.1", "localhost"):
        lan = _lan_address()
        if lan:
            print(f"  スマホから    →  http://{lan}:{port}/   （同じ Wi-Fi に繋いでください）")
        print("  ※ 同じネットワークの人は誰でもこの画面を開けます。"
              "発注はシミュレーションのみなので実害はありませんが、"
              "使い終わったら Ctrl-C で止めてください。")
    print("  終了するには Ctrl-C")
    print()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n終了します。")
    finally:
        session.stop()
        httpd.server_close()
