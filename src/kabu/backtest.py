"""過去の実データで「あのときこのルールならどうなっていたか」を確かめる。

`kabu play --source real` と同じ考え方（日足を 寄り → 高値 → 安値 → 引け の
4 点でなぞる）だが、画面ではなく結果の要約を出す。回すのは実弾モードと
まったく同じ売買ロジック（strategy.py / risk.py）で、発注先だけが
PaperBroker に差し替わっている。

日足しか無いので、その日の値動きの順序は再現できない（高値と安値のどちらが
先だったかは分からない）。当たったかどうかは正しいが、往復の回数は
実際と変わりうる。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Callable, Sequence

from .broker import PaperBroker
from .clock import MarketClock
from .config import Config, RiskConfig
from .journal import Journal
from .marketdata import Bar
from .runner import Runner
from .state import AppState

log = logging.getLogger(__name__)

# 日足の 4 点を 1 日のどこに置くか（寄り → 高値 → 安値 → 引け）。
# 引けは大引けそのものではなく、新規建ての締切より前に置く。
_POINT_TIMES = (time(9, 0), time(10, 0), time(13, 0), time(14, 0))


@dataclass
class Trade:
    """1 回の売買（買って売るまで）。売り抜けていなければ exit は None。"""

    rule_id: str
    symbol: str
    quantity: int
    entry_at: datetime
    entry_price: float
    exit_at: datetime | None = None
    exit_price: float | None = None
    exit_note: str = ""
    exit_reason: str = ""   # 売り注文を出したときの理由（損切りかどうかが分かる）

    @property
    def closed(self) -> bool:
        return self.exit_price is not None

    @property
    def profit(self) -> float:
        if self.exit_price is None:
            return 0.0
        return (self.exit_price - self.entry_price) * self.quantity


@dataclass
class BacktestResult:
    days: list[date]
    bars: dict[str, list[Bar]]
    trades: list[Trade] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)
    open_positions: list[Trade] = field(default_factory=list)

    @property
    def realized(self) -> float:
        return sum(t.profit for t in self.trades if t.closed)

    @property
    def fills(self) -> int:
        return len([e for e in self.events if e["event"] == "FILL"])

    @property
    def orders(self) -> int:
        return len([e for e in self.events if e["event"] == "ORDER"])


class _CollectingJournal(Journal):
    """CSV には書かず、出来事をメモリに集める。"""

    def __init__(self, mode: str, now_provider, sink: list[dict[str, Any]]) -> None:
        super().__init__(Path("backtest.csv"), mode, now_provider)
        self._sink = sink

    def record(self, **kwargs: Any) -> None:  # CSV には書かない
        self._sink.append({
            "at": kwargs.get("at") or self._now(),
            "event": kwargs.get("event", ""),
            "rule_id": kwargs.get("rule_id", ""),
            "symbol": kwargs.get("symbol", ""),
            "side": kwargs.get("side", ""),
            "quantity": kwargs.get("quantity", ""),
            "price": kwargs.get("price", ""),
            "note": kwargs.get("note", ""),
        })


class _FrozenClock(MarketClock):
    """時刻を外から与える時計。バックテストの 1 点ごとに進める。"""

    def __init__(self, config, at: datetime) -> None:
        super().__init__(config)
        self._at = at

    def set(self, at: datetime) -> None:
        self._at = at

    def now(self) -> datetime:
        return self._at


def _replay_risk(risk: RiskConfig) -> RiskConfig:
    """日足の再生に噛み合わない安全弁だけを緩める。

    緩めるのは「実データの配信を疑うための弁」と「秒単位の時間軸を前提に
    した弁」の 2 種類だけ。1 回の発注額・1 日の発注件数といった、
    ルールそのものの制約はユーザーの設定のまま使う。

      * max_price_deviation_pct — 誤配信を弾く弁。日足では高値と安値の差が
        そのまま隣り合うので、正常な値動きでも引っかかる
      * pending_timeout_sec     — 指値が約定しないまま放置されるのを防ぐ弁。
        再生では点と点の間が数時間空くので、必ず引っかかる
      * min_seconds_between_orders / quote_max_age_sec — 同上、秒単位の前提
    """
    return replace(
        risk,
        max_price_deviation_pct=1_000_000.0,
        pending_timeout_sec=86_400.0,
        min_seconds_between_orders=0.0,
        quote_max_age_sec=86_400.0,
    )


def trading_days(bars: dict[str, list[Bar]]) -> list[date]:
    """データがある日を古い順に返す（銘柄をまたいで和集合）。"""
    days: set[date] = set()
    for series in bars.values():
        days.update(bar.day for bar in series)
    return sorted(days)


def select_days(
    bars: dict[str, list[Bar]],
    *,
    days: int | None = None,
    since: date | None = None,
    until: date | None = None,
) -> list[date]:
    """回す日を決める。既定は直近 1 日。"""
    available = trading_days(bars)
    if since:
        available = [d for d in available if d >= since]
    if until:
        available = [d for d in available if d <= until]
    if days is not None:
        available = available[-days:] if days > 0 else available
    return available


def _price_points(
    bars: dict[str, list[Bar]], day: date
) -> list[tuple[datetime, dict[str, float]]]:
    """その日の 4 点を「時刻 → 銘柄ごとの値段」に組み替える。"""
    by_symbol = {
        symbol: next((b for b in series if b.day == day), None)
        for symbol, series in bars.items()
    }
    points: list[tuple[datetime, dict[str, float]]] = []
    for i, at_time in enumerate(_POINT_TIMES):
        quotes = {
            symbol: bar.walk()[i] for symbol, bar in by_symbol.items() if bar is not None
        }
        if quotes:
            points.append((datetime.combine(day, at_time), quotes))
    return points


def run_backtest(
    config: Config,
    bars: dict[str, list[Bar]],
    *,
    days: int | None = 1,
    since: date | None = None,
    until: date | None = None,
) -> BacktestResult:
    """実データを流して、その期間の売買を再現する。

    state ファイルには触らない（保存はメモリ上だけで完結させる）。
    """
    target_days = select_days(bars, days=days, since=since, until=until)
    result = BacktestResult(days=target_days, bars=bars)
    if not target_days:
        return result

    # 立会時間の判定は使うが、祝日リストで弾かれると回せないので日付は素直に信じる。
    market = replace(config.market, holidays=())
    config = replace(config, mode="paper", market=market, risk=_replay_risk(config.risk))

    first_at = datetime.combine(target_days[0], _POINT_TIMES[0])
    clock = _FrozenClock(market, first_at)
    journal = _CollectingJournal("paper", clock.now, result.events)
    broker = PaperBroker()

    quotes_now: dict[str, float] = {}
    runner = Runner(
        config, broker, lambda symbols: {s: quotes_now[s] for s in symbols if s in quotes_now},
        clock=clock, journal=journal, state_store=_MemoryStore(),
    )

    for day in target_days:
        for at, quotes in _price_points(bars, day):
            quotes_now = quotes
            # 同じ値段で 2 回回す。1 回目で注文が出て、2 回目でその値段の
            # 約定判定が走る。実際の場では値段はその水準にしばらく留まるので、
            # 「線に触れた瞬間に注文を出したが、次に見たときには戻っていて
            # 永久に約定しない」という、標本点が粗いことによる嘘を防ぐ。
            for offset in (0, 1):
                moment = at + timedelta(seconds=offset)
                clock.set(moment)
                try:
                    runner.tick(moment)
                except Exception:  # pragma: no cover - 1 点の失敗で全体を止めない
                    log.exception("バックテストの %s でエラーが発生しました", moment)

    _pair_up_trades(result)
    return result


class _MemoryStore:
    """state.json を書かない保存先。バックテストは痕跡を残さない。"""

    def __init__(self) -> None:
        self.state = AppState()

    def load(self) -> AppState:
        return self.state

    def save(self, state: AppState) -> None:
        self.state = state


def _pair_up_trades(result: BacktestResult) -> None:
    """約定イベントを「買い → 売り」の組にまとめる。"""
    open_by_rule: dict[str, Trade] = {}
    last_sell_reason: dict[str, str] = {}
    for event in result.events:
        # 売り注文の理由は ORDER 側にしか無いので、直前のものを憶えておく。
        if event["event"] == "ORDER" and event["side"] == "SELL":
            last_sell_reason[str(event["rule_id"])] = str(event.get("note", ""))
        if event["event"] != "FILL":
            continue
        try:
            price = float(event["price"])
            quantity = int(event["quantity"])
        except (TypeError, ValueError):
            continue
        rule_id = str(event["rule_id"])
        if event["side"] == "BUY":
            open_by_rule[rule_id] = Trade(
                rule_id=rule_id, symbol=str(event["symbol"]), quantity=quantity,
                entry_at=event["at"], entry_price=price,
            )
            continue
        trade = open_by_rule.pop(rule_id, None)
        if trade is None:
            continue
        trade.exit_at = event["at"]
        trade.exit_price = price
        trade.exit_note = str(event.get("note", ""))
        trade.exit_reason = last_sell_reason.get(rule_id, "")
        result.trades.append(trade)
    result.open_positions = list(open_by_rule.values())
