"""別スレッドから Excel を触るときの下ごしらえと、その失敗の伝え方の検査。

`kabu play --source rss` は売買ループを別スレッドで回すため、そのスレッドで
COM を初期化しないと Excel 操作が「CoInitialize は呼び出されていません」で
毎周回落ちる。さらにその失敗が「シートが無い」と誤診されると原因に辿り着けない。
"""

from __future__ import annotations

import threading

import pytest

from kabu.comthread import init_com, release_com
from kabu.errors import BrokerError
from kabu.rss import ExcelBridge


def test_init_com_is_a_noop_without_pywin32():
    """Windows 以外では何もせずに None を返す（例外を投げない）。"""
    handle = init_com()
    release_com(handle)  # 解放も同様に無害であること


def test_init_com_can_be_called_from_a_worker_thread():
    result: list[object] = []

    def worker() -> None:
        handle = init_com()
        result.append(handle)
        release_com(handle)

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join()
    assert len(result) == 1


class _Sheets:
    """xlwings の sheets を真似た最小限のスタブ。"""

    def __init__(self, names, *, listable=True) -> None:
        self._names = names
        self._listable = listable

    def __getitem__(self, name):
        raise KeyError(name)

    def __iter__(self):
        if not self._listable:
            raise RuntimeError("CoInitialize は呼び出されていません。")
        return iter([type("S", (), {"name": n})() for n in self._names])


def _bridge_with(sheets) -> ExcelBridge:
    bridge = ExcelBridge.__new__(ExcelBridge)  # __init__ は Excel を掴むので通さない
    bridge._book = type("Book", (), {"sheets": sheets})()
    return bridge


def test_missing_sheet_reports_the_sheets_that_do_exist():
    bridge = _bridge_with(_Sheets(["QUOTES", "ORDERS"]))
    with pytest.raises(BrokerError, match="ワークブックにありません") as exc:
        bridge._sheet("MISSING")
    assert "QUOTES" in str(exc.value)


def test_com_failure_is_not_reported_as_a_missing_sheet():
    """通信自体が死んでいるときに「シートが無い」と言わない（誤診で原因を隠さない）。"""
    bridge = _bridge_with(_Sheets(["QUOTES"], listable=False))
    with pytest.raises(BrokerError, match="Excel に問い合わせできませんでした") as exc:
        bridge._sheet("QUOTES")
    assert "CoInitialize" in str(exc.value)
