"""マーケットスピードII RSS（Excel アドイン）との接続。

RSS は「Excel のセルに関数を書くと値が push されてくる」仕組みなので、
    * 株価は QUOTES シートに =RssMarket(...) を並べておいて、毎回まとめて読む
    * 発注は Application.Run 経由で RSS のマクロ関数を呼ぶ
    * 約定確認は ORDERS シートに出ている注文一覧を見出し行で読む
という三層構成にしている。

関数名・引数の並び・列見出しは RSS のバージョンで変わりうるため、すべて
config.yaml (rss: セクション) 側に逃がしてある。コードを書き換えずに追従できる。
本モジュールは Windows + Excel + xlwings が揃っている環境でのみ動作する。
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Any

from .config import Config
from .errors import BrokerError, QuoteError
from .models import BrokerOrder, OrderIntent, OrderResult, OrderStatus, OrderType, Side

log = logging.getLogger(__name__)

# 「まだ注文が生きている」と判定する RSS 側の状態文字列。
_PENDING_WORDS = ("受付", "訂正中", "取消中", "待機", "発注済", "新規")
_FILLED_WORDS = ("全約定", "約定")
_CANCELLED_WORDS = ("取消", "失効")
_REJECTED_WORDS = ("エラー", "拒否", "不受理")


def _import_xlwings() -> Any:
    try:
        import xlwings  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover - Windows 以外では常にここ
        raise BrokerError(
            "xlwings が見つかりません。Windows で `pip install xlwings` を実行してください。"
            "（Mac/Linux では mode: paper のみ利用できます）"
        ) from exc
    return xlwings


class ExcelBridge:
    """ワークブックを開き、QUOTES シートを組み立てて値を読む。"""

    def __init__(self, config: Config) -> None:
        self._config = config
        self._xw = _import_xlwings()
        self._book = self._open_book()

    def _open_books(self) -> list[Any]:
        """今 開かれているブックを全部集める。

        Excel が 1 つも起動していなければ空リスト（xlwings は
        「Couldn't find any active App!」で落ちるので、ここで吸収する）。
        アクティブでない Excel のブックも見るのは、既に開いてあるブックを
        二重に開かないため。
        """
        xw = self._xw
        try:
            apps = list(xw.apps)
        except Exception:  # pragma: no cover - Excel 未起動などはここに来る
            return []
        books: list[Any] = []
        for app in apps:
            try:
                books.extend(list(app.books))
            except Exception:  # pragma: no cover - 終了しかけの Excel は飛ばす
                continue
        return books

    def _open_book(self) -> Any:
        xw = self._xw
        path = os.path.abspath(self._config.excel.workbook)
        for book in self._open_books():
            try:
                if os.path.abspath(book.fullname) == path:
                    return book
            except Exception:  # pragma: no cover - COM 越しの取得失敗は無視
                continue
        if not os.path.exists(path):
            raise BrokerError(
                f"ワークブックが見つかりません: {path}\n"
                "`kabu init-sheet` で雛形を作るか、excel.workbook のパスを見直してください。"
            )
        try:
            running = bool(list(xw.apps))
        except Exception:  # pragma: no cover - 数えられないなら起動していない扱い
            running = False
        if running:
            app = xw.apps.active
        else:
            # 自分で起動した Excel はアドインを読み込まないので、ここで入れる。
            app = xw.App(visible=self._config.excel.visible, add_book=False)
            self._load_rss_addins(app)
        return app.books.open(path)

    def _load_rss_addins(self, app: Any) -> None:
        """kabu が起動した Excel に RSS アドインを読み込ませる。

        COM（自動化）で起動した Excel は、利用者が「ファイル → オプション →
        アドイン」で登録したアドインを自動では読み込まない。そのままだと
        =RssMarket(...) が未定義のままになり、株価が 1 件も取れない。

        読み込めなくても致命傷にはしない。利用者が自分で開いた Excel に
        繋ぐ道（そちらはアドインが入っている）が残っているため。
        """
        from .excelinfo import VBA_ADDIN, addin_dir, detect_excel

        folder = addin_dir()
        xll_name = detect_excel().xll_name
        targets = [folder / xll_name] if xll_name else []
        targets.append(folder / VBA_ADDIN)

        for target in targets:
            if not target.exists():
                log.warning("RSS アドインが見つかりません: %s", target)
                continue
            try:
                if target.suffix.lower() == ".xll":
                    app.api.RegisterXLL(str(target))
                else:
                    app.api.Workbooks.Open(str(target))
                log.info("RSS アドインを読み込みました: %s", target.name)
            except Exception:
                log.warning("RSS アドインを読み込めませんでした: %s", target, exc_info=True)

    def _sheet(self, name: str) -> Any:
        try:
            return self._book.sheets[name]
        except Exception as exc:
            # Excel との通信そのものが失敗しているのを「シートが無い」と
            # 誤診しない。シート名を一覧できたときだけ不在と判断する。
            try:
                names = [s.name for s in self._book.sheets]
            except Exception as probe_exc:
                raise BrokerError(
                    f"Excel に問い合わせできませんでした: {probe_exc}\n"
                    "Excel やブックが閉じられていないか確認してください。"
                ) from probe_exc
            raise BrokerError(
                f"シート '{name}' がワークブックにありません（あるのは {', '.join(names) or 'なし'}）"
            ) from exc

    @staticmethod
    def _existing_symbols(sheet: Any, count: int) -> list[str]:
        """A 列の 2 行目から count 行ぶんの銘柄コードを読む（1 行のときは値が素で返る）。"""
        raw = sheet.range((2, 1), (count + 1, 1)).value
        cells = raw if isinstance(raw, list) else [raw]
        return [str(c).strip() for c in cells if c is not None]

    def build_quote_sheet(self, symbols: list[str]) -> None:
        """QUOTES シートに銘柄と RSS 関数を書き込む（毎回張り替えず、差分があるときだけ）。"""
        rss = self._config.rss
        sheet = self._sheet(self._config.excel.quote_sheet)
        if self._existing_symbols(sheet, len(symbols)) == symbols:
            return
        sheet.range("A1").value = ["銘柄コード", "現在値", "時刻"]
        for i, symbol in enumerate(symbols, start=2):
            sheet.range((i, 1)).value = symbol
            sheet.range((i, 2)).formula = f'={rss.quote_function}(A{i},"{rss.price_field}")'
            sheet.range((i, 3)).formula = f'={rss.quote_function}(A{i},"{rss.time_field}")'
        log.info("QUOTES シートを %d 銘柄で初期化しました", len(symbols))

    def read_quotes(self, symbols: list[str]) -> dict[str, float]:
        """QUOTES シートから現在値をまとめて読む。数値でないセルは欠測として返さない。"""
        sheet = self._sheet(self._config.excel.quote_sheet)
        last_row = len(symbols) + 1
        values = sheet.range((2, 1), (last_row, 2)).value or []
        if last_row == 2:  # 1 銘柄のときは 1 次元で返ってくる
            values = [values]
        out: dict[str, float] = {}
        seen: list[str] = []
        for row in values:
            if not row or row[0] is None:
                continue
            symbol = str(row[0]).strip()
            # RSS は現在値を数値で返すのが基本だが、セルの書式や RSS の版に
            # よっては "2,980" のような文字列で返ることがある。どちらも受ける。
            price = _to_float(row[1])
            seen.append(f"{symbol}={row[1]!r}")
            if price is not None and price > 0:
                out[symbol] = price
        if not out:
            raise QuoteError(
                "QUOTES シートから有効な株価を 1 件も取得できませんでした。\n"
                f"  読めた中身    : {', '.join(seen) or '（空）'}\n"
                "  1. マーケットスピードII を起動してログインしていますか\n"
                "  2. QUOTES シートの現在値の列は何になっていますか\n"
                "     #NAME? → その Excel に RSS アドインが入っていません。"
                "Excel を自分で開いてから kabu を起動し直してください\n"
                "     空欄   → マーケットスピードII 側から値が来ていません\n"
                "  3. Excel とマーケットスピードII の権限（管理者で実行しているか）は揃っていますか"
            )
        return out

    def read_order_table(self) -> list[dict[str, Any]]:
        """ORDERS シートを見出し行つきの表として読む。"""
        sheet = self._sheet(self._config.excel.order_sheet)
        used = sheet.used_range.value or []
        if not used or not isinstance(used, list) or len(used) < 2:
            return []
        header = [str(h).strip() if h is not None else "" for h in used[0]]
        rows: list[dict[str, Any]] = []
        for raw in used[1:]:
            cells = raw if isinstance(raw, list) else [raw]
            if all(c is None or str(c).strip() == "" for c in cells):
                continue
            rows.append({header[i]: cells[i] for i in range(min(len(header), len(cells)))})
        return rows

    def run_macro(self, name: str, args: list[Any]) -> Any:
        try:
            return self._book.app.macro(name)(*args)
        except Exception as exc:
            raise BrokerError(f"RSS 関数 '{name}' の呼び出しに失敗しました: {exc}") from exc

    def close(self) -> None:
        # ブックは開いたままにする（人間が画面で確認できるほうが安全）。
        pass


class RssBroker:
    """ExcelBridge を使って実際に発注する Broker 実装。"""

    def __init__(self, config: Config, bridge: ExcelBridge) -> None:
        self._config = config
        self._bridge = bridge

    def _password(self) -> str:
        env = self._config.rss.password_env
        pw = os.environ.get(env)
        if not pw:
            raise BrokerError(
                f"取引パスワードが環境変数 {env} に設定されていません。"
                "設定ファイルにパスワードを書かないでください。"
            )
        return pw

    def _build_args(self, intent: OrderIntent) -> list[Any]:
        """config の arg_order に従って RSS 関数の引数列を組み立てる。"""
        rss = self._config.rss
        symbol, _, market = intent.symbol.partition(".")
        values: dict[str, Any] = dict(rss.order_defaults)
        # ここで決まる値は defaults より優先する（売買方向や数量を静的に固定させない）。
        values.update(
            {
                "symbol": symbol,
                "market": market or "T",
                "side": "現物買" if intent.side is Side.BUY else "現物売",
                "quantity": intent.quantity,
                "order_type": "成行" if intent.order_type is OrderType.MARKET else "指値",
                "price": 0 if intent.order_type is OrderType.MARKET else intent.price,
                "password": self._password(),
            }
        )

        missing = [k for k in rss.order_arg_order if k not in values]
        if missing:
            raise BrokerError(
                f"RSS 発注引数 {missing} の値が決まりません。"
                "rss.order.defaults に追記するか rss.order.arg_order を見直してください。"
            )
        return [values[k] for k in rss.order_arg_order]

    def place_order(self, intent: OrderIntent) -> OrderResult:
        args = self._build_args(intent)
        redacted = ["***" if k == "password" else a
                    for k, a in zip(self._config.rss.order_arg_order, args)]
        log.info("発注: %s %s", self._config.rss.order_function, redacted)
        raw = self._bridge.run_macro(self._config.rss.order_function, args)
        order_id = str(raw).strip() if raw is not None else ""
        if not order_id or order_id.startswith("-") or order_id.lower() in ("false", "none", "0"):
            return OrderResult(ok=False, order_id=None, message=f"発注が受け付けられませんでした: {raw!r}")
        return OrderResult(ok=True, order_id=order_id, message="発注受付")

    def cancel_order(self, order_id: str) -> OrderResult:
        raw = self._bridge.run_macro(self._config.rss.cancel_function, [order_id, self._password()])
        return OrderResult(ok=True, order_id=order_id, message=f"取消要求: {raw!r}")

    def list_orders(self) -> list[BrokerOrder]:
        cols = self._config.rss.order_columns
        col_id = cols.get("order_id", "注文番号")
        col_symbol = cols.get("symbol", "銘柄コード")
        col_side = cols.get("side", "売買区分")
        col_qty = cols.get("quantity", "注文数量")
        col_filled = cols.get("filled_quantity", "約定数量")
        col_price = cols.get("avg_price", "約定単価")
        col_status = cols.get("status", "状態")

        orders: list[BrokerOrder] = []
        for row in self._bridge.read_order_table():
            order_id = str(row.get(col_id, "")).strip()
            if not order_id:
                continue
            orders.append(
                BrokerOrder(
                    order_id=order_id,
                    symbol=str(row.get(col_symbol, "")).strip(),
                    side=Side.SELL if "売" in str(row.get(col_side, "")) else Side.BUY,
                    quantity=_to_int(row.get(col_qty)),
                    filled_quantity=_to_int(row.get(col_filled)),
                    avg_price=_to_float(row.get(col_price)),
                    status=parse_status(str(row.get(col_status, ""))),
                )
            )
        return orders

    def close(self) -> None:
        self._bridge.close()


def parse_status(text: str) -> OrderStatus:
    """RSS の日本語の状態文字列を OrderStatus に落とす。

    判定順が重要: 「取消」を含む文字列より先に「全約定」を見る必要はないが、
    「取消中」はまだ生きている注文なので PENDING 側で先に拾う。
    """
    t = text.strip()
    if not t:
        return OrderStatus.UNKNOWN
    if any(w in t for w in _REJECTED_WORDS):
        return OrderStatus.REJECTED
    if any(w in t for w in _PENDING_WORDS):
        return OrderStatus.PENDING
    if any(w in t for w in _FILLED_WORDS):
        return OrderStatus.FILLED
    if any(w in t for w in _CANCELLED_WORDS):
        return OrderStatus.CANCELLED
    return OrderStatus.UNKNOWN


def _to_int(value: Any) -> int:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return int(value)
    try:
        return int(str(value).replace(",", "").strip() or 0)
    except ValueError:
        return 0


def _to_float(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    try:
        text = str(value).replace(",", "").strip()
        return float(text) if text else None
    except ValueError:
        return None


def read_quote_snapshot(bridge: ExcelBridge, symbols: list[str], now: datetime) -> dict[str, Any]:
    """デバッグ用: 現在値を時刻つきで返す。"""
    return {"asof": now.isoformat(timespec="seconds"), "quotes": bridge.read_quotes(symbols)}
