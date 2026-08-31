"""売買記録を CSV に追記する。

確定申告や後からの検証で使うので、1 行 = 1 イベントで人間が読める形にしておく。
"""

from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path
from typing import Callable

FIELDS = (
    "timestamp",
    "event",
    "rule_id",
    "symbol",
    "side",
    "quantity",
    "price",
    "order_id",
    "mode",
    "note",
)


class Journal:
    def __init__(
        self,
        path: str | Path,
        mode: str,
        now_provider: Callable[[], datetime] = datetime.now,
    ) -> None:
        self.path = Path(path)
        self.mode = mode
        # 記録は取引所のタイムゾーンで残す。サーバの locale に引きずられないようにする。
        self._now = now_provider

    def record(
        self,
        *,
        event: str,
        rule_id: str = "",
        symbol: str = "",
        side: str = "",
        quantity: int | str = "",
        price: float | str = "",
        order_id: str = "",
        note: str = "",
        at: datetime | None = None,
    ) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        is_new = not self.path.exists() or self.path.stat().st_size == 0
        with self.path.open("a", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=FIELDS)
            if is_new:
                writer.writeheader()
            writer.writerow(
                {
                    "timestamp": (at or self._now()).isoformat(timespec="seconds"),
                    "event": event,
                    "rule_id": rule_id,
                    "symbol": symbol,
                    "side": side,
                    "quantity": quantity,
                    "price": price,
                    "order_id": order_id,
                    "mode": self.mode,
                    "note": note,
                }
            )
