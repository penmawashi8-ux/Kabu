"""過去データでのバックテストの検査。

ネットワークには触らず、四本値を手で作って売買ロジックを回す。
"""

from __future__ import annotations

from datetime import date

from conftest import make_rule

from kabu.backtest import run_backtest, select_days
from kabu.marketdata import Bar


def _bar(day, open_, high, low, close) -> Bar:
    return Bar(day=day, open=open_, high=high, low=low, close=close)


def test_buy_and_sell_in_one_day(config_factory):
    """買いラインを割って、同じ日に売りラインまで戻れば 1 往復する。"""
    rule = make_rule(id="toyota", symbol="7203.T", quantity=100,
                     buy_at=2800.0, sell_at=3000.0, max_cycles=3)
    bars = {"7203.T": [_bar(date(2026, 8, 28), 2900, 3050, 2750, 3010)]}

    result = run_backtest(config_factory(rules=[rule]), bars)

    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.entry_price <= 2800, "買いは指値以下で約定する"
    assert trade.exit_price >= 3000, "売りは指値以上で約定する"
    assert trade.profit > 0


def test_no_trade_when_the_price_never_reaches_the_line(config_factory):
    rule = make_rule(id="toyota", symbol="7203.T", buy_at=2000.0, sell_at=2200.0)
    bars = {"7203.T": [_bar(date(2026, 8, 28), 2900, 3050, 2850, 3010)]}

    result = run_backtest(config_factory(rules=[rule]), bars)

    assert result.trades == []
    assert result.fills == 0


def test_stop_loss_closes_the_position_at_a_loss(config_factory):
    """損切りラインを割ったら成行で撤退し、損として記録される。"""
    rule = make_rule(id="sony", symbol="6758.T", quantity=100,
                     buy_at=3000.0, sell_at=3400.0, stop_loss=2900.0)
    # 寄りで買い、その後 損切りラインを割って引ける（寄り 2950 → 安値 2800）。
    bars = {"6758.T": [_bar(date(2026, 8, 28), 2950, 3000, 2800, 2820)]}

    result = run_backtest(config_factory(rules=[rule]), bars)

    assert len(result.trades) == 1
    assert result.trades[0].profit < 0
    assert result.realized < 0


def test_position_left_open_is_reported_separately(config_factory):
    """買っただけで売れずに終わった場合は、未決済として残る。"""
    rule = make_rule(id="toyota", symbol="7203.T", quantity=100,
                     buy_at=2800.0, sell_at=5000.0)
    bars = {"7203.T": [_bar(date(2026, 8, 28), 2900, 2950, 2700, 2750)]}

    result = run_backtest(config_factory(rules=[rule]), bars)

    assert result.trades == []
    assert len(result.open_positions) == 1
    assert result.open_positions[0].symbol == "7203.T"


def test_multiple_symbols_run_together(config_factory):
    rules = [
        make_rule(id="toyota", symbol="7203.T", buy_at=2800.0, sell_at=3000.0),
        make_rule(id="sony", symbol="6758.T", quantity=50,
                  buy_at=3000.0, sell_at=3400.0),
    ]
    bars = {
        "7203.T": [_bar(date(2026, 8, 28), 2900, 3050, 2750, 3010)],
        "6758.T": [_bar(date(2026, 8, 28), 3100, 3450, 2950, 3400)],
    }

    result = run_backtest(config_factory(rules=rules), bars)

    assert {t.symbol for t in result.trades} == {"7203.T", "6758.T"}


def test_select_days_defaults_to_the_latest_day():
    bars = {"7203.T": [
        _bar(date(2026, 8, 26), 1, 1, 1, 1),
        _bar(date(2026, 8, 27), 1, 1, 1, 1),
        _bar(date(2026, 8, 28), 1, 1, 1, 1),
    ]}
    assert select_days(bars, days=1) == [date(2026, 8, 28)]
    assert select_days(bars, days=2) == [date(2026, 8, 27), date(2026, 8, 28)]
    assert select_days(bars, days=None, since=date(2026, 8, 27)) == [
        date(2026, 8, 27), date(2026, 8, 28)
    ]


def test_backtest_leaves_no_state_file_behind(config_factory, tmp_path):
    """バックテストは本番の state.json / journal.csv を汚さない。"""
    rule = make_rule(id="toyota", symbol="7203.T", buy_at=2800.0, sell_at=3000.0)
    config = config_factory(rules=[rule])
    bars = {"7203.T": [_bar(date(2026, 8, 28), 2900, 3050, 2750, 3010)]}

    run_backtest(config, bars)

    assert not (tmp_path / "state.json").exists()
    assert not (tmp_path / "journal.csv").exists()


def test_order_size_cap_is_still_enforced(config_factory):
    """再生用に緩めるのは時間軸の弁だけ。発注額の上限は本番と同じに効かせる。"""
    rule = make_rule(id="sony", symbol="6758.T", quantity=100,
                     buy_at=3400.0, sell_at=3800.0)   # 100 株 × 3,400 円 = 34 万円
    bars = {"6758.T": [_bar(date(2026, 8, 28), 3500, 3900, 3350, 3850)]}

    result = run_backtest(config_factory(rules=[rule]), bars)

    assert result.trades == [], "1 回あたり 30 万円の上限を超えるので発注されない"
    assert any("上限" in str(e["note"]) for e in result.events if e["event"] == "BLOCKED")


def test_stop_loss_exit_is_labelled_as_such(config_factory):
    """要約で「損切りで切れた」のか「売りラインに届いた」のかを見分けられること。"""
    rule = make_rule(id="sony", symbol="6758.T", quantity=100,
                     buy_at=3000.0, sell_at=3400.0, stop_loss=2900.0)
    bars = {"6758.T": [_bar(date(2026, 8, 28), 2950, 3000, 2800, 2820)]}

    result = run_backtest(config_factory(rules=[rule]), bars)

    assert "損切り" in result.trades[0].exit_reason


def test_target_exit_is_not_labelled_as_a_stop_loss(config_factory):
    rule = make_rule(id="toyota", symbol="7203.T", quantity=100,
                     buy_at=2800.0, sell_at=3000.0, stop_loss=2700.0)
    bars = {"7203.T": [_bar(date(2026, 8, 28), 2900, 3050, 2750, 3010)]}

    result = run_backtest(config_factory(rules=[rule]), bars)

    assert "損切り" not in result.trades[0].exit_reason
