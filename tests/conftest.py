from __future__ import annotations

from datetime import time
from typing import Any

import pytest

from kabu.config import Config, ExcelConfig, MarketConfig, RiskConfig, RssConfig, Rule
from kabu.models import OrderType


def make_rule(**overrides: Any) -> Rule:
    base = dict(
        id="r1",
        symbol="7203.T",
        quantity=100,
        buy_at=2800.0,
        sell_at=3000.0,
        stop_loss=None,
        order_type=OrderType.LIMIT,
        max_cycles=1,
        enabled=True,
    )
    base.update(overrides)
    return Rule(**base)  # type: ignore[arg-type]


def make_config(tmp_path, rules=None, **overrides: Any) -> Config:
    base = dict(
        mode="paper",
        poll_interval_sec=1.0,
        excel=ExcelConfig(),
        rss=RssConfig(),
        risk=RiskConfig(min_seconds_between_orders=0),
        market=MarketConfig(
            sessions=((time(9, 0), time(11, 30)), (time(12, 30), time(15, 30))),
            trade_cutoff=time(15, 20),
        ),
        rules=tuple(rules or [make_rule()]),
        state_file=str(tmp_path / "state.json"),
        journal_file=str(tmp_path / "journal.csv"),
        log_file=str(tmp_path / "kabu.log"),
    )
    base.update(overrides)
    return Config(**base)  # type: ignore[arg-type]


@pytest.fixture
def config_factory(tmp_path):
    def _factory(rules=None, **overrides: Any) -> Config:
        return make_config(tmp_path, rules, **overrides)

    return _factory
