"""安全弁。

自動売買で一番怖いのは「ロジックのミスで想定外の注文を連射すること」なので、
戦略の判断とは独立に、発注直前で必ずここを通す。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .config import RiskConfig
from .models import OrderIntent, Quote, Side
from .state import AppState


@dataclass(frozen=True)
class RiskVerdict:
    allowed: bool
    reason: str = ""

    @staticmethod
    def ok() -> "RiskVerdict":
        return RiskVerdict(True)

    @staticmethod
    def deny(reason: str) -> "RiskVerdict":
        return RiskVerdict(False, reason)


class RiskManager:
    def __init__(self, config: RiskConfig) -> None:
        self._config = config

    def kill_switch_engaged(self) -> bool:
        """指定ファイルを置くだけで止められる。実行中の緊急停止手段。"""
        return Path(self._config.kill_switch_file).exists()

    def validate_quote(self, quote: Quote, state: AppState, *, now_epoch: float) -> RiskVerdict:
        """飛び値・気配のゼロ・古すぎる値段を弾く。"""
        if quote.price <= 0:
            return RiskVerdict.deny(f"{quote.symbol}: 株価が {quote.price} は不正")

        age = now_epoch - quote.asof.timestamp()
        if age > self._config.quote_max_age_sec:
            return RiskVerdict.deny(
                f"{quote.symbol}: 株価が {age:.0f} 秒前のもので古すぎます"
                f"（上限 {self._config.quote_max_age_sec:.0f} 秒）"
            )

        last = state.last_price.get(quote.symbol)
        if last and last > 0:
            deviation = abs(quote.price - last) / last * 100
            if deviation > self._config.max_price_deviation_pct:
                return RiskVerdict.deny(
                    f"{quote.symbol}: 前回値 {last:g} から {deviation:.1f}% 乖離しています"
                    f"（上限 {self._config.max_price_deviation_pct:g}%）。誤配信の可能性があるため無視します"
                )
        return RiskVerdict.ok()

    def check_order(self, intent: OrderIntent, state: AppState, *, now_epoch: float) -> RiskVerdict:
        cfg = self._config
        daily = state.daily

        if daily.orders_placed >= cfg.max_orders_per_day:
            return RiskVerdict.deny(
                f"本日の発注上限 {cfg.max_orders_per_day} 件に到達しました"
            )

        elapsed = now_epoch - daily.last_order_epoch
        if daily.last_order_epoch > 0 and elapsed < cfg.min_seconds_between_orders:
            return RiskVerdict.deny(
                f"前回発注から {elapsed:.1f} 秒しか経っていません"
                f"（最低 {cfg.min_seconds_between_orders:g} 秒）"
            )

        notional = intent.notional
        if notional <= 0:
            return RiskVerdict.deny("約定代金を計算できないため発注しません")

        if notional > cfg.max_notional_per_order:
            return RiskVerdict.deny(
                f"1 回の約定代金 {notional:,.0f} 円が上限 {cfg.max_notional_per_order:,.0f} 円を超えます"
            )

        # 売り注文は建玉の解消なので、日次の投入金額上限には算入しない。
        if intent.side is Side.BUY and daily.notional_used + notional > cfg.max_notional_per_day:
            return RiskVerdict.deny(
                f"本日の買付総額が上限 {cfg.max_notional_per_day:,.0f} 円を超えます"
                f"（既に {daily.notional_used:,.0f} 円）"
            )

        return RiskVerdict.ok()

    def register_order(self, intent: OrderIntent, state: AppState, *, now_epoch: float) -> None:
        state.daily.orders_placed += 1
        state.daily.last_order_epoch = now_epoch
        if intent.side is Side.BUY:
            state.daily.notional_used += intent.notional
