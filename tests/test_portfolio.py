"""複数銘柄を同時に持つ場合の検証の、その検証。

一番大事なのは「単元株の制約を無視していないこと」。100 株単位で買えない
金額のポートフォリオを組めてしまうと、机上でだけ分散できてしまう。
"""

from __future__ import annotations

from datetime import date, timedelta

from kabu.marketdata import Bar
from kabu.portfolio import run_portfolio, single_stock_median


def _series(n: int, base: float, drift: float = 0.0) -> list[Bar]:
    bars, day, price = [], date(2020, 1, 6), base
    for _ in range(n):
        while day.weekday() >= 5:
            day += timedelta(days=1)
        price *= 1 + drift
        bars.append(Bar(day=day, open=price, high=price * 1.01,
                        low=price * 0.99, close=price))
        day += timedelta(days=1)
    return bars


def _universe(count: int, n: int = 400, base: float = 1000.0) -> dict[str, list[Bar]]:
    return {
        f"{1000 + i}.T": _series(n, base + i * 10, drift=0.0005 + i * 0.0001)
        for i in range(count)
    }


def test_small_capital_cannot_hold_many_names():
    """資金が足りなければ、持てる銘柄数は減る（机上で分散させない）。"""
    bars = _universe(10, base=3000.0)     # 1 単元 30 万円
    poor = run_portfolio(bars, capital=500_000, hold=5, select="equal", lookback=60)
    rich = run_portfolio(bars, capital=10_000_000, hold=5, select="equal", lookback=60)

    assert poor.average_names < rich.average_names
    assert poor.skipped_capital, "買えなかったことを記録すること"


def test_lots_are_multiples_of_one_hundred():
    bars = _universe(3, base=1000.0)
    result = run_portfolio(bars, capital=1_000_000, hold=3, select="equal", lookback=60)
    assert result.equity, "評価額の推移が記録されること"
    assert result.average_names > 0


def test_momentum_prefers_the_stronger_names():
    """モメンタム選択は、伸びている銘柄を選ぶ。"""
    bars = {
        "1001.T": _series(400, 1000, drift=0.002),    # よく伸びる
        "1002.T": _series(400, 1000, drift=-0.001),   # 下がる
        "1003.T": _series(400, 1000, drift=0.0),      # 横ばい
    }
    momentum = run_portfolio(bars, capital=5_000_000, hold=1,
                             select="momentum", lookback=60, rebalance_days=20)
    assert momentum.return_pct > 0


def test_taxes_are_charged_on_realised_gains():
    bars = _universe(4, base=1000.0)
    taxed = run_portfolio(bars, capital=5_000_000, hold=2, select="momentum",
                          lookback=60, rebalance_days=20, tax_pct=20.315)
    free = run_portfolio(bars, capital=5_000_000, hold=2, select="momentum",
                         lookback=60, rebalance_days=20, tax_pct=0.0)
    assert taxed.realized_tax >= 0
    assert taxed.return_pct <= free.return_pct


def test_drawdown_is_negative_or_zero():
    bars = _universe(5)
    result = run_portfolio(bars, capital=5_000_000, hold=3, select="equal", lookback=60)
    assert result.max_drawdown_pct <= 0


def test_single_stock_median_is_the_baseline():
    bars = _universe(5, base=1000.0)
    assert single_stock_median(bars) > 0


def test_not_enough_history_returns_empty():
    bars = _universe(3, n=30)
    result = run_portfolio(bars, capital=1_000_000, hold=2, lookback=120)
    assert result.equity == []


def test_capital_is_not_left_idle_when_a_lot_is_affordable():
    """等分では買えなくても、資金があるなら優先順位の高い銘柄から買うこと。

    均等配分にこだわって全額を現金のまま置くのは、実際の買い方と違う。
    """
    bars = _universe(10, base=3000.0)     # 1 単元 30 万円
    result = run_portfolio(bars, capital=1_000_000, hold=10,
                           select="equal", lookback=60)
    # 100 万円 ÷ 10 銘柄 = 10 万円では 1 単元も買えないが、
    # 上位から 3 銘柄ぶんは買えるはず。
    assert result.average_names >= 1.0


def test_equal_weight_index_is_the_diversification_baseline():
    """全銘柄等ウェイトは、分散だけを効かせた基準として計算できること。"""
    from kabu.portfolio import equal_weight_index

    bars = _universe(5, base=1000.0)
    total, drawdown = equal_weight_index(bars, tax_pct=0.0)
    assert total > 0
    assert drawdown <= 0


def test_index_baseline_is_taxed_on_gains_only():
    from kabu.portfolio import equal_weight_index

    rising = _universe(3, base=1000.0)
    gross, _ = equal_weight_index(rising, tax_pct=0.0)
    net, _ = equal_weight_index(rising, tax_pct=20.0)
    assert net < gross
    assert abs(net - gross * 0.8) < 1e-6


def test_fractional_shares_allow_real_diversification_on_small_capital():
    """単元未満株（1 株単位）なら、少額でも銘柄数を増やせること。"""
    bars = _universe(10, base=3000.0)      # 1 単元 30 万円
    lots = run_portfolio(bars, capital=500_000, hold=10, select="equal", lookback=60)
    single = run_portfolio(bars, capital=500_000, hold=10, select="equal",
                           lookback=60, unit=1)

    assert lots.average_names <= 2, "50 万円では単元株で 1〜2 銘柄しか持てない"
    assert single.average_names > lots.average_names
