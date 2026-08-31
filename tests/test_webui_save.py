"""`kabu play` の画面（GameSession）から config.yaml への保存を検査する。

HTTP サーバは起動せず、GameSession を直接操作する
（サーバ層は薄いディスパッチだけなので、ロジックはここでカバーする）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kabu.config import load_config
from kabu.errors import ConfigError
from kabu.webui import GameSession


def _config_yaml(tmp_path: Path) -> Path:
    p = tmp_path / "config.yaml"
    p.write_text(
        "mode: paper\n"
        "excel:\n"
        "  workbook: C:/kabu/quotes.xlsx  # 保存で消えてはいけない\n"
        "rules:\n"
        "  - id: seed\n"
        "    symbol: 7203.T\n"
        "    quantity: 100\n"
        "    buy_at: 2800\n"
        "    sell_at: 3000\n",
        encoding="utf-8",
    )
    return p


def test_save_to_file_without_config_path_is_rejected(config_factory):
    session = GameSession(config_factory())
    with pytest.raises(ConfigError, match="分かりません"):
        session.save_to_file()


def test_save_to_file_writes_applied_rules(tmp_path, config_factory):
    config_path = _config_yaml(tmp_path)
    session = GameSession(config_factory(), config_path=config_path)

    session.apply_rules([
        {"id": "toyota", "symbol": "7203.T", "quantity": 300, "buy_at": 2800, "sell_at": 3000},
        {"id": "sony", "symbol": "6758.T", "quantity": 100, "buy_at": 12000, "sell_at": 13000,
         "stop_loss": 11500},
    ])
    session.save_to_file()

    text = config_path.read_text(encoding="utf-8")
    assert "保存で消えてはいけない" in text
    assert "C:/kabu/quotes.xlsx" in text
    assert "seed" not in text

    reloaded = load_config(config_path)
    assert [r.id for r in reloaded.rules] == ["toyota", "sony"]
    assert reloaded.rules[1].stop_loss == 11500


def test_snapshot_reports_config_path(tmp_path, config_factory):
    config_path = _config_yaml(tmp_path)
    session = GameSession(config_factory(), config_path=config_path)
    assert session.snapshot()["config_path"] == str(config_path)


def test_snapshot_config_path_is_none_when_not_given(config_factory):
    session = GameSession(config_factory())
    assert session.snapshot()["config_path"] is None
