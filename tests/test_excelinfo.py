"""Excel のビット数判定。

登録するアドインを間違えると RSS が読み込めないので、判定ロジックだけは
Windows が無くても検査できるように純関数へ切り出してある。
"""

from __future__ import annotations

import pytest

from kabu.excelinfo import (
    ExcelInfo,
    addin_dir,
    bitness_from_path,
    bitness_from_platform_value,
    describe_addin_setup,
)


@pytest.mark.parametrize(
    "value,expected",
    [
        ("x64", "64"),
        ("X64", "64"),
        (" x86 ", "32"),
        ("amd64", "64"),
        ("32ビット", "32"),
        ("64ビット", "64"),
        ("", None),
        (None, None),
        ("よく分からない値", None),
    ],
)
def test_platform_value_is_normalised(value, expected):
    assert bitness_from_platform_value(value) == expected


@pytest.mark.parametrize(
    "path,expected",
    [
        (r"C:\Program Files (x86)\Microsoft Office\root\Office16\EXCEL.EXE", "32"),
        (r"C:\Program Files\Microsoft Office\root\Office16\EXCEL.EXE", "64"),
        # 大文字小文字とスラッシュの揺れを吸収する
        (r"c:/PROGRAM FILES (X86)/Microsoft Office/EXCEL.EXE", "32"),
        (r"D:\Apps\Office\EXCEL.EXE", None),
        ("", None),
        (None, None),
    ],
)
def test_bitness_is_read_from_the_executable_path(path, expected):
    assert bitness_from_path(path) == expected


def test_program_files_x86_wins_over_program_files():
    """"Program Files (x86)" は "Program Files" も含む文字列なので順序が重要。"""
    assert bitness_from_path(r"C:\Program Files (x86)\Office\EXCEL.EXE") == "32"


def test_xll_name_follows_the_bitness():
    assert ExcelInfo("64", "test").xll_name == "MarketSpeed2_RSS_64bit.xll"
    assert ExcelInfo("32", "test").xll_name == "MarketSpeed2_RSS_32bit.xll"
    assert ExcelInfo(None, "test").xll_name is None


def test_addin_dir_points_at_the_marketspeed_folder():
    assert addin_dir().parts[-3:] == ("MarketSpeed2", "Bin", "rss")


def test_guidance_names_both_files_when_the_bitness_is_known(monkeypatch):
    from kabu import excelinfo

    monkeypatch.setattr(excelinfo, "detect_excel", lambda: ExcelInfo("64", "テスト"))
    text = "\n".join(describe_addin_setup())
    assert "64 ビット" in text
    assert "MarketSpeed2_RSS_64bit.xll" in text
    assert "MarketSpeed2_RSS_VBA.xlam" in text      # 2 つ目を忘れさせない


def test_guidance_falls_back_to_manual_steps(monkeypatch):
    """判定できない環境（Mac など）でも、手で確かめる方法を案内する。"""
    from kabu import excelinfo

    monkeypatch.setattr(excelinfo, "detect_excel", lambda: ExcelInfo(None, "テスト"))
    text = "\n".join(describe_addin_setup())
    assert "バージョン情報" in text
    assert "Alt" in text
    assert "Windows のビット数ではなく" in text
