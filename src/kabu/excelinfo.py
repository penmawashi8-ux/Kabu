"""Excel のビット数と RSS アドインの場所を調べる。

RSS のアドインは 32bit 版と 64bit 版があり、**Excel** のビット数に合うほうを
登録しないと読み込めない。ここで一番間違えやすいのは、64bit の Windows でも
Excel は 32bit ということが普通にある点。Windows のビット数を見ても答えにならない。

判定できる材料を上から順に試し、どれも駄目なら手で確認する手順を案内する。
Windows 以外では何も分からないので None を返す。
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

ADDIN_SUBPATH = Path("MarketSpeed2") / "Bin" / "rss"
VBA_ADDIN = "MarketSpeed2_RSS_VBA.xlam"


@dataclass(frozen=True)
class ExcelInfo:
    bitness: str | None          # "32" / "64" / None（判定できず）
    source: str                  # どうやって判定したか（利用者への説明用）

    @property
    def xll_name(self) -> str | None:
        """登録すべき .xll のファイル名。"""
        return f"MarketSpeed2_RSS_{self.bitness}bit.xll" if self.bitness else None


def bitness_from_platform_value(value: str | None) -> str | None:
    """レジストリの Platform / Bitness 値（x86 / x64）を 32 / 64 に直す。"""
    if not value:
        return None
    normalized = value.strip().lower()
    if normalized in ("x64", "amd64", "64", "64-bit", "64ビット"):
        return "64"
    if normalized in ("x86", "32", "32-bit", "32ビット"):
        return "32"
    return None


def bitness_from_path(path: str | None) -> str | None:
    """EXCEL.EXE の場所から推定する。

    64bit Windows では 32bit のアプリが "Program Files (x86)" に入る。
    そこに居れば 32bit、素の "Program Files" に居れば 64bit。
    """
    if not path:
        return None
    lowered = str(path).replace("/", "\\").lower()
    if "program files (x86)" in lowered:
        return "32"
    if "program files" in lowered:
        return "64"
    return None


def _registry_value(root: str, key: str, name: str) -> str | None:
    """Windows のレジストリを 1 つ読む。読めなければ None。"""
    if sys.platform != "win32":
        return None
    try:
        import winreg  # type: ignore[import-not-found]
    except ImportError:  # pragma: no cover - Windows 以外
        return None

    hive = getattr(winreg, root, None)
    if hive is None:
        return None
    for view in (0, getattr(winreg, "KEY_WOW64_64KEY", 0), getattr(winreg, "KEY_WOW64_32KEY", 0)):
        try:
            with winreg.OpenKey(hive, key, 0, winreg.KEY_READ | view) as handle:
                value, _ = winreg.QueryValueEx(handle, name)
                return str(value)
        except OSError:
            continue
    return None


def detect_excel() -> ExcelInfo:
    """Excel のビット数を調べる。判定できなければ bitness=None を返す。"""
    if sys.platform != "win32":
        return ExcelInfo(None, "Windows ではないため判定できません")

    # 1) Microsoft 365 / クイック実行版はここに x86 / x64 が入っている
    platform = _registry_value(
        "HKEY_LOCAL_MACHINE", r"SOFTWARE\Microsoft\Office\ClickToRun\Configuration", "Platform"
    )
    bitness = bitness_from_platform_value(platform)
    if bitness:
        return ExcelInfo(bitness, "レジストリ（Office クイック実行の構成）")

    # 2) 従来のインストーラ版はバージョンごとに Bitness を持つ
    for version in ("16.0", "15.0", "14.0"):
        value = _registry_value(
            "HKEY_LOCAL_MACHINE", rf"SOFTWARE\Microsoft\Office\{version}\Outlook", "Bitness"
        )
        bitness = bitness_from_platform_value(value)
        if bitness:
            return ExcelInfo(bitness, f"レジストリ（Office {version}）")

    # 3) 最後は EXCEL.EXE の場所から推定する
    exe = _registry_value(
        "HKEY_LOCAL_MACHINE",
        r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\EXCEL.EXE",
        "",
    )
    bitness = bitness_from_path(exe)
    if bitness:
        return ExcelInfo(bitness, f"EXCEL.EXE の場所（{exe}）")

    return ExcelInfo(None, "判定できませんでした")


def addin_dir() -> Path:
    """RSS のアドインが置かれるフォルダ。"""
    local = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(local) / ADDIN_SUBPATH


MANUAL_CHECK = (
    "Excel で ファイル → アカウント → 「Microsoft Excel のバージョン情報」を開くと、\n"
    "    タイトル行の末尾に「32 ビット」「64 ビット」と出ます"
    "（ショートカット: Alt → F → D → A）。\n"
    "    ※ Windows のビット数ではなく Excel のビット数です。"
    "64bit Windows に 32bit Excel は普通にあります。"
)


def describe_addin_setup() -> list[str]:
    """`kabu doctor` に出す案内。"""
    info = detect_excel()
    lines: list[str] = []
    if info.bitness:
        lines.append(f"Excel のビット数  : {info.bitness} ビット　［{info.source}］")
        lines.append(f"  登録する 2 つ   : {info.xll_name}")
        lines.append(f"                    {VBA_ADDIN}")
    else:
        lines.append(f"Excel のビット数  : 判定できず　［{info.source}］")
        lines.append("  " + MANUAL_CHECK.replace("\n", "\n  "))

    folder = addin_dir()
    lines.append(f"  アドインの場所  : {folder}"
                 f"{'' if folder.exists() else '（まだ存在しません）'}")
    return lines
