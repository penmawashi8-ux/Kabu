"""QUOTES シートの読み取りの検査。

Excel に値が見えているのに kabu 側が「1 件も取れない」と言う事故を防ぐ。
RSS の版やセルの書式によって、現在値は数値でも文字列でも返ってくる。
"""

from __future__ import annotations

import pytest

from kabu.errors import QuoteError
from kabu.rss import ExcelBridge


class _FakeRange:
    def __init__(self, value) -> None:
        self.value = value


class _FakeSheet:
    def __init__(self, value) -> None:
        self._value = value

    def range(self, start, end=None):
        return _FakeRange(self._value)


def _bridge_reading(value, config) -> ExcelBridge:
    bridge = ExcelBridge.__new__(ExcelBridge)
    bridge._config = config
    bridge._book = type("Book", (), {"sheets": {"QUOTES": _FakeSheet(value)}})()
    return bridge


def test_reads_numeric_prices(config_factory):
    bridge = _bridge_reading([["7203.T", 2980.0], ["6758.T", 12500.0]], config_factory())
    assert bridge.read_quotes(["7203.T", "6758.T"]) == {"7203.T": 2980.0, "6758.T": 12500.0}


def test_reads_prices_that_arrive_as_text(config_factory):
    """RSS が文字列で返しても取りこぼさない（桁区切りつきも含む）。"""
    bridge = _bridge_reading([["7203.T", "2,980"], ["6758.T", " 12500.5 "]], config_factory())
    assert bridge.read_quotes(["7203.T", "6758.T"]) == {"7203.T": 2980.0, "6758.T": 12500.5}


def test_single_symbol_comes_back_flat(config_factory):
    bridge = _bridge_reading(["7203.T", 2980.0], config_factory())
    assert bridge.read_quotes(["7203.T"]) == {"7203.T": 2980.0}


def test_blank_and_error_cells_are_skipped(config_factory):
    bridge = _bridge_reading([["7203.T", None], ["6758.T", 12500.0]], config_factory())
    assert bridge.read_quotes(["7203.T", "6758.T"]) == {"6758.T": 12500.0}


def test_error_message_shows_what_was_actually_read(config_factory):
    """原因の切り分けができるよう、読めたセルの中身をそのまま出す。"""
    bridge = _bridge_reading([["7203.T", "#NAME?"], ["6758.T", None]], config_factory())
    with pytest.raises(QuoteError) as exc:
        bridge.read_quotes(["7203.T", "6758.T"])
    message = str(exc.value)
    assert "#NAME?" in message
    assert "7203.T" in message
