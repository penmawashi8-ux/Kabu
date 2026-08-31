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


# --------------------------------------------- どの Excel に繋がったかの診断

class _RecordingSheet:
    """書き込みを受け付けるだけのシート（読み返しは別途差し替える）。"""

    def range(self, start, end=None):
        class _R:
            value = None
            formula = None
        return _R()


def _bridge_on(config, sheet):
    bridge = ExcelBridge.__new__(ExcelBridge)
    bridge._config = config
    bridge._book = type("Book", (), {
        "sheets": {"QUOTES": sheet},
        "fullname": r"C:\work\Kabu\quotes.xlsx",
        "app": type("App", (), {"pid": 1234})(),
    })()
    return bridge


def test_describe_names_the_excel_and_workbook(config_factory):
    """繋いだ先が分かること（別の Excel を掴んでいる事故を見つけるため）。"""
    described = _bridge_on(config_factory(), _RecordingSheet()).describe()
    assert "1234" in described
    assert "quotes.xlsx" in described


def test_build_quote_sheet_warns_when_the_write_does_not_stick(config_factory, caplog, monkeypatch):
    """書いた銘柄が読み返せないときは警告する（別のブックに書いていた場合）。"""
    bridge = _bridge_on(config_factory(), _RecordingSheet())
    # 1 回目（書く前の確認）は空、2 回目（書いた後の読み返し）は 1 銘柄だけ返す。
    answers = iter([[], ["6758.T"]])
    monkeypatch.setattr(ExcelBridge, "_existing_symbols",
                        staticmethod(lambda sheet, count: next(answers)))

    with caplog.at_level("WARNING"):
        bridge.build_quote_sheet(["6758.T", "7203.T"])

    assert any("読み返せません" in r.getMessage() for r in caplog.records)


def test_build_quote_sheet_is_quiet_when_the_write_sticks(config_factory, caplog, monkeypatch):
    bridge = _bridge_on(config_factory(), _RecordingSheet())
    answers = iter([[], ["6758.T", "7203.T"]])
    monkeypatch.setattr(ExcelBridge, "_existing_symbols",
                        staticmethod(lambda sheet, count: next(answers)))

    with caplog.at_level("WARNING"):
        bridge.build_quote_sheet(["6758.T", "7203.T"])

    assert not [r for r in caplog.records if "読み返せません" in r.getMessage()]
