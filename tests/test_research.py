"""手法比較の検査。

一番大事なのは「未来を覗いていないこと」。当日の終値を見て当日の終値で
売買できてしまうと、どんな手法でも勝ってしまう。
"""

from __future__ import annotations

from datetime import date, timedelta

from kabu.marketdata import Bar
from kabu.research import (
    atr, buy_and_hold, compare, fixed_band, rsi, rsi_reversion, sma, sma_trend,
    trailing_stop,
)


def _bars(closes: list[float], *, start=date(2026, 1, 5)) -> list[Bar]:
    bars, day = [], start
    for close in closes:
        while day.weekday() >= 5:
            day += timedelta(days=1)
        bars.append(Bar(day=day, open=close, high=close * 1.01,
                        low=close * 0.99, close=close))
        day += timedelta(days=1)
    return bars


# ------------------------------------------------------------------ 指標

def test_sma_waits_for_enough_data():
    assert sma([1, 2, 3, 4], 3) == [None, None, 2.0, 3.0]


def test_rsi_is_100_when_only_rising():
    values = rsi([1, 2, 3, 4, 5], period=2)
    assert values[-1] == 100.0


def test_atr_measures_the_daily_range():
    bars = [Bar(day=date(2026, 1, 5) + timedelta(days=i), open=100, high=102, low=98, close=100)
            for i in range(5)]
    assert atr(bars, period=3)[-1] == 4.0


# -------------------------------------------------------------- 執行の公平性

def test_execution_happens_on_the_next_open_not_todays_close():
    """判断した日の終値では約定しない（未来を覗かない）。"""
    bars = _bars([100, 100, 100, 90, 130])
    # 4 本目で大きく下げ、5 本目で急騰する。終値で判断→翌日の寄りで約定なら、
    # 90 を見て買った約定値は 5 本目の寄り（=130）になる。
    outcome = fixed_band(bars, buy_pct=-5.0, sell_pct=20.0, stop_pct=-50.0)
    entries = [p.entry_price for p in outcome.positions]
    assert 90 not in entries, "判断した日の終値で買えてしまってはいけない"
    assert entries == [130]


def test_open_position_is_closed_at_the_last_close():
    """最後まで持っていたら最終日の終値で評価する（塩漬けを勝ちに見せない）。"""
    bars = _bars([100, 101, 102, 103, 104])
    outcome = buy_and_hold(bars)
    assert outcome.positions[-1].exit_price == 104


# ------------------------------------------------------------------ 各手法

def test_buy_and_hold_captures_the_whole_move():
    outcome = buy_and_hold(_bars([100, 100, 120]))
    assert outcome.return_pct > 0
    assert outcome.trades == 1


def test_trend_following_buys_on_the_way_up():
    rising = _bars([100 + i for i in range(120)])
    outcome = sma_trend(rising, fast=5, slow=20)
    assert outcome.trades >= 1
    assert outcome.return_pct > 0


def test_trend_following_stays_out_while_falling():
    falling = _bars([200 - i for i in range(120)])
    outcome = sma_trend(falling, fast=5, slow=20)
    assert outcome.exposure_pct < 20, "下げ相場ではほとんど持たないこと"


def test_rsi_reversion_does_not_buy_below_the_trend_filter():
    """長期の下降局面では買わない（落ちるナイフを掴まない）。"""
    falling = _bars([300 - i for i in range(150)])
    outcome = rsi_reversion(falling, period=2, entry=10.0, trend=100)
    assert outcome.trades == 0


def test_trailing_stop_exits_after_a_drop_from_the_peak():
    path = _bars([100] * 30 + [100 + i * 2 for i in range(40)] + [180 - i * 6 for i in range(20)])
    outcome = trailing_stop(path, fast=5, slow=20, trail_pct=8.0)
    assert outcome.trades >= 1
    exits = [p.exit_price for p in outcome.positions if p.exit_price]
    assert max(exits) < 180, "天井では売れない（高値から一定 % 下げて初めて降りる）"


def test_max_drawdown_is_reported_as_a_negative_number():
    outcome = buy_and_hold(_bars([100, 100, 120, 60, 80]))
    assert outcome.max_drawdown_pct < 0


def test_compare_runs_every_strategy():
    bars = _bars([100 + (i % 7) * 3 for i in range(260)])
    outcomes = compare(bars)
    assert len(outcomes) == 6
    assert len({o.name for o in outcomes}) == 6


# ------------------------------------------------------------ 税金と滑り

def test_costs_are_taken_off_every_round_trip():
    """滑りは 1 往復につき 2 回ぶん（買いと売り）引く。"""
    outcome = buy_and_hold(_bars([100, 100, 110]))
    gross = outcome.return_pct
    net = outcome.net_return_pct(tax_pct=0.0, slippage_pct=0.5)
    assert abs((gross - net) - 1.0) < 1e-9


def test_tax_applies_only_to_a_profit():
    profit = buy_and_hold(_bars([100, 100, 200]))
    assert profit.net_return_pct(tax_pct=20.0, slippage_pct=0.0) == pytest_approx(80.0)

    loss = buy_and_hold(_bars([100, 100, 50]))
    # 負けたときに税金は取られない（そのままの損失）。
    assert loss.net_return_pct(tax_pct=20.0, slippage_pct=0.0) == pytest_approx(-50.0)


def test_frequent_trading_pays_more_slippage():
    """売買回数が多い手法ほど滑りで不利になること。"""
    choppy = _bars([100, 90, 110, 90, 110, 90, 110, 90, 110])
    active = fixed_band(choppy, buy_pct=-5.0, sell_pct=5.0, stop_pct=-30.0)
    assert active.trades >= 2
    gross = active.return_pct
    net = active.net_return_pct(tax_pct=0.0, slippage_pct=0.5)
    assert gross - net == pytest_approx(active.trades * 1.0)


def pytest_approx(value, tol=1e-6):
    class _Approx:
        def __eq__(self, other): return abs(other - value) < tol
        def __repr__(self): return f"≈{value}"
    return _Approx()
