"""売買ループ本体。

1 周のなかで行うこと:
  1. 緊急停止ファイルの確認
  2. 立会時間の確認
  3. 株価の取得と検証
  4. 発注中の注文の約定確認（状態遷移）
  5. 各ルールの判定 → リスク検査 → 発注
  6. 状態の保存
"""

from __future__ import annotations

import logging
import time as time_module
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Iterable

from .broker import Broker, PaperBroker
from .clock import MarketClock
from .config import Config, Rule
from .errors import KillSwitch, QuoteError
from .journal import Journal
from .models import BrokerOrder, OrderStatus, Quote, RuleState, Side
from .risk import RiskManager
from .state import AppState, StateStore
from .strategy import decide

log = logging.getLogger(__name__)

QuoteSource = Callable[[list[str]], dict[str, float]]


@dataclass
class TickResult:
    """1 周の結果。テストとログ出力で使う。"""

    ran: bool
    reason: str = ""
    orders_placed: int = 0
    fills: int = 0


class Runner:
    def __init__(
        self,
        config: Config,
        broker: Broker,
        quote_source: QuoteSource,
        *,
        clock: MarketClock | None = None,
        state_store: StateStore | None = None,
        journal: Journal | None = None,
    ) -> None:
        self.config = config
        self.broker = broker
        self.quote_source = quote_source
        self.clock = clock or MarketClock(config.market)
        self.store = state_store or StateStore(config.state_file)
        self.journal = journal or Journal(config.journal_file, config.mode, self.clock.now)
        self.risk = RiskManager(config.risk)
        self.state: AppState = self.store.load()
        self._rules = {r.id: r for r in config.rules}

    # ------------------------------------------------------------------ ループ

    def run_forever(self) -> None:
        log.info(
            "起動しました（mode=%s, ルール %d 件, 監視間隔 %g 秒）",
            self.config.mode, len(self._rules), self.config.poll_interval_sec,
        )
        self.journal.record(event="START", note=f"ルール {len(self._rules)} 件")
        try:
            while True:
                try:
                    result = self.tick()
                except KillSwitch as exc:
                    log.warning("緊急停止: %s", exc)
                    self.journal.record(event="KILL_SWITCH", note=str(exc))
                    return
                except QuoteError as exc:
                    # 株価が取れないのは一時的なことが多い。次の周回で回復を待つ。
                    log.error("株価取得エラー: %s", exc)
                    result = TickResult(ran=False, reason="quote_error")
                except Exception:
                    log.exception("想定外のエラー。安全のため停止します")
                    self.journal.record(event="FATAL", note="想定外のエラー")
                    raise

                time_module.sleep(self._sleep_seconds(result))
        except KeyboardInterrupt:
            log.info("Ctrl-C を受け取りました。終了します")
            self.journal.record(event="STOP", note="手動停止")
        finally:
            self.store.save(self.state)
            self.broker.close()

    def _sleep_seconds(self, result: TickResult) -> float:
        if result.reason == "closed":
            # 閉場中は最大 5 分単位で待つ（Ctrl-C の反応が鈍くならない程度に）。
            return min(300.0, max(self.config.poll_interval_sec, self.clock.seconds_until_open()))
        return self.config.poll_interval_sec

    # -------------------------------------------------------------------- 1 周

    def tick(self, now: datetime | None = None) -> TickResult:
        now = now or self.clock.now()
        now_epoch = now.timestamp()

        if self.risk.kill_switch_engaged():
            raise KillSwitch(f"停止ファイル {self.config.risk.kill_switch_file} を検出しました")

        if self.state.daily.roll_over_if_needed(now.date()):
            log.info("日付が変わったので当日カウンタをリセットしました (%s)", self.state.daily.day)

        if not self.clock.is_open(now):
            return TickResult(ran=False, reason="closed")

        symbols = sorted({r.symbol for r in self._rules.values() if r.enabled})
        if not symbols:
            return TickResult(ran=False, reason="no_enabled_rules")

        raw_quotes = self.quote_source(symbols)
        quotes = self._validate_quotes(raw_quotes, now)

        # ペーパートレードでは、この値段で指値の約定判定を進める。
        if isinstance(self.broker, PaperBroker):
            for symbol, quote in quotes.items():
                self.broker.on_quote(symbol, quote.price)

        fills = self._reconcile(now_epoch)
        placed = self._apply_rules(quotes, now, now_epoch)

        self.store.save(self.state)
        return TickResult(ran=True, orders_placed=placed, fills=fills)

    def _validate_quotes(self, raw: dict[str, float], now: datetime) -> dict[str, Quote]:
        """株価を検証し、通ったものだけを返す。弾いた銘柄は last_price を更新しない。"""
        out: dict[str, Quote] = {}
        for symbol, price in raw.items():
            quote = Quote(symbol=symbol, price=float(price), asof=now)
            verdict = self.risk.validate_quote(quote, self.state, now_epoch=now.timestamp())
            if not verdict.allowed:
                log.warning("株価を無視しました: %s", verdict.reason)
                continue
            out[symbol] = quote
            self.state.last_price[symbol] = quote.price
        return out

    # ------------------------------------------------------------ 約定の突き合わせ

    def _reconcile(self, now_epoch: float) -> int:
        """発注中のルールについて、証券会社側の注文状態を見て状態を進める。"""
        pending = {
            rid: rt for rid, rt in self.state.rules.items()
            if rt.state in (RuleState.BUY_PENDING, RuleState.SELL_PENDING) and rt.open_order_id
        }
        if not pending:
            return 0

        by_id = {o.order_id: o for o in self.broker.list_orders()}
        fills = 0
        for rule_id, runtime in pending.items():
            order = by_id.get(str(runtime.open_order_id))
            if order is None:
                self._handle_missing_order(rule_id, runtime, now_epoch)
                continue
            if order.status is OrderStatus.FILLED:
                self._on_filled(rule_id, runtime, order)
                fills += 1
            elif order.status in (OrderStatus.CANCELLED, OrderStatus.REJECTED):
                self._on_dead_order(rule_id, runtime, order)
            else:
                self._check_pending_timeout(rule_id, runtime, now_epoch)
        return fills

    def _handle_missing_order(self, rule_id: str, runtime, now_epoch: float) -> None:
        """注文一覧に見当たらない。反映待ちの猶予を過ぎたら人間に判断を委ねる。"""
        placed_at = runtime.open_order_placed_at or now_epoch
        if now_epoch - placed_at < self.config.risk.pending_timeout_sec:
            return
        self._halt(
            rule_id, runtime,
            f"注文 {runtime.open_order_id} が注文一覧に見つからないまま "
            f"{self.config.risk.pending_timeout_sec:.0f} 秒経過しました。手動で確認してください",
        )

    def _check_pending_timeout(self, rule_id: str, runtime, now_epoch: float) -> None:
        placed_at = runtime.open_order_placed_at or now_epoch
        if now_epoch - placed_at < self.config.risk.pending_timeout_sec:
            return
        self._halt(
            rule_id, runtime,
            f"注文 {runtime.open_order_id} が約定しないまま "
            f"{self.config.risk.pending_timeout_sec:.0f} 秒経過しました。手動で確認してください",
        )

    def _on_filled(self, rule_id: str, runtime, order: BrokerOrder) -> None:
        rule = self._rules[rule_id]
        price = order.avg_price or 0.0
        if order.side is Side.BUY:
            runtime.state = RuleState.HOLDING
            runtime.position_qty = order.filled_quantity
            runtime.entry_price = price
            runtime.last_note = f"{price:g} 円で {order.filled_quantity} 株買付"
            log.info("[%s] 買い約定: %s %d 株 @ %g", rule_id, rule.symbol, order.filled_quantity, price)
        else:
            pnl = ""
            if runtime.entry_price:
                gross = (price - runtime.entry_price) * order.filled_quantity
                pnl = f"（概算損益 {gross:+,.0f} 円 / 手数料・税金は含まず）"
            runtime.cycles_done += 1
            runtime.position_qty = 0
            runtime.entry_price = None
            runtime.state = RuleState.DONE if runtime.cycles_done >= rule.max_cycles else RuleState.FLAT
            runtime.last_note = f"{price:g} 円で売却 {pnl}"
            log.info("[%s] 売り約定: %s %d 株 @ %g %s",
                     rule_id, rule.symbol, order.filled_quantity, price, pnl)

        runtime.open_order_id = None
        runtime.open_order_placed_at = None
        self.journal.record(
            event="FILL", rule_id=rule_id, symbol=order.symbol, side=order.side.value,
            quantity=order.filled_quantity, price=price, order_id=order.order_id,
            note=runtime.last_note,
        )

    def _on_dead_order(self, rule_id: str, runtime, order: BrokerOrder) -> None:
        # 買いが流れたら振り出しに戻る。売りが流れたのは玉が残っているので危険 → 停止。
        runtime.open_order_id = None
        runtime.open_order_placed_at = None
        if runtime.state is RuleState.BUY_PENDING:
            runtime.state = RuleState.FLAT
            runtime.last_note = f"買い注文が{order.status.value}になりました"
            log.info("[%s] 買い注文 %s: %s", rule_id, order.status.value, order.order_id)
        else:
            self._halt(
                rule_id, runtime,
                f"売り注文 {order.order_id} が{order.status.value}になりました。"
                "建玉が残っている可能性があるため手動で確認してください",
            )
        self.journal.record(
            event=order.status.value, rule_id=rule_id, symbol=order.symbol,
            side=order.side.value, order_id=order.order_id, note=runtime.last_note,
        )

    def _halt(self, rule_id: str, runtime, reason: str) -> None:
        runtime.state = RuleState.HALTED
        runtime.last_note = reason
        log.error("[%s] 停止: %s", rule_id, reason)
        self.journal.record(event="HALT", rule_id=rule_id, note=reason)

    # ---------------------------------------------------------------- 判定と発注

    def _apply_rules(self, quotes: dict[str, Quote], now: datetime, now_epoch: float) -> int:
        allow_entry = self.clock.accepts_new_entry(now)
        force_exit = self.clock.should_close_positions(now)
        placed = 0

        for rule in self._active_rules():
            quote = quotes.get(rule.symbol)
            if quote is None:
                continue
            runtime = self.state.rule(rule.id)
            intent = decide(rule, runtime, quote, allow_entry=allow_entry, force_exit=force_exit)
            if intent is None:
                continue

            verdict = self.risk.check_order(intent, self.state, now_epoch=now_epoch)
            if not verdict.allowed:
                log.warning("[%s] 発注を見送りました: %s", rule.id, verdict.reason)
                self.journal.record(
                    event="BLOCKED", rule_id=rule.id, symbol=intent.symbol,
                    side=intent.side.value, quantity=intent.quantity, price=intent.price or "",
                    note=verdict.reason,
                )
                continue

            log.info("[%s] %s %s %d株 @ %s — %s",
                     rule.id, intent.symbol, intent.side.japanese, intent.quantity,
                     f"{intent.price:g}" if intent.price else "成行", intent.reason)

            result = self.broker.place_order(intent)
            if not result.ok or not result.order_id:
                self._halt(rule.id, runtime, f"発注に失敗しました: {result.message}")
                self.journal.record(
                    event="ORDER_FAILED", rule_id=rule.id, symbol=intent.symbol,
                    side=intent.side.value, quantity=intent.quantity, note=result.message,
                )
                continue

            self.risk.register_order(intent, self.state, now_epoch=now_epoch)
            runtime.state = RuleState.BUY_PENDING if intent.side is Side.BUY else RuleState.SELL_PENDING
            runtime.open_order_id = result.order_id
            runtime.open_order_placed_at = now_epoch
            runtime.last_note = intent.reason
            placed += 1
            self.journal.record(
                event="ORDER", rule_id=rule.id, symbol=intent.symbol, side=intent.side.value,
                quantity=intent.quantity, price=intent.price or "", order_id=result.order_id,
                note=intent.reason,
            )
        return placed

    def _active_rules(self) -> Iterable[Rule]:
        return (r for r in self.config.rules if r.enabled)

    # ------------------------------------------------------------------ 表示用

    def status_lines(self) -> list[str]:
        lines = [
            f"モード      : {self.config.mode}",
            f"本日の発注  : {self.state.daily.orders_placed} 件 / 買付 {self.state.daily.notional_used:,.0f} 円",
        ]
        for rule in self.config.rules:
            rt = self.state.rule(rule.id)
            lines.append(
                f"  [{rule.id}] {rule.symbol} {rt.state.value} "
                f"サイクル {rt.cycles_done}/{rule.max_cycles} "
                f"買 {rule.buy_at:g} / 売 {rule.sell_at:g}"
                + (f" — {rt.last_note}" if rt.last_note else "")
            )
        return lines
