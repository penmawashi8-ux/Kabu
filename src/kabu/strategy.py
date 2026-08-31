"""価格トリガーの判定。

「〇〇円以下になったら買い、〇〇円以上になったら売り」という判断だけを行う純関数群。
発注もリスク検査もここではやらない（テストしやすさのため）。
"""

from __future__ import annotations

from .config import Rule
from .models import OrderIntent, OrderType, Quote, RuleState, Side
from .state import RuleRuntimeState


def decide(
    rule: Rule,
    runtime: RuleRuntimeState,
    quote: Quote,
    *,
    allow_entry: bool,
    force_exit: bool = False,
) -> OrderIntent | None:
    """このルールが今この瞬間に出したい注文を 1 つ返す。無ければ None。

    allow_entry: 新規買いを許可するか（引け間際は False にする）
    force_exit : 保有中なら値段に関係なく手仕舞いするか（引け前の強制決済）
    """
    if not rule.enabled:
        return None
    if runtime.state in (RuleState.BUY_PENDING, RuleState.SELL_PENDING, RuleState.DONE, RuleState.HALTED):
        # 注文中は二重発注しない。完了・停止中も何もしない。
        return None

    if runtime.state is RuleState.FLAT:
        if not allow_entry or force_exit:
            return None
        if runtime.cycles_done >= rule.max_cycles:
            return None
        if quote.price <= rule.buy_at:
            if rule.stop_loss is not None and quote.price <= rule.stop_loss:
                # 買いラインだけでなく損切りラインまで割り込んでいる値段。ここで
                # 買うと、次の瞬間に損切りが発動して即撤退する。それを繰り返すと
                # 手数料を払いながら往復するだけになるので、下げ止まるまで買わない。
                return None
            return OrderIntent(
                rule_id=rule.id,
                symbol=rule.symbol,
                side=Side.BUY,
                quantity=rule.quantity,
                order_type=rule.order_type,
                price=rule.buy_at if rule.order_type is OrderType.LIMIT else quote.price,
                reason=f"現在値 {quote.price:g} が買いトリガー {rule.buy_at:g} 以下",
            )
        return None

    if runtime.state is RuleState.HOLDING:
        qty = runtime.position_qty or rule.quantity
        if force_exit:
            return OrderIntent(
                rule_id=rule.id,
                symbol=rule.symbol,
                side=Side.SELL,
                quantity=qty,
                order_type=OrderType.MARKET,
                price=quote.price,
                reason="引け前の強制手仕舞い",
            )
        if rule.stop_loss is not None and quote.price <= rule.stop_loss:
            # 損切りは滑ってでも逃げたいので成行。
            return OrderIntent(
                rule_id=rule.id,
                symbol=rule.symbol,
                side=Side.SELL,
                quantity=qty,
                order_type=OrderType.MARKET,
                price=quote.price,
                reason=f"現在値 {quote.price:g} が損切りライン {rule.stop_loss:g} 以下",
            )
        if quote.price >= rule.sell_at:
            return OrderIntent(
                rule_id=rule.id,
                symbol=rule.symbol,
                side=Side.SELL,
                quantity=qty,
                order_type=rule.order_type,
                price=rule.sell_at if rule.order_type is OrderType.LIMIT else quote.price,
                reason=f"現在値 {quote.price:g} が売りトリガー {rule.sell_at:g} 以上",
            )
    return None
