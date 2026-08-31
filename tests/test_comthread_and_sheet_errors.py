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


# ------------------------------------------------- Excel が起動していない場合

class _FakeApps:
    """xlwings の xw.apps を真似る。Excel 未起動なら空。"""

    def __init__(self, apps=(), *, raises=False) -> None:
        self._apps = list(apps)
        self._raises = raises

    def __iter__(self):
        if self._raises:
            raise RuntimeError("Couldn't find any active App!")
        return iter(self._apps)

    @property
    def active(self):
        return self._apps[0]


class _FakeBook:
    def __init__(self, fullname) -> None:
        self.fullname = fullname


class _FakeApp:
    def __init__(self, books=(), *, opened=None) -> None:
        self.books = _FakeBookCollection(books, opened=opened)


class _FakeBookCollection:
    def __init__(self, books=(), *, opened=None) -> None:
        self._books = list(books)
        self._opened = opened
        self.opened_path = None

    def __iter__(self):
        return iter(self._books)

    def open(self, path):
        self.opened_path = path
        return self._opened or _FakeBook(path)


class _FakeXw:
    """必要なぶんだけの xlwings スタブ。"""

    def __init__(self, apps, *, books_raises=False) -> None:
        self.apps = apps
        self.created_app = None
        self._books_raises = books_raises

    @property
    def books(self):
        if self._books_raises:
            raise RuntimeError("Couldn't find any active App!")
        return iter([])

    def App(self, visible=True, add_book=False):  # noqa: N802 - xlwings の API 名
        self.created_app = _FakeApp()
        return self.created_app


def _bridge_with_xw(xw, config) -> ExcelBridge:
    bridge = ExcelBridge.__new__(ExcelBridge)
    bridge._xw = xw
    bridge._config = config
    return bridge


def test_starts_excel_when_none_is_running(tmp_path, config_factory):
    """Excel が 1 つも起動していなくても、起動してブックを開く（例外で落ちない）。"""
    from dataclasses import replace

    from kabu.config import ExcelConfig

    workbook = tmp_path / "quotes.xlsx"
    workbook.write_text("", encoding="utf-8")
    config = config_factory()
    config = replace(config, excel=ExcelConfig(workbook=str(workbook)))

    xw = _FakeXw(_FakeApps(raises=True), books_raises=True)
    bridge = _bridge_with_xw(xw, config)

    bridge._open_book()

    assert xw.created_app is not None, "Excel 未起動なら新しく起動すること"
    assert xw.created_app.books.opened_path == str(workbook)


def test_reuses_a_book_already_open_in_a_background_excel(tmp_path, config_factory):
    """アクティブでない Excel で開かれているブックも見つけて使い回す。"""
    from dataclasses import replace

    from kabu.config import ExcelConfig

    workbook = tmp_path / "quotes.xlsx"
    workbook.write_text("", encoding="utf-8")
    config = config_factory()
    config = replace(config, excel=ExcelConfig(workbook=str(workbook)))

    already_open = _FakeBook(str(workbook))
    background = _FakeApp(books=[already_open])
    xw = _FakeXw(_FakeApps([background]))
    bridge = _bridge_with_xw(xw, config)

    assert bridge._open_book() is already_open
    assert xw.created_app is None, "既に開いているなら Excel を新たに起動しない"


# ------------------------------------- 自分で起動した Excel へのアドイン読込

class _FakeApi:
    def __init__(self, *, register_fails=False) -> None:
        self.registered: list[str] = []
        self.opened: list[str] = []
        self._register_fails = register_fails
        self.Workbooks = self

    def RegisterXLL(self, path):  # noqa: N802 - Excel の API 名
        if self._register_fails:
            raise RuntimeError("読み込めません")
        self.registered.append(path)

    def Open(self, path):  # noqa: N802 - Excel の API 名
        self.opened.append(path)


def _addin_bridge(monkeypatch, tmp_path, *, files, bitness="64"):
    from kabu import excelinfo, rss as rss_module

    folder = tmp_path / "rss"
    folder.mkdir()
    for name in files:
        (folder / name).write_text("", encoding="utf-8")
    monkeypatch.setattr(excelinfo, "addin_dir", lambda: folder)
    monkeypatch.setattr(
        excelinfo, "detect_excel", lambda: excelinfo.ExcelInfo(bitness, "テスト")
    )
    bridge = rss_module.ExcelBridge.__new__(rss_module.ExcelBridge)
    return bridge, folder


def test_loads_both_rss_addins_into_an_excel_we_started(monkeypatch, tmp_path):
    """COM で起動した Excel はアドインを自動で読まないので、明示的に入れる。"""
    bridge, folder = _addin_bridge(
        monkeypatch, tmp_path,
        files=["MarketSpeed2_RSS_64bit.xll", "MarketSpeed2_RSS_VBA.xlam"],
    )
    app = type("App", (), {"api": _FakeApi()})()

    bridge._load_rss_addins(app)

    assert app.api.registered == [str(folder / "MarketSpeed2_RSS_64bit.xll")]
    assert app.api.opened == [str(folder / "MarketSpeed2_RSS_VBA.xlam")]


def test_missing_addin_files_do_not_raise(monkeypatch, tmp_path):
    """アドインが見つからなくても落ちない（利用者が開いた Excel に繋ぐ道を残す）。"""
    bridge, _ = _addin_bridge(monkeypatch, tmp_path, files=[])
    app = type("App", (), {"api": _FakeApi()})()

    bridge._load_rss_addins(app)  # 例外が出ないこと

    assert app.api.registered == []


def test_addin_load_failure_does_not_raise(monkeypatch, tmp_path):
    bridge, folder = _addin_bridge(
        monkeypatch, tmp_path,
        files=["MarketSpeed2_RSS_64bit.xll", "MarketSpeed2_RSS_VBA.xlam"],
    )
    app = type("App", (), {"api": _FakeApi(register_fails=True)})()

    bridge._load_rss_addins(app)  # 例外が出ないこと

    assert app.api.opened == [str(folder / "MarketSpeed2_RSS_VBA.xlam")], "片方失敗でも続行する"
