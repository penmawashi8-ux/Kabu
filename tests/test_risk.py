from __future__ import annotations

from datetime import datetime

from kabu.config import RiskConfig
from kabu.models import OrderIntent, OrderType, Quote, Side
from kabu.risk import RiskManager
from kabu.state import AppState

NOW = datetime(2026, 8, 31, 10, 0)
EPOCH = NOW.timestamp()


def intent(quantity: int = 100, price: float = 2800.0, side: Side = Side.BUY) -> OrderIntent:
    return OrderIntent(
        rule_id="r1", symbol="7203.T", side=side, quantity=quantity,
        order_type=OrderType.LIMIT, price=price, reason="test",
    )


def test_blocks_when_daily_order_count_exhausted():
    rm = RiskManager(RiskConfig(max_orders_per_day=2))
    state = AppState()
    state.daily.orders_placed = 2
    assert not rm.check_order(intent(), state, now_epoch=EPOCH).allowed


def test_blocks_oversized_single_order():
    rm = RiskManager(RiskConfig(max_notional_per_order=100_000))
    assert not rm.check_order(intent(quantity=100, price=2800), AppState(), now_epoch=EPOCH).allowed


def test_blocks_when_daily_notional_would_be_exceeded():
    rm = RiskManager(RiskConfig(max_notional_per_day=300_000, max_notional_per_order=1_000_000))
    state = AppState()
    state.daily.notional_used = 200_000
    assert not rm.check_order(intent(price=2000), state, now_epoch=EPOCH).allowed


def test_sell_orders_do_not_consume_daily_buying_budget():
    rm = RiskManager(RiskConfig(max_notional_per_day=300_000, max_notional_per_order=1_000_000))
    state = AppState()
    state.daily.notional_used = 290_000
    assert rm.check_order(intent(price=2000, side=Side.SELL), state, now_epoch=EPOCH).allowed


def test_rapid_fire_is_blocked():
    rm = RiskManager(RiskConfig(min_seconds_between_orders=30, max_notional_per_order=1_000_000))
    state = AppState()
    state.daily.last_order_epoch = EPOCH - 5
    assert not rm.check_order(intent(), state, now_epoch=EPOCH).allowed
    state.daily.last_order_epoch = EPOCH - 31
    assert rm.check_order(intent(), state, now_epoch=EPOCH).allowed


def test_register_order_updates_counters_for_buys_only():
    rm = RiskManager(RiskConfig())
    state = AppState()
    rm.register_order(intent(price=1000), state, now_epoch=EPOCH)
    rm.register_order(intent(price=1000, side=Side.SELL), state, now_epoch=EPOCH)
    assert state.daily.orders_placed == 2
    assert state.daily.notional_used == 100_000


def test_rejects_stale_quote():
    rm = RiskManager(RiskConfig(quote_max_age_sec=30))
    quote = Quote(symbol="7203.T", price=2800, asof=NOW)
    assert not rm.validate_quote(quote, AppState(), now_epoch=EPOCH + 120).allowed


def test_rejects_price_spike():
    rm = RiskManager(RiskConfig(max_price_deviation_pct=5))
    state = AppState()
    state.last_price["7203.T"] = 2800
    spike = Quote(symbol="7203.T", price=4000, asof=NOW)
    assert not rm.validate_quote(spike, state, now_epoch=EPOCH).allowed
    normal = Quote(symbol="7203.T", price=2850, asof=NOW)
    assert rm.validate_quote(normal, state, now_epoch=EPOCH).allowed


def test_rejects_non_positive_price():
    rm = RiskManager(RiskConfig())
    assert not rm.validate_quote(Quote("7203.T", 0.0, NOW), AppState(), now_epoch=EPOCH).allowed


def test_kill_switch_detected(tmp_path):
    path = tmp_path / "STOP"
    rm = RiskManager(RiskConfig(kill_switch_file=str(path)))
    assert not rm.kill_switch_engaged()
    path.write_text("stop")
    assert rm.kill_switch_engaged()
