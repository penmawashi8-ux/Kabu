"""PaperBroker の約定モデル。"""

from __future__ import annotations

from kabu.broker import PaperBroker
from kabu.models import OrderIntent, OrderStatus, OrderType, Side


def intent(side: Side, price: float, order_type: OrderType = OrderType.LIMIT) -> OrderIntent:
    return OrderIntent(
        rule_id="r1", symbol="7203.T", side=side, quantity=100,
        order_type=order_type, price=price, reason="test",
    )


def test_buy_limit_fills_at_the_better_market_price():
    """2,800 円の買い指値は、板が 2,690 円まで下げていれば 2,690 円で約定する。"""
    broker = PaperBroker()
    broker.place_order(intent(Side.BUY, 2800))
    broker.on_quote("7203.T", 2690)
    order = broker.list_orders()[0]
    assert order.status is OrderStatus.FILLED
    assert order.avg_price == 2690


def test_buy_limit_never_fills_above_the_limit():
    broker = PaperBroker()
    broker.place_order(intent(Side.BUY, 2800))
    broker.on_quote("7203.T", 2800)
    assert broker.list_orders()[0].avg_price == 2800


def test_sell_limit_fills_at_the_better_market_price():
    broker = PaperBroker()
    broker.place_order(intent(Side.SELL, 3000))
    broker.on_quote("7203.T", 3120)
    assert broker.list_orders()[0].avg_price == 3120


def test_limit_does_not_fill_before_the_price_is_crossed():
    broker = PaperBroker()
    broker.place_order(intent(Side.BUY, 2800))
    broker.on_quote("7203.T", 2850)
    assert broker.list_orders()[0].status is OrderStatus.PENDING


def test_market_order_fills_immediately():
    broker = PaperBroker()
    broker.place_order(intent(Side.SELL, 2905, OrderType.MARKET))
    order = broker.list_orders()[0]
    assert order.status is OrderStatus.FILLED
    assert order.avg_price == 2905


def test_cancel_removes_a_pending_order():
    broker = PaperBroker()
    result = broker.place_order(intent(Side.BUY, 2800))
    assert broker.cancel_order(result.order_id).ok
    assert broker.list_orders()[0].status is OrderStatus.CANCELLED
    broker.on_quote("7203.T", 2700)   # 取消後は跨いでも約定しない
    assert broker.list_orders()[0].status is OrderStatus.CANCELLED
