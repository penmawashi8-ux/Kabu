from __future__ import annotations

from datetime import datetime, time

import pytest

from kabu.clock import MarketClock
from kabu.config import Config, MarketConfig, load_config
from kabu.errors import ConfigError
from kabu.rss import parse_status
from kabu.models import OrderStatus

MINIMAL = {
    "mode": "paper",
    "rules": [{"id": "a", "symbol": "7203.T", "quantity": 100, "buy_at": 2800, "sell_at": 3000}],
}


def test_minimal_config_loads():
    config = Config.from_dict(MINIMAL)
    assert config.mode == "paper"
    assert config.rules[0].symbol == "7203.T"


def test_sell_below_buy_is_rejected():
    raw = {**MINIMAL, "rules": [{**MINIMAL["rules"][0], "sell_at": 2700}]}
    with pytest.raises(ConfigError, match="無限ループ"):
        Config.from_dict(raw)


def test_stop_loss_above_entry_is_rejected():
    raw = {**MINIMAL, "rules": [{**MINIMAL["rules"][0], "stop_loss": 2900}]}
    with pytest.raises(ConfigError, match="損切り"):
        Config.from_dict(raw)


def test_duplicate_rule_id_is_rejected():
    raw = {**MINIMAL, "rules": [MINIMAL["rules"][0], MINIMAL["rules"][0]]}
    with pytest.raises(ConfigError, match="重複"):
        Config.from_dict(raw)


def test_negative_quantity_is_rejected():
    raw = {**MINIMAL, "rules": [{**MINIMAL["rules"][0], "quantity": -100}]}
    with pytest.raises(ConfigError):
        Config.from_dict(raw)


def test_unknown_mode_is_rejected():
    with pytest.raises(ConfigError):
        Config.from_dict({**MINIMAL, "mode": "yolo"})


def test_empty_rules_is_rejected():
    with pytest.raises(ConfigError, match="rules"):
        Config.from_dict({"mode": "paper", "rules": []})


def test_too_fast_polling_is_rejected():
    with pytest.raises(ConfigError, match="poll_interval_sec"):
        Config.from_dict({**MINIMAL, "poll_interval_sec": 0.2})


def test_shipped_example_config_is_valid(tmp_path):
    from pathlib import Path

    example = Path(__file__).resolve().parents[1] / "config.example.yaml"
    config = load_config(example)
    assert config.mode == "paper"
    assert len(config.rules) == 2


def clock(**overrides) -> MarketClock:
    base = dict(
        sessions=((time(9, 0), time(11, 30)), (time(12, 30), time(15, 30))),
        trade_cutoff=time(15, 20),
    )
    base.update(overrides)
    return MarketClock(MarketConfig(**base))  # type: ignore[arg-type]


def at(hour: int, minute: int = 0, day: int = 31) -> datetime:
    return datetime(2026, 8, day, hour, minute, tzinfo=clock().timezone)


def test_market_hours():
    c = clock()
    assert not c.is_open(at(8, 59))
    assert c.is_open(at(9, 0))
    assert c.is_open(at(11, 29))
    assert not c.is_open(at(11, 30))   # 前場引け
    assert not c.is_open(at(12, 0))    # 昼休み
    assert c.is_open(at(12, 30))
    assert c.is_open(at(15, 29))
    assert not c.is_open(at(15, 30))   # 大引け


def test_weekend_is_closed():
    c = clock()
    assert not c.is_open(at(10, 0, day=29))  # 2026-08-29 は土曜
    assert not c.is_open(at(10, 0, day=30))  # 日曜


def test_holiday_list_is_respected():
    c = MarketClock(MarketConfig(holidays=("2026-08-31",)))
    assert not c.is_open(at(10, 0))


def test_entry_cutoff():
    c = clock()
    assert c.accepts_new_entry(at(15, 19))
    assert not c.accepts_new_entry(at(15, 21))


def test_seconds_until_open_from_lunch_break():
    c = clock()
    assert c.seconds_until_open(at(12, 0)) == 30 * 60


def test_seconds_until_open_skips_weekend():
    c = clock()
    # 金曜の引け後 → 次は月曜の 9:00
    friday_evening = datetime(2026, 8, 28, 16, 0, tzinfo=c.timezone)
    hours = c.seconds_until_open(friday_evening) / 3600
    assert 60 < hours < 70


@pytest.mark.parametrize(
    "text,expected",
    [
        ("全約定", OrderStatus.FILLED),
        ("約定済", OrderStatus.FILLED),
        ("受付済", OrderStatus.PENDING),
        ("取消中", OrderStatus.PENDING),   # 取消中はまだ生きている
        ("取消済", OrderStatus.CANCELLED),
        ("失効", OrderStatus.CANCELLED),
        ("エラー", OrderStatus.REJECTED),
        ("", OrderStatus.UNKNOWN),
    ],
)
def test_order_status_parsing(text, expected):
    assert parse_status(text) is expected


# ------------------------------------------------------- NISA 口座の取り違え

ROTATING_RULE = {**MINIMAL["rules"][0], "max_cycles": 3, "stop_loss": 2700}
HOLDING_RULE = {**MINIMAL["rules"][0], "max_cycles": 1}


def config_with_account(account: str, rule: dict | None = None) -> Config:
    return Config.from_dict({
        **MINIMAL,
        "mode": "live",
        "rules": [rule or ROTATING_RULE],
        "rss": {"order": {"defaults": {"account_type": account}}},
    })


def test_nisa_account_is_warned_about():
    """NISA 枠での回転売買は不利なので、実弾に進む前に気づかせる。"""
    from kabu.cli import _nisa_warning

    warning = _nisa_warning(config_with_account("NISA"))
    assert "損益通算" in warning
    assert "特定口座" in warning


def test_nisa_warning_names_the_offending_rules():
    """どのルールを直せばいいのかが分からないと、警告は読み飛ばされる。"""
    from kabu.cli import _nisa_warning

    assert "a" in _nisa_warning(config_with_account("NISA"))


def test_nisa_warning_is_case_insensitive():
    from kabu.cli import _nisa_warning

    assert _nisa_warning(config_with_account("nisa成長投資枠"))


def test_specific_account_is_not_warned_about():
    from kabu.cli import _nisa_warning

    assert _nisa_warning(config_with_account("特定")) == ""
    assert _nisa_warning(Config.from_dict(MINIMAL)) == ""


# 「買って持ち続けるだけ」は NISA の本来の使い道なので警告しない。
# ここで警告を出すと「NISA では何もするな」に読めてしまい、本当に危ない
# 往復売買のときの警告まで信用されなくなる。

def test_buy_and_hold_in_nisa_is_not_warned_about():
    from kabu.cli import _nisa_warning

    assert _nisa_warning(config_with_account("NISA", HOLDING_RULE)) == ""


def test_stop_loss_alone_is_enough_to_warn():
    """損切りするなら往復しなくても損益通算の不利は効く。"""
    from kabu.cli import _nisa_warning

    rule = {**MINIMAL["rules"][0], "max_cycles": 1, "stop_loss": 2700}
    assert _nisa_warning(config_with_account("NISA", rule))


def test_disabled_rotating_rule_is_not_warned_about():
    """無効なルールは発注しないので、警告の理由にならない。"""
    from kabu.cli import _nisa_warning

    rule = {**ROTATING_RULE, "enabled": False}
    assert _nisa_warning(config_with_account("NISA", rule)) == ""


def test_shipped_core_config_holds_instead_of_rotating():
    """config.core.yaml は「買って持つ」枠。往復するルールが混ざったら気づきたい。"""
    from pathlib import Path

    from kabu.cli import _rotating_rules
    from kabu.config import load_config

    path = Path(__file__).resolve().parent.parent / "config.core.yaml"
    config = load_config(path)
    assert _rotating_rules(config) == []
    assert all(r.max_cycles == 1 and r.stop_loss is None for r in config.rules)


# ------------------------------------------------- -c をどこに書いても効くこと
#
# README は `kabu play -c config.simulate.yaml` の形で書いてある。以前は
# トップレベルにしか -c が無く、この形だと "unrecognized arguments" で落ちていた。

def test_config_flag_after_subcommand():
    from kabu.cli import build_parser

    args = build_parser().parse_args(["play", "-c", "config.core.yaml"])
    assert args.config == "config.core.yaml"


def test_config_flag_before_subcommand():
    from kabu.cli import build_parser

    args = build_parser().parse_args(["-c", "config.core.yaml", "doctor"])
    assert args.config == "config.core.yaml"


def test_subcommand_does_not_clobber_the_earlier_config_flag():
    """サブコマンド側の既定値が、前に書いた指定を上書きしないこと。"""
    from kabu.cli import build_parser

    args = build_parser().parse_args(["-c", "config.core.yaml", "play"])
    assert args.config == "config.core.yaml"


def test_config_defaults_to_config_yaml():
    from kabu.cli import build_parser

    assert build_parser().parse_args(["doctor"]).config == "config.yaml"


def test_verbose_works_on_either_side():
    from kabu.cli import build_parser

    assert build_parser().parse_args(["-v", "doctor"]).verbose is True
    assert build_parser().parse_args(["doctor", "-v"]).verbose is True
    assert build_parser().parse_args(["doctor"]).verbose is False
