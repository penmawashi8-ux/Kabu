"""ワーカースレッドで COM（Excel / RSS）を使うための下ごしらえ。

COM はスレッドごとに初期化が必要で、`CoInitialize` を呼んでいないスレッドから
Excel を触ると `CoInitialize は呼び出されていません` で落ちる。`kabu play` は
売買ループを別スレッドで回すため、そのスレッドの入口でこれを呼ぶ。

Windows 以外（pywin32 が無い環境）では何もしない。
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)


def init_com() -> Any | None:
    """このスレッドで COM を使えるようにする。戻り値は release_com() に渡す。"""
    try:
        import pythoncom  # type: ignore[import-not-found]
    except ImportError:  # Windows 以外。COM 自体が無いので何もしなくてよい
        return None
    try:
        pythoncom.CoInitialize()
    except Exception:  # pragma: no cover - 既に初期化済みなら続行してよい
        log.debug("CoInitialize に失敗しました（初期化済みとみなして続行します）", exc_info=True)
    return pythoncom


def release_com(handle: Any | None) -> None:
    """init_com() で確保したものを解放する。"""
    if handle is None:
        return
    try:
        handle.CoUninitialize()
    except Exception:  # pragma: no cover - 終了処理なので失敗しても黙って進む
        log.debug("CoUninitialize に失敗しました", exc_info=True)
