"""Excel が無い環境（Mac/Linux）でロジックを動かすための擬似株価。

実際の相場とは無関係。状態遷移やリスク検査の挙動を目で確認するためだけのもの。
prices.json があればそれを優先して読むので、任意の値段を手で流し込める。
"""

from __future__ import annotations

import json
import math
import random
import time
from pathlib import Path

from .config import Config


class SimulatedFeed:
    def __init__(self, config: Config, *, override_file: str = "prices.json") -> None:
        self._override = Path(override_file)
        # 各銘柄の中心値をルールの買い/売りトリガーの中間に置き、その周りを揺らす。
        self._center = {
            rule.symbol: (rule.buy_at + rule.sell_at) / 2 for rule in config.rules
        }
        self._amplitude = {
            rule.symbol: max(1.0, (rule.sell_at - rule.buy_at) / 2 * 1.2) for rule in config.rules
        }
        self._rng = random.Random(0)
        self._t0 = time.time()

    def read_quotes(self, symbols: list[str]) -> dict[str, float]:
        override = self._read_override()
        out: dict[str, float] = {}
        elapsed = time.time() - self._t0
        for symbol in symbols:
            if symbol in override:
                out[symbol] = float(override[symbol])
                continue
            center = self._center.get(symbol, 1000.0)
            amp = self._amplitude.get(symbol, 50.0)
            # ゆっくりした正弦波 + 小さなノイズ。トリガーを跨ぐように振らせる。
            wave = math.sin(elapsed / 30.0)
            noise = self._rng.uniform(-0.05, 0.05)
            out[symbol] = round(center + amp * (wave + noise), 1)
        return out

    def _read_override(self) -> dict[str, float]:
        if not self._override.exists():
            return {}
        try:
            with self._override.open("r", encoding="utf-8") as f:
                data = json.load(f)
            return {str(k): float(v) for k, v in data.items()}
        except (json.JSONDecodeError, TypeError, ValueError):
            return {}
