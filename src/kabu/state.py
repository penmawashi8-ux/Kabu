"""ルールごとの状態と当日カウンタの永続化。

途中でプロセスが落ちても、再起動時に「注文を出しっぱなしのまま忘れる」ことがないよう
発注のたびにディスクへ書き出す。書き込みは一時ファイル経由の原子的置換。
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from .models import RuleState


@dataclass
class RuleRuntimeState:
    state: RuleState = RuleState.FLAT
    cycles_done: int = 0
    open_order_id: str | None = None
    open_order_placed_at: float | None = None
    entry_price: float | None = None
    position_qty: int = 0
    last_note: str = ""

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["state"] = self.state.value
        return d

    @staticmethod
    def from_dict(raw: dict[str, Any]) -> "RuleRuntimeState":
        return RuleRuntimeState(
            state=RuleState(raw.get("state", RuleState.FLAT.value)),
            cycles_done=int(raw.get("cycles_done", 0)),
            open_order_id=raw.get("open_order_id"),
            open_order_placed_at=raw.get("open_order_placed_at"),
            entry_price=raw.get("entry_price"),
            position_qty=int(raw.get("position_qty", 0)),
            last_note=str(raw.get("last_note", "")),
        )


@dataclass
class DailyCounters:
    day: str = ""
    orders_placed: int = 0
    notional_used: float = 0.0
    last_order_epoch: float = 0.0

    def roll_over_if_needed(self, today: date) -> bool:
        """日付が変わっていたらカウンタをリセットする。リセットしたら True。"""
        key = today.isoformat()
        if self.day == key:
            return False
        self.day = key
        self.orders_placed = 0
        self.notional_used = 0.0
        self.last_order_epoch = 0.0
        return True


@dataclass
class AppState:
    rules: dict[str, RuleRuntimeState] = field(default_factory=dict)
    daily: DailyCounters = field(default_factory=DailyCounters)
    last_price: dict[str, float] = field(default_factory=dict)

    def rule(self, rule_id: str) -> RuleRuntimeState:
        return self.rules.setdefault(rule_id, RuleRuntimeState())

    def to_dict(self) -> dict[str, Any]:
        return {
            "rules": {k: v.to_dict() for k, v in self.rules.items()},
            "daily": asdict(self.daily),
            "last_price": self.last_price,
        }

    @staticmethod
    def from_dict(raw: dict[str, Any]) -> "AppState":
        return AppState(
            rules={k: RuleRuntimeState.from_dict(v) for k, v in (raw.get("rules") or {}).items()},
            daily=DailyCounters(**(raw.get("daily") or {})),
            last_price={k: float(v) for k, v in (raw.get("last_price") or {}).items()},
        )


class StateStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def load(self) -> AppState:
        if not self.path.exists():
            return AppState()
        try:
            with self.path.open("r", encoding="utf-8") as f:
                return AppState.from_dict(json.load(f))
        except (json.JSONDecodeError, TypeError, ValueError):
            # 壊れた状態ファイルで自動売買を続けるのは危険なので、退避して初期状態から。
            backup = self.path.with_suffix(self.path.suffix + ".corrupt")
            self.path.replace(backup)
            return AppState()

    def save(self, state: AppState) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(state.to_dict(), ensure_ascii=False, indent=2)
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
