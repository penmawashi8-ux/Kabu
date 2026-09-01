"""複数銘柄を同時に持つ場合の検証。

これまでの検証（research.py）は 1 銘柄ずつ売買する話だった。ここでは
**同時に何銘柄も持つ**場合を扱う。狙いはタイミングではなく分散で、
「期待リターンを下げずにブレだけ減らせるか」を見る。

**現実の制約を必ず入れる。**

  * 日本株は 100 株単位（単元株）でしか買えない。3,000 円の株なら 1 銘柄
    30 万円。資金 100 万円では 3 銘柄しか持てず、分散はそこまで効かない。
    「20 銘柄に分散すれば安全」という机上の話にしないため、資金額から
    実際に持てる銘柄数を計算する
  * 入れ替えのたびに利益が確定して課税される（特定口座で約 20.315%）。
    楽天証券のゼロコースなら売買手数料は 0 円だが、税金は残る
  * 判断は当日の終値まで、約定は翌日の寄り付き（未来を覗かない）
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import date

from .marketdata import Bar

LOT = 100   # 単元株


@dataclass
class Holding:
    symbol: str
    shares: int
    entry_price: float


@dataclass
class PortfolioResult:
    name: str
    equity: list[tuple[date, float]] = field(default_factory=list)
    rebalances: int = 0
    realized_tax: float = 0.0
    names_held: list[int] = field(default_factory=list)   # 各時点の保有銘柄数
    skipped_capital: bool = False

    @property
    def return_pct(self) -> float:
        if len(self.equity) < 2:
            return 0.0
        start, end = self.equity[0][1], self.equity[-1][1]
        return (end - start) / start * 100 if start else 0.0

    @property
    def max_drawdown_pct(self) -> float:
        peak, worst = float("-inf"), 0.0
        for _day, value in self.equity:
            peak = max(peak, value)
            if peak > 0:
                worst = min(worst, (value - peak) / peak * 100)
        return worst

    @property
    def average_names(self) -> float:
        return sum(self.names_held) / len(self.names_held) if self.names_held else 0.0


def _closes_by_day(bars: dict[str, list[Bar]]) -> dict[date, dict[str, Bar]]:
    """日付 → その日に値がある銘柄の四本値。"""
    out: dict[date, dict[str, Bar]] = {}
    for symbol, series in bars.items():
        for bar in series:
            out.setdefault(bar.day, {})[symbol] = bar
    return out


def _momentum(series: list[Bar], day: date, lookback: int) -> float | None:
    """その日までの騰落率（過去 lookback 営業日）。銘柄間の比較に使う。"""
    history = [b for b in series if b.day <= day]
    if len(history) <= lookback:
        return None
    old, new = history[-lookback - 1].close, history[-1].close
    return (new - old) / old * 100 if old else None


def run_portfolio(
    bars: dict[str, list[Bar]],
    *,
    capital: float = 1_000_000,
    hold: int = 5,
    rebalance_days: int = 20,
    select: str = "momentum",
    lookback: int = 120,
    tax_pct: float = 20.315,
    slippage_pct: float = 0.05,
) -> PortfolioResult:
    """N 銘柄を同時に持ち、一定間隔で入れ替える。

    select="momentum" なら過去 lookback 日の騰落率で上位を選ぶ。
    select="equal" なら（並べ替えず）先頭から N 銘柄を等ウェイトで持ち続ける。
    """
    name = ("モメンタム上位 {n} 銘柄・{r} 日ごと入替" if select == "momentum"
            else "等ウェイト {n} 銘柄・{r} 日ごと入替").format(n=hold, r=rebalance_days)
    result = PortfolioResult(name=name)

    by_day = _closes_by_day(bars)
    days = sorted(by_day)
    if len(days) < lookback + rebalance_days + 2:
        return result

    cash = capital
    holdings: dict[str, Holding] = {}
    start_index = lookback + 1

    for i in range(start_index, len(days) - 1):
        today, tomorrow = days[i], days[i + 1]
        prices_today, prices_tomorrow = by_day[today], by_day[tomorrow]

        # 評価額（現金 + 保有の時価）。
        value = cash + sum(
            h.shares * prices_today[s].close
            for s, h in holdings.items() if s in prices_today
        )
        result.equity.append((today, value))
        result.names_held.append(len(holdings))

        if (i - start_index) % rebalance_days:
            continue

        # ---- 入れ替え。判断は今日の終値まで、約定は翌日の寄り付き ----
        candidates: list[str] = []
        if select == "momentum":
            scored = [
                (s, _momentum(bars[s], today, lookback))
                for s in prices_today if s in bars
            ]
            candidates = [s for s, m in sorted(
                ((s, m) for s, m in scored if m is not None),
                key=lambda row: row[1], reverse=True,
            )][:hold]
        else:
            candidates = sorted(prices_today)[:hold]

        # 売り: 選から外れたものを翌日の寄りで処分（利益には課税）。
        for symbol in list(holdings):
            if symbol in candidates or symbol not in prices_tomorrow:
                continue
            position = holdings.pop(symbol)
            price = prices_tomorrow[symbol].open * (1 - slippage_pct / 100)
            proceeds = position.shares * price
            gain = proceeds - position.shares * position.entry_price
            tax = gain * tax_pct / 100 if gain > 0 else 0.0
            result.realized_tax += tax
            cash += proceeds - tax

        # 買い: 現金を等分して割り当てる。ただし等分では 1 単元も買えないとき、
        # 資金があるなら優先順位の高い銘柄から 1 単元ずつ買う。均等配分に
        # こだわって現金を寝かせるのは、実際の買い方とかけ離れるため。
        wanted = [s for s in candidates if s not in holdings and s in prices_tomorrow]
        if wanted:
            budget = cash / len(wanted)
            for symbol in wanted:            # candidates の順 = 優先順位
                price = prices_tomorrow[symbol].open * (1 + slippage_pct / 100)
                lot_cost = price * LOT
                lots = int(budget // lot_cost)
                if lots < 1 and cash >= lot_cost:
                    lots = 1
                if lots < 1:
                    result.skipped_capital = True
                    continue
                shares = lots * LOT
                cost = shares * price
                if cost > cash:
                    continue
                cash -= cost
                holdings[symbol] = Holding(symbol=symbol, shares=shares, entry_price=price)
            result.rebalances += 1

    # 最終日に時価評価（持ち越しを勝ちに見せない）。
    last = days[-1]
    final = cash + sum(
        h.shares * by_day[last][s].close for s, h in holdings.items() if s in by_day[last]
    )
    result.equity.append((last, final))
    return result


def equal_weight_index(bars: dict[str, list[Bar]], *, tax_pct: float = 20.315) -> tuple[float, float]:
    """全銘柄を等ウェイトで買って、最後まで持った場合（インデックス代わり）。

    ポートフォリオ手法を評価するときの正しい比較対象。1 銘柄の中央値と
    比べるだけでは、「分散したから良くなった」のか「選び方が良かった」のかが
    区別できない。こちらは分散だけを効かせた基準になる。

    単元株の制約は入れない（指数に投資する代わりの仮想的な基準のため）。
    戻り値は (損益率, 最大下落率)。
    """
    by_day = _closes_by_day(bars)
    days = sorted(by_day)
    if len(days) < 2:
        return 0.0, 0.0

    first = {s: bar.close for s, bar in by_day[days[0]].items() if bar.close > 0}
    if not first:
        return 0.0, 0.0

    curve: list[float] = []
    for day in days:
        prices = by_day[day]
        values = [prices[s].close / base for s, base in first.items() if s in prices]
        if values:
            curve.append(sum(values) / len(values))

    gross = (curve[-1] - 1) * 100
    net = gross * (1 - tax_pct / 100) if gross > 0 else gross

    peak, worst = float("-inf"), 0.0
    for value in curve:
        peak = max(peak, value)
        worst = min(worst, (value - peak) / peak * 100)
    return net, worst


def single_stock_median(bars: dict[str, list[Bar]], *, tax_pct: float = 20.315,
                        slippage_pct: float = 0.05) -> float:
    """1 銘柄ずつ持ちっぱなしにしたときの中央値（比較の基準）。"""
    from .research import buy_and_hold

    values = [
        buy_and_hold(series).net_return_pct(tax_pct=tax_pct, slippage_pct=slippage_pct)
        for series in bars.values() if len(series) > 2
    ]
    return statistics.median(values) if values else 0.0
