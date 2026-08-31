"""PaperBroker を使って、買い→約定→売り→約定の一連の流れを検証する。"""

from __future__ import annotations

from datetime import datetime

import pytest
from conftest import make_rule

from kabu.broker import PaperBroker
from kabu.config import RiskConfig
from kabu.errors import KillSwitch
from kabu.models import OrderStatus, OrderType, RuleState, Side
from kabu.runner import Runner

OPEN = datetime(2026, 8, 31, 10, 0)  # 2026-08-31 は月曜


class ScriptedFeed:
    """テスト内で株価を手で動かすための供給源。"""

    def __init__(self, price: float) -> None:
        self.price = price

    def __call__(self, symbols: list[str]) -> dict[str, float]:
        return {s: self.price for s in symbols}


def make_runner(config, feed) -> Runner:
    return Runner(config, PaperBroker(), feed)


def test_full_buy_then_sell_cycle(config_factory):
    rule = make_rule(stop_loss=None, max_cycles=1)
    config = config_factory(rules=[rule])
    feed = ScriptedFeed(3100)
    runner = make_runner(config, feed)

    # まだ買いトリガーに届いていない
    runner.tick(OPEN)
    assert runner.state.rule("r1").state is RuleState.FLAT

    # 買いトリガーに到達 → 発注、PaperBroker が同じ周回で約定させる
    feed.price = 2795
    runner.tick(OPEN)
    assert runner.state.rule("r1").state is RuleState.BUY_PENDING

    runner.tick(OPEN)
    runtime = runner.state.rule("r1")
    assert runtime.state is RuleState.HOLDING
    assert runtime.position_qty == 100
    assert runtime.entry_price == 2800

    # 売りトリガーに到達
    feed.price = 3050
    runner.tick(OPEN)
    assert runner.state.rule("r1").state is RuleState.SELL_PENDING

    runner.tick(OPEN)
    runtime = runner.state.rule("r1")
    assert runtime.state is RuleState.DONE   # max_cycles=1 なので完了
    assert runtime.cycles_done == 1
    assert runtime.position_qty == 0


def test_cycle_repeats_until_max_cycles(config_factory):
    config = config_factory(rules=[make_rule(max_cycles=2)])
    feed = ScriptedFeed(2795)
    runner = make_runner(config, feed)

    for _ in range(2):
        feed.price = 2795
        runner.tick(OPEN)  # 買い発注
        runner.tick(OPEN)  # 約定確認
        feed.price = 3050
        runner.tick(OPEN)  # 売り発注
        runner.tick(OPEN)  # 約定確認

    runtime = runner.state.rule("r1")
    assert runtime.cycles_done == 2
    assert runtime.state is RuleState.DONE


def test_no_duplicate_orders_while_pending(config_factory):
    """指値に届かないまま何周回っても、注文は 1 本しか出ない。"""
    config = config_factory(rules=[make_rule(order_type=OrderType.LIMIT)])
    feed = ScriptedFeed(2795)
    runner = make_runner(config, feed)

    runner.tick(OPEN)
    # 約定しない値段に戻す（指値 2800 に対して上で推移）
    feed.price = 2900
    for _ in range(5):
        runner.tick(OPEN)

    orders = runner.broker.list_orders()
    assert len(orders) == 1
    assert orders[0].status is OrderStatus.PENDING


def test_kill_switch_stops_the_tick(config_factory, tmp_path):
    stop = tmp_path / "STOP"
    stop.write_text("stop")
    config = config_factory(risk=RiskConfig(kill_switch_file=str(stop)))
    runner = make_runner(config, ScriptedFeed(2795))
    with pytest.raises(KillSwitch):
        runner.tick(OPEN)


def test_no_trading_outside_market_hours(config_factory):
    config = config_factory()
    runner = make_runner(config, ScriptedFeed(2000))
    result = runner.tick(datetime(2026, 8, 31, 8, 0))
    assert not result.ran and result.reason == "closed"
    assert runner.state.rule("r1").state is RuleState.FLAT


def test_no_new_entry_after_cutoff_but_exit_still_works(config_factory):
    config = config_factory(rules=[make_rule(max_cycles=2)])
    feed = ScriptedFeed(2795)
    runner = make_runner(config, feed)

    late = datetime(2026, 8, 31, 15, 25)  # trade_cutoff 15:20 を過ぎている
    runner.tick(late)
    assert runner.state.rule("r1").state is RuleState.FLAT  # 新規は入らない

    # 既に保有している状態を作って、売りは通ることを確認する
    runtime = runner.state.rule("r1")
    runtime.state = RuleState.HOLDING
    runtime.position_qty = 100
    runtime.entry_price = 2800
    feed.price = 3050
    runner.tick(late)
    assert runner.state.rule("r1").state is RuleState.SELL_PENDING


def test_risk_limit_blocks_order(config_factory):
    config = config_factory(
        rules=[make_rule()],
        risk=RiskConfig(max_notional_per_order=1000, min_seconds_between_orders=0),
    )
    runner = make_runner(config, ScriptedFeed(2795))
    runner.tick(OPEN)
    assert runner.state.rule("r1").state is RuleState.FLAT
    assert runner.broker.list_orders() == []


def test_price_spike_is_ignored(config_factory):
    config = config_factory(risk=RiskConfig(max_price_deviation_pct=5, min_seconds_between_orders=0))
    feed = ScriptedFeed(3000)
    runner = make_runner(config, feed)
    runner.tick(OPEN)                       # last_price = 3000 を記録

    feed.price = 100                        # 誤配信を模した飛び値。買いトリガー以下だが無視される
    runner.tick(OPEN)
    assert runner.state.rule("r1").state is RuleState.FLAT
    assert runner.state.last_price["7203.T"] == 3000


def test_state_survives_restart(config_factory):
    config = config_factory(rules=[make_rule(max_cycles=2)])
    feed = ScriptedFeed(2795)
    runner = make_runner(config, feed)
    runner.tick(OPEN)
    runner.tick(OPEN)
    assert runner.state.rule("r1").state is RuleState.HOLDING

    # 同じ設定で作り直すと、保存済み状態から再開する
    revived = make_runner(config, feed)
    runtime = revived.state.rule("r1")
    assert runtime.state is RuleState.HOLDING
    assert runtime.position_qty == 100
    assert runtime.entry_price == 2800


def test_stuck_order_halts_the_rule(config_factory):
    config = config_factory(risk=RiskConfig(pending_timeout_sec=60, min_seconds_between_orders=0))
    feed = ScriptedFeed(2795)
    runner = make_runner(config, feed)
    runner.tick(OPEN)
    assert runner.state.rule("r1").state is RuleState.BUY_PENDING

    feed.price = 2900  # 約定しないまま時間だけ進む
    runner.tick(datetime(2026, 8, 31, 10, 5))
    assert runner.state.rule("r1").state is RuleState.HALTED


def test_forced_close_at_end_of_day(config_factory):
    from datetime import time as dtime

    from kabu.config import MarketConfig

    config = config_factory(
        market=MarketConfig(
            sessions=((dtime(9, 0), dtime(11, 30)), (dtime(12, 30), dtime(15, 30))),
            trade_cutoff=dtime(15, 20),
            close_positions_at=dtime(15, 25),
        )
    )
    feed = ScriptedFeed(2850)
    runner = make_runner(config, feed)
    runtime = runner.state.rule("r1")
    runtime.state = RuleState.HOLDING
    runtime.position_qty = 100
    runtime.entry_price = 2800

    runner.tick(datetime(2026, 8, 31, 15, 26))
    orders = runner.broker.list_orders()
    assert len(orders) == 1
    assert orders[0].side is Side.SELL
    assert orders[0].status is OrderStatus.FILLED  # 成行なので即約定


def test_journal_records_orders_and_fills(config_factory, tmp_path):
    config = config_factory()
    feed = ScriptedFeed(2795)
    runner = make_runner(config, feed)
    runner.tick(OPEN)
    runner.tick(OPEN)

    text = (tmp_path / "journal.csv").read_text(encoding="utf-8-sig")
    assert "ORDER" in text
    assert "FILL" in text
    assert "7203.T" in text
