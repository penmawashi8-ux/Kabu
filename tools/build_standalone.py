#!/usr/bin/env python3
"""simulator.html を作り直す。

src/kabu/dashboard.html は Artifact としても公開できるように doctype や <head> を
持たない断片。これをブラウザで直接開ける 1 枚の HTML に包んで、リポジトリの
ルートに置く。Python も証券口座も要らずにダブルクリックで開ける入口になる。

    python tools/build_standalone.py

編集したら必ず実行すること（tests/test_standalone.py が同期を検査している）。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from kabu.webui import render_standalone_page  # noqa: E402

OUTPUT = ROOT / "simulator.html"


def main() -> int:
    OUTPUT.write_text(render_standalone_page(), encoding="utf-8")
    size = OUTPUT.stat().st_size
    print(f"{OUTPUT.relative_to(ROOT)} を書き出しました（{size:,} バイト）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
