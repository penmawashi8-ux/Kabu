"""調べる銘柄の一覧の検査。"""

from __future__ import annotations

import pytest

from kabu.universe import LARGE, UNIVERSES, from_file, name_of, symbols


def test_major_matches_the_dashboard_catalog():
    assert len(symbols("major")) == 16
    assert all(s.endswith(".T") for s in symbols("major"))


def test_large_is_actually_large():
    assert len(symbols("large")) > 100
    assert len(set(symbols("large"))) == len(symbols("large")), "重複しないこと"


def test_every_code_is_four_digits():
    assert all(code.isdigit() and len(code) == 4 for code in LARGE)


def test_unknown_universe_is_rejected():
    with pytest.raises(ValueError, match="知らない銘柄リスト"):
        symbols("topix9999")


def test_universes_are_all_usable():
    for name in UNIVERSES:
        assert symbols(name)


def test_file_accepts_the_shapes_people_actually_paste(tmp_path):
    """証券会社の一覧をコピペしてもそのまま読めること。"""
    path = tmp_path / "list.txt"
    path.write_text(
        "# 自分用のリスト\n"
        "7203\n"
        "6758.T\n"
        "9432,日本電信電話\n"
        "8306 三菱UFJ\n"
        "\n"
        "7203\n",             # 重複
        encoding="utf-8",
    )
    assert from_file(path) == ["7203.T", "6758.T", "9432.T", "8306.T"]


def test_name_lookup():
    assert name_of("7203.T") == "トヨタ自動車"
    assert name_of("9999.T") == ""
