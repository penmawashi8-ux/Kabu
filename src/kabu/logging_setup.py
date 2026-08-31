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

    path = Path(log_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    rotating = RotatingFileHandler(path, maxBytes=5_000_000, backupCount=5, encoding="utf-8")
    rotating.setFormatter(formatter)
    root.addHandler(rotating)
