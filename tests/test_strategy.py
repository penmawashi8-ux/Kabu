from __future__ import annotations

from datetime import datetime

from conftest import make_rule
from kabu.models import OrderType, Quote, RuleState, Side
from kabu.state import RuleRuntimeState
from kabu.strategy import decide

NOW = datetime(2026, 8, 31, 10, 0)


def quote(price: float, symbol: str = "7203.T") -> Quote:
    return Quote(symbol=symbol, price=price, asof=NOW)


def test_buys_when_price_reaches_trigger():
    intent = decide(make_rule(), RuleRuntimeState(), quote(2800), allow_entry=True)
    assert intent is not None
    assert intent.side is Side.BUY
    assert intent.quantity == 100
    assert intent.price == 2800


def test_does_not_buy_above_trigger():
    assert decide(make_rule(), RuleRuntimeState(), quote(2801), allow_entry=True) is None


def test_does_not_buy_when_entries_are_closed():
    assert decide(make_rule(), RuleRuntimeState(), quote(2700), allow_entry=False) is None


def test_does_not_buy_after_max_cycles():
    runtime = RuleRuntimeState(state=RuleState.FLAT, cycles_done=1)
    assert decide(make_rule(max_cycles=1), runtime, quote(2700), allow_entry=True) is None


def test_sells_when_price_reaches_target():
    runtime = RuleRuntimeState(state=RuleState.HOLDING, position_qty=100, entry_price=2800)
    intent = decide(make_rule(), runtime, quote(3000), allow_entry=True)
    assert intent is not None
    assert intent.side is Side.SELL
    assert intent.price == 3000


def test_stop_loss_exits_with_market_order():
    runtime = RuleRuntimeState(state=RuleState.HOLDING, position_qty=100, entry_price=2800)
    intent = decide(make_rule(stop_loss=2700.0), runtime, quote(2699), allow_entry=True)
    assert intent is not None
    assert intent.side is Side.SELL
    assert intent.order_type is OrderType.MARKET


def test_holding_does_nothing_between_triggers():
    runtime = RuleRuntimeState(state=RuleState.HOLDING, position_qty=100)
    assert decide(make_rule(stop_loss=2700.0), runtime, quote(2900), allow_entry=True) is None


def test_force_exit_sells_regardless_of_price():
    runtime = RuleRuntimeState(state=RuleState.HOLDING, position_qty=100)
    intent = decide(make_rule(), runtime, quote(2850), allow_entry=True, force_exit=True)
    assert intent is not None
    assert intent.side is Side.SELL
    assert intent.order_type is OrderType.MARKET


def test_force_exit_does_not_open_new_position():
    assert decide(make_rule(), RuleRuntimeState(), quote(2700), allow_entry=True, force_exit=True) is None


def test_no_double_order_while_pending():
    for state in (RuleState.BUY_PENDING, RuleState.SELL_PENDING, RuleState.HALTED, RuleState.DONE):
        runtime = RuleRuntimeState(state=state, position_qty=100)
        assert decide(make_rule(), runtime, quote(2700), allow_entry=True) is None


def test_disabled_rule_is_ignored():
    assert decide(make_rule(enabled=False), RuleRuntimeState(), quote(2700), allow_entry=True) is None


def test_sell_uses_actual_position_size():
    runtime = RuleRuntimeState(state=RuleState.HOLDING, position_qty=200)
    intent = decide(make_rule(), runtime, quote(3100), allow_entry=True)
    assert intent is not None
    assert intent.quantity == 200
