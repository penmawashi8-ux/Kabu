"""発注経路の抽象化。

Runner は Broker インターフェースだけを見る。実弾（RssBroker）と
ペーパートレード（PaperBroker）を差し替えても売買ロジックは 1 行も変わらない。
"""

from __future__ import annotations

import itertools
from abc import ABC, abstractmethod
from dataclasses import replace

from .models import BrokerOrder, OrderIntent, OrderResult, OrderStatus, OrderType, Side


class Broker(ABC):
    @abstractmethod
    def place_order(self, intent: OrderIntent) -> OrderResult:
        """注文を出す。約定の確認は list_orders() 側で行う。"""

    @abstractmethod
    def cancel_order(self, order_id: str) -> OrderResult:
        ...

    @abstractmethod
    def list_orders(self) -> list[BrokerOrder]:
        """当日の注文一覧。約定確認はここを唯一の真実として扱う。"""

    def close(self) -> None:  # pragma: no cover - 既定では何もしない
        pass


def _fill_price(side: Side, limit: float, market: float) -> float:
    """指値が市場価格を跨いだときの約定値段。

    実際の板では有利なほうで約定する（2,800 円の買い指値は、板が 2,690 円なら
    2,690 円で約定する）。指値そのもので約定させると、下振れした瞬間に必ず
    高値掴みしたことになり、損益がシミュレーションのたびに実際より悪く出る。
    """
    return min(limit, market) if side is Side.BUY else max(limit, market)


class PaperBroker(Broker):
    """約定シミュレータ。

    指値は「気配が指値を跨いだら約定」、成行は即時約定。
    実弾に切り替える前の検証と、テストでの状態遷移確認に使う。
    """

    def __init__(self) -> None:
        self._orders: dict[str, BrokerOrder] = {}
        self._pending_prices: dict[str, float | None] = {}
        self._ids = itertools.count(1)

    def place_order(self, intent: OrderIntent) -> OrderResult:
        order_id = f"PAPER-{next(self._ids):05d}"
        if intent.order_type is OrderType.MARKET:
            fill_price = intent.price or 0.0
            self._orders[order_id] = BrokerOrder(
                order_id=order_id,
                symbol=intent.symbol,
                side=intent.side,
                quantity=intent.quantity,
                filled_quantity=intent.quantity,
                avg_price=fill_price,
                status=OrderStatus.FILLED,
            )
        else:
            self._orders[order_id] = BrokerOrder(
                order_id=order_id,
                symbol=intent.symbol,
                side=intent.side,
                quantity=intent.quantity,
                filled_quantity=0,
                avg_price=None,
                status=OrderStatus.PENDING,
            )
            self._pending_prices[order_id] = intent.price
        return OrderResult(ok=True, order_id=order_id, message="ペーパー発注")

    def cancel_order(self, order_id: str) -> OrderResult:
        order = self._orders.get(order_id)
        if order is None:
            return OrderResult(ok=False, order_id=order_id, message="注文が見つかりません")
        if order.status is not OrderStatus.PENDING:
            return OrderResult(ok=False, order_id=order_id, message=f"取消不可: {order.status.value}")
        self._orders[order_id] = replace(order, status=OrderStatus.CANCELLED)
        self._pending_prices.pop(order_id, None)
        return OrderResult(ok=True, order_id=order_id, message="取消しました")

    def list_orders(self) -> list[BrokerOrder]:
        return list(self._orders.values())

    def on_quote(self, symbol: str, price: float) -> None:
        """新しい値段が来たら、跨いだ指値を約定させる。"""
        for order_id, limit in list(self._pending_prices.items()):
            order = self._orders[order_id]
            if order.symbol != symbol or limit is None:
                continue
            crossed = price <= limit if order.side is Side.BUY else price >= limit
            if crossed:
                self._orders[order_id] = replace(
                    order,
                    filled_quantity=order.quantity,
                    avg_price=_fill_price(order.side, limit, price),
                    status=OrderStatus.FILLED,
                )
                self._pending_prices.pop(order_id, None)
