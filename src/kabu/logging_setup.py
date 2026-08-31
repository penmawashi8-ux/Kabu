"""ログ設定。画面とファイルの両方に出す。"""

from __future__ import annotations

import logging
import sys
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from zoneinfo import ZoneInfo

_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"


def setup_logging(log_file: str | Path, *, verbose: bool = False, timezone: str = "Asia/Tokyo") -> None:
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    formatter = logging.Formatter(_FORMAT, datefmt=_DATEFMT)
    # サーバの locale ではなく取引所の時刻でログを残す。
    tz = ZoneInfo(timezone)
    formatter.converter = lambda ts: datetime.fromtimestamp(ts, tz).timetuple()

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    root.addHandler(console)

    _install_file_handler(path=Path(log_file), formatter=formatter, root=root)


def quiet_analysis(verbose: bool = False) -> None:
    """バックテストや探索の間、1 周ごとのログを画面に出さない。

    これらは何千周も回すため、そのまま出すと結果の要約が流れて読めなくなる。
    記録はログファイルには残るので、追いたいときは -v を付ければ画面にも出る。
    """
    if not verbose:
        logging.getLogger("kabu").setLevel(logging.ERROR)


def _install_file_handler(*, path: Path, formatter, root) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rotating = RotatingFileHandler(path, maxBytes=5_000_000, backupCount=5, encoding="utf-8")
    rotating.setFormatter(formatter)
    root.addHandler(rotating)
