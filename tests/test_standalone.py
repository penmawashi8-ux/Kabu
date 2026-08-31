"""simulator.html（ダブルクリックで開ける 1 枚版）の検査。

Artifact 用の断片（dashboard.html）から生成しているので、片方だけ直して
もう片方を忘れると内容がずれる。それを防ぐための同期チェック。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kabu.webui import dashboard_source, render_standalone_page

ROOT = Path(__file__).resolve().parents[1]
STANDALONE = ROOT / "simulator.html"


def test_standalone_file_is_in_sync_with_the_dashboard():
    """ずれていたら `python tools/build_standalone.py` を実行すること。"""
    assert STANDALONE.exists(), "simulator.html がありません"
    assert STANDALONE.read_text(encoding="utf-8") == render_standalone_page(), (
        "simulator.html が dashboard.html と一致しません。"
        "`python tools/build_standalone.py` で作り直してください"
    )


def test_standalone_declares_utf8_before_any_japanese_text():
    """charset の宣言が後ろにあると日本語が化ける。"""
    html = STANDALONE.read_text(encoding="utf-8")
    charset_at = html.index("<meta charset=")
    first_japanese = min(html.index("株"), html.index("円"))
    assert charset_at < first_japanese


def test_standalone_is_a_complete_document():
    html = STANDALONE.read_text(encoding="utf-8")
    for token in ("<!doctype html>", '<html lang="ja">', "<head>", "<body>"):
        assert token in html


def test_dashboard_fragment_has_no_document_wrapper():
    """Artifact 側は断片のまま保つ（包むのは公開時と simulator.html だけ）。"""
    fragment = dashboard_source()
    for token in ("<!doctype", "<html", "<body>"):
        assert token not in fragment.lower()


@pytest.mark.parametrize("marker", ["<title>", "<style>", "<script>"])
def test_standalone_carries_the_page_itself(marker):
    assert marker in STANDALONE.read_text(encoding="utf-8")
