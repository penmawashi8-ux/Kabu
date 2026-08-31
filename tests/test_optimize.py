"""値幅の総当たり探索の検査。

一番大事なのは「検証期間を選定に使っていないこと」。ここが漏れると、
過去に合わせ込んだだけの数字を実力と勘違いする。
"""

from __future__ import annotations

import math
from datetime import date, timedelta

from kabu.marketdata import Bar
from kabu.optimize import (
    Candidate, default_grid, optimize_symbol, split_days,
)


def _series(n: int, *, start=date(2026, 1, 5), base=3000.0, swing=0.05) -> list[Bar]:
    """上下に振れ続ける値動きを作る（1 日おきに下げ→戻しを繰り返す）。"""
    bars = []
    day = start
    for i in range(n):
        while day.weekday() >= 5:
            day += timedelta(days=1)
        wave = math.sin(i / 2) * swing
        mid = base * (1 + wave)
        bars.append(Bar(day=day, open=mid, high=mid * 1.02, low=mid * 0.98, close=mid))
        day += timedelta(days=1)
    return bars


def test_grid_is_always_valid_as_a_rule():
    """候補はすべて「損切り < 買い < 売り」を満たす（設定検証を通る）。"""
    for c in default_grid():
        assert c.stop_pct < c.buy_pct < 0 < c.sell_pct


def test_candidate_converts_percentages_to_prices():
    rule = Candidate(-4.0, 4.0, -8.0).to_rule("r", "7203.T", 3000.0, 100)
    assert rule.buy_at == 2880.0
    assert rule.sell_at == 3120.0
    assert rule.stop_loss == 2760.0


def test_split_keeps_the_holdout_at_the_end():
    days = [date(2026, 1, 1) + timedelta(days=i) for i in range(10)]
    train, holdout = split_days(days, 3)
    assert train == days[:7]
    assert holdout == days[7:]
    assert set(train).isdisjoint(holdout), "検証期間は探索期間と重ならない"


def test_short_history_is_skipped_with_a_reason(config_factory):
    report = optimize_symbol(config_factory(), "7203.T", _series(10), holdout=5)
    assert report.skipped
    assert report.train == []


def test_expensive_stock_is_skipped_with_the_reason(config_factory):
    """1 単元が発注上限を超える銘柄は、全滅する前に理由を出して外す。"""
    report = optimize_symbol(
        config_factory(), "6758.T", _series(120, base=4000.0), holdout=30, quantity=100,
    )
    assert "上限" in report.skipped


def test_finds_a_workable_setting_on_a_swinging_market(config_factory):
    report = optimize_symbol(
        config_factory(), "7203.T", _series(160, base=2500.0), holdout=40, quantity=100,
    )
    assert report.skipped == ""
    assert report.train, "探索期間の上位が出ること"
    assert len(report.holdout) == len(report.train), "上位はすべて検証期間でも回すこと"
    assert report.train[0].trades > 0


def test_holdout_is_evaluated_on_the_same_candidates(config_factory):
    """検証期間では、探索期間で選んだ候補だけを回す（選び直さない）。"""
    report = optimize_symbol(
        config_factory(), "7203.T", _series(160, base=2500.0), holdout=40, top=3,
    )
    assert [s.candidate for s in report.holdout] == [s.candidate for s in report.train]


def test_hold_return_is_reported_for_comparison(config_factory):
    """持ちっぱなしの成績も出す（売買を繰り返す価値があったかの比較用）。"""
    rising = [
        Bar(day=date(2026, 1, 5) + timedelta(days=i), open=1000 + i, high=1010 + i,
            low=990 + i, close=1005 + i)
        for i in range(160)
    ]
    report = optimize_symbol(config_factory(), "7203.T", rising, holdout=40)
    assert report.hold_return_pct > 0


def test_scores_expose_rate_and_win_rate(config_factory):
    report = optimize_symbol(
        config_factory(), "7203.T", _series(160, base=2500.0), holdout=40,
    )
    best = report.train[0]
    assert best.capital == best.reference * best.quantity
    assert 0 <= best.win_rate <= 100
    assert best.return_pct == best.realized / best.capital * 100


def test_bands_narrower_than_a_days_range_are_excluded():
    """日足では確かめようのない狭いバンドは、探索対象から外す。"""
    from kabu.optimize import wide_enough

    grid = [Candidate(-1.0, 1.0, -2.0), Candidate(-4.0, 4.0, -8.0)]
    kept = wide_enough(grid, daily_range_pct=3.0)
    assert kept == [Candidate(-4.0, 4.0, -8.0)]


def test_stop_too_close_to_the_buy_line_is_excluded():
    """買った直後に日中の揺れで切られ続ける設定も、日足では確かめられない。"""
    from kabu.optimize import wide_enough

    tight_stop = Candidate(-1.0, 3.0, -1.5)     # 買いと損切りの差が 0.5%
    roomy = Candidate(-4.0, 4.0, -8.0)          # 差は 4.0%
    assert wide_enough([tight_stop, roomy], daily_range_pct=2.4) == [roomy]


def test_all_candidates_kept_when_none_are_wide_enough():
    """全部落ちてしまう場合は落とさない（結果が空になるより情報が残るほうがよい）。"""
    from kabu.optimize import wide_enough

    grid = [Candidate(-1.0, 1.0, -2.0)]
    assert wide_enough(grid, daily_range_pct=99.0) == grid


def test_median_daily_range_is_measured_on_the_window():
    from datetime import date as _d

    from kabu.optimize import median_daily_range_pct

    series = [
        Bar(day=_d(2026, 1, 5), open=100, high=104, low=100, close=100),   # 4%
        Bar(day=_d(2026, 1, 6), open=100, high=102, low=100, close=100),   # 2%
        Bar(day=_d(2026, 1, 7), open=100, high=106, low=100, close=100),   # 6%
    ]
    assert median_daily_range_pct(series, [_d(2026, 1, 5), _d(2026, 1, 7)]) == 4.0
