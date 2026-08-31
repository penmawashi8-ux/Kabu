"""RSS 用ワークブックの雛形を作る（Windows + Excel 環境専用）。

QUOTES シートは kabu 側が自動で組み立てるので空で良い。
ORDERS シートは RSS の注文一覧関数を人が 1 度だけ貼る必要があるため、
どこに何を入れるかの案内をシートに書き込んでおく。
"""

from __future__ import annotations

from pathlib import Path

from .config import Config
from .errors import BrokerError

_GUIDE = [
    "このシートに「注文一覧」を表示させてください。",
    "マーケットスピードII RSS の注文一覧関数を A1 に入力すると、見出し行つきの表が展開されます。",
    "kabu は 1 行目を見出しとして読み、config.yaml の rss.order_columns で列名を対応づけます。",
    "既定で見ている列名: 注文番号 / 銘柄コード / 売買区分 / 注文数量 / 約定数量 / 約定単価 / 状態",
    "実際の見出しが違う場合は config.yaml 側を実際の文字列に合わせてください。",
]


def write_template(config: Config) -> Path:
    try:
        import xlwings as xw  # type: ignore[import-not-found]
    except ImportError as exc:
        raise BrokerError(
            "init-sheet は Windows + Excel + xlwings が必要です。"
            "他の環境では Excel で手動でブックを作り、QUOTES と ORDERS シートを用意してください。"
        ) from exc

    path = Path(config.excel.workbook).absolute()
    if path.exists():
        raise BrokerError(f"既にファイルが存在します: {path}（上書きしません）")
    path.parent.mkdir(parents=True, exist_ok=True)

    app = xw.App(visible=False, add_book=True)
    try:
        book = app.books[0]
        quotes = book.sheets[0]
        quotes.name = config.excel.quote_sheet
        quotes.range("A1").value = ["銘柄コード", "現在値", "時刻"]

        orders = book.sheets.add(config.excel.order_sheet, after=quotes)
        for i, line in enumerate(_GUIDE, start=1):
            orders.range((i + 2, 1)).value = line

        book.save(str(path))
        book.close()
    finally:
        app.quit()
    return path
