"""`kabu play` の画面から config.yaml へルールを書き戻す機能の検査。

rules: 以外のキー・コメントには触れないこと、既定値と同じ項目は書かないことを確認する。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kabu.config import Config, Rule, load_config, save_rules
from kabu.errors import ConfigError

SAMPLE = """\
# 先頭のコメント
mode: paper  # 行末コメント

excel:
  workbook: C:/kabu/quotes.xlsx  # ここも保存で変わってはいけない

rules:
  - id: old
    symbol: "9999.T"
    quantity: 100
    buy_at: 1000
    sell_at: 1100
"""


def _write(tmp_path: Path, text: str = SAMPLE) -> Path:
    p = tmp_path / "config.yaml"
    p.write_text(text, encoding="utf-8")
    return p


def test_save_rules_updates_only_the_rules_block(tmp_path):
    path = _write(tmp_path)
    new_rule = Rule(id="toyota", symbol="7203.T", quantity=200, buy_at=2800, sell_at=3000)

    save_rules(path, [new_rule])

    text = path.read_text(encoding="utf-8")
    assert "先頭のコメント" in text
    assert "行末コメント" in text
    assert "ここも保存で変わってはいけない" in text
    assert "C:/kabu/quotes.xlsx" in text
    assert "old" not in text
    assert "toyota" in text
    assert "7203.T" in text

    # 書き戻した内容がそのまま読めること（実際の読み込み経路との整合性）。
    config = load_config(path)
    assert [r.id for r in config.rules] == ["toyota"]
    assert config.rules[0].quantity == 200


def test_save_rules_omits_fields_equal_to_default(tmp_path):
    path = _write(tmp_path)
    plain = Rule(id="a", symbol="7203.T", quantity=100, buy_at=2800, sell_at=3000)

    save_rules(path, [plain])

    text = path.read_text(encoding="utf-8")
    assert "stop_loss" not in text
    assert "max_cycles" not in text
    assert "order_type" not in text
    assert "enabled" not in text


def test_save_rules_keeps_fields_that_differ_from_default(tmp_path):
    path = _write(tmp_path)
    custom = Rule(
        id="a", symbol="7203.T", quantity=100, buy_at=2800, sell_at=3000,
        stop_loss=2700, max_cycles=3, enabled=False,
    )

    save_rules(path, [custom])

    config = load_config(path)
    assert config.rules[0].stop_loss == 2700
    assert config.rules[0].max_cycles == 3
    assert config.rules[0].enabled is False


def test_save_rules_multiple_rules_round_trip(tmp_path):
    path = _write(tmp_path)
    rules = [
        Rule(id="a", symbol="7203.T", quantity=100, buy_at=2800, sell_at=3000),
        Rule(id="b", symbol="9984.T", quantity=100, buy_at=7000, sell_at=7500, stop_loss=6800),
    ]

    save_rules(path, rules)

    config = load_config(path)
    assert [r.id for r in config.rules] == ["a", "b"]


def test_save_rules_missing_file_is_rejected(tmp_path):
    with pytest.raises(ConfigError, match="見つかりません"):
        save_rules(tmp_path / "does-not-exist.yaml", [
            Rule(id="a", symbol="7203.T", quantity=100, buy_at=2800, sell_at=3000)
        ])


def test_save_rules_rejects_non_mapping_file(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("- just\n- a\n- list\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="マッピング"):
        save_rules(path, [Rule(id="a", symbol="7203.T", quantity=100, buy_at=2800, sell_at=3000)])
