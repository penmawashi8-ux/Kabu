"""例外の定義。"""

from __future__ import annotations


class KabuError(Exception):
    """本ツールが送出する例外の基底クラス。"""


class ConfigError(KabuError):
    """設定ファイルが不正。"""


class QuoteError(KabuError):
    """株価が取得できない／信用できない。"""


class BrokerError(KabuError):
    """発注経路の異常。"""


class KillSwitch(KabuError):
    """緊急停止。ループを即座に終了させる。"""
