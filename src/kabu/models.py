"""売買ロジック全体で共有する値オブジェクト。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"

    @property
    def japanese(self) -> str:
        return "買い" if self is Side.BUY else "売り"


class OrderType(str, Enum):
    LIMIT = "limit"
    MARKET = "market"


class OrderStatus(str, Enum):
    PENDING = "PENDING"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    UNKNOWN = "UNKNOWN"

    @property
    def is_terminal(self) -> bool:
        return self in (OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED)


class RuleState(str, Enum):
    """1 ルールのライフサイクル。

    FLAT -> BUY_PENDING -> HOLDING -> SELL_PENDING -> FLAT (1 サイクル完了)
    HALTED は人間の確認が必要な状態で、自動では抜けない。
    """

    FLAT = "FLAT"
    BUY_PENDING = "BUY_PENDING"
    HOLDING = "HOLDING"
    SELL_PENDING = "SELL_PENDING"
    DONE = "DONE"
    HALTED = "HALTED"


@dataclass(frozen=True)
class Quote:
    symbol: str
    price: float
    asof: datetime


@dataclass(frozen=True)
class OrderIntent:
    """まだ発注していない「発注したい内容」。リスク検査はこの段階で行う。"""

    rule_id: str
    symbol: str
    side: Side
    quantity: int
    order_type: OrderType
    price: float | None
    reason: str

    @property
    def notional(self) -> float:
        """概算約定代金。成行はリスク計算用に参照価格を price に入れておく。"""
        return (self.price or 0.0) * self.quantity


@dataclass(frozen=True)
class BrokerOrder:
    """証券会社側に存在する注文の現在状態。"""

    order_id: str
    symbol: str
    side: Side
    quantity: int
    filled_quantity: int
    avg_price: float | None
    status: OrderStatus


@dataclass(frozen=True)
class OrderResult:
    ok: bool
    order_id: str | None
    message: str
