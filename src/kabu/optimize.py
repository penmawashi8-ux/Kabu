"""「どの銘柄を、どの値幅で売買していたら良かったか」を過去データから総当たりで探す。

やっていることは単純で、値幅の候補を並べて片っ端からバックテストし、成績順に
並べるだけ。ただし**過去に一番良かった設定が、これから儲かる設定とは限らない**。
むしろ過去に合わせ込むほど、将来は当たらなくなる（過剰適合）。

そこで、この探索は必ず期間を 2 つに割って行う。

    * 探索期間（train）   — ここで候補を総当たりして良いものを選ぶ
    * 検証期間（holdout） — 選んだあとで初めて触る。選定に一切使わない

検証期間の成績が、実運用で期待できる姿にいくらか近い。探索期間の成績は
「後から見れば分かったこと」であって、実力ではない。

さらに、単純に持ちっぱなしにした場合（buy & hold）の成績も並べて出す。
売買を繰り返して手数料を払う価値があったのかは、それと比べないと分からない。

**期間は短く区切って何度も回す。** このボットの売買ラインは「2,800 円で買う」と
いう固定の値段なので、株価が大きく動いてしまえばラインは置き去りになる。
1 年ぶんを一続きで回しても、測っているのは値幅の設定ではなく「株価がどれだけ
ドリフトしたか」になってしまう。そこで期間を 20 営業日ほどに区切り、区間ごとに
その直前の終値を基準として線を引き直し、何区間ぶんも積み上げて評価する。
実際の使い方（いまの株価を見て線を引き、しばらく回して、また引き直す）にも近い。

「安定して」の中身は、**儲かった区間の割合**と**最悪の区間**で見る。合計だけでは
1 区間の大当たりで平均が持ち上がっていても分からない。
"""

from __future__ import annotations

import logging
import statistics
from dataclasses import dataclass, replace
from datetime import date

from .backtest import run_backtest
from .config import Config, Rule
from .marketdata import Bar

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Candidate:
    """基準価格からの値幅（%）で表した売買ルール。"""

    buy_pct: float    # 下げ幅（負の数）。例: -4.0 → 基準から 4% 下で買う
    sell_pct: float   # 上げ幅（正の数）
    stop_pct: float   # 損切りの下げ幅（buy_pct より下）

    def to_rule(self, rule_id: str, symbol: str, reference: float, quantity: int) -> Rule:
        return Rule(
            id=rule_id,
            symbol=symbol,
            quantity=quantity,
            buy_at=round(reference * (1 + self.buy_pct / 100), 1),
            sell_at=round(reference * (1 + self.sell_pct / 100), 1),
            stop_loss=round(reference * (1 + self.stop_pct / 100), 1),
            max_cycles=999,
        )

    def label(self) -> str:
        return f"買 {self.buy_pct:+.1f}% / 売 {self.sell_pct:+.1f}% / 損切 {self.stop_pct:+.1f}%"


@dataclass
class Score:
    """1 つの候補を 1 つの期間で回した結果。"""

    symbol: str
    candidate: Candidate
    reference: float
    quantity: int
    trades: int = 0
    wins: int = 0
    realized: float = 0.0
    worst: float = 0.0            # 1 回の売買での最大損失
    held_at_end: int = 0
    segments: int = 0             # 回した区間の数
    profitable_segments: int = 0  # そのうち利益で終わった区間
    worst_segment: float = 0.0    # 一番負けた区間の損益

    @property
    def capital(self) -> float:
        """1 回の売買に必要な金額（率の分母に使う）。"""
        return self.reference * self.quantity

    @property
    def return_pct(self) -> float:
        return self.realized / self.capital * 100 if self.capital else 0.0

    @property
    def win_rate(self) -> float:
        return self.wins / self.trades * 100 if self.trades else 0.0

    @property
    def steady_pct(self) -> float:
        """利益で終わった区間の割合。「安定して」の中身。"""
        return self.profitable_segments / self.segments * 100 if self.segments else 0.0


@dataclass
class SymbolReport:
    symbol: str
    reference: float
    quantity: int
    train: list[Score]                 # 探索期間の上位
    holdout: list[Score]               # その上位を検証期間で回した結果
    hold_return_pct: float             # 同じ期間を持ちっぱなしにした場合
    skipped: str = ""                  # 対象外にした理由（あれば）
    daily_range_pct: float = 0.0       # 1 日の値幅の中央値（候補の足切りに使った値）
    tested: int = 0                    # 実際に試した候補数


def default_grid() -> list[Candidate]:
    """値幅の候補。README の推奨値（控えめ / 標準 / 大きめ）の周辺を埋める。"""
    grid: list[Candidate] = []
    for buy in (-1.0, -2.0, -3.0, -4.0, -6.0, -8.0):
        for sell in (1.0, 2.0, 3.0, 4.0, 6.0, 8.0):
            for stop_multiple in (1.5, 2.0, 3.0):
                grid.append(Candidate(buy, sell, round(buy * stop_multiple, 1)))
    return grid


def median_daily_range_pct(series: list[Bar], window: list[date]) -> float:
    """1 日の値幅（高値 − 安値）の中央値を、終値に対する % で返す。"""
    inside = [b for b in series if window[0] <= b.day <= window[-1] and b.close > 0]
    if not inside:
        return 0.0
    return statistics.median((b.high - b.low) / b.close * 100 for b in inside)


def wide_enough(grid: list[Candidate], daily_range_pct: float) -> list[Candidate]:
    """1 日の値動きに埋もれてしまう候補を落とす。

    日足は 4 点（寄り・高値・安値・引け）でしかなぞれないので、1 日の値幅に
    埋もれる細かい設定は、その 4 点の中で何度も売買が成立してしまい、
    「1 日に何往復もして儲かった／損した」という、日足からは確かめようのない
    結果になる。実際にそうなるかは分足を見ないと分からない。

    落とすのは次の 2 つ。どちらも「1 日の値幅より狭い」ことが条件。

      * 買いと売りの間隔  — 狭いと 1 日のうちに何往復もしたことになる
      * 買いと損切りの間隔 — 狭いと買った直後に日中の揺れで切られ続ける
    """
    kept = [
        c for c in grid
        if (c.sell_pct - c.buy_pct) >= daily_range_pct
        and (c.buy_pct - c.stop_pct) >= daily_range_pct
    ]
    return kept or grid


def split_days(days: list[date], holdout: int) -> tuple[list[date], list[date]]:
    """古い側を探索用、新しい側を検証用に割る。"""
    if holdout <= 0 or len(days) <= holdout:
        return days, []
    return days[:-holdout], days[-holdout:]


def segment_days(days: list[date], size: int) -> list[list[date]]:
    """営業日を size 日ずつの区間に切る（端数の最後の区間は捨てる）。"""
    if size <= 1:
        return [days] if days else []
    return [days[i:i + size] for i in range(0, len(days) - size + 1, size)]


def _reference_price(series: list[Bar], window: list[date]) -> float:
    """値幅の基準にする価格。期間が始まる直前の終値。

    その日に画面を見て「いまの株価を基準に上下何 % に線を引くか」を決める、
    という実際の使い方に合わせている。
    """
    before = [b for b in series if b.day < window[0]]
    if before:
        return before[-1].close
    return next(b.open for b in series if b.day == window[0])


def _score(
    config: Config, symbol: str, series: list[Bar], candidate: Candidate,
    segments: list[list[date]], quantity: int,
) -> Score:
    """区間ごとに線を引き直して回し、合算する。

    区間をまたいで持ち越さない（各区間は独立に始めて独立に終わる）。
    固定の値段で線を引く以上、基準にした株価から離れたら引き直すのが
    本来の使い方なので、それに合わせている。
    """
    score = Score(symbol=symbol, candidate=candidate,
                  reference=_reference_price(series, segments[0][0:1] and segments[0]),
                  quantity=quantity)
    for window in segments:
        reference = _reference_price(series, window)
        rule = candidate.to_rule("opt", symbol, reference, quantity)
        result = run_backtest(
            replace(config, rules=(rule,)), {symbol: series},
            days=None, since=window[0], until=window[-1],
        )
        profits = [t.profit for t in result.trades]
        segment_profit = sum(profits)

        score.segments += 1
        score.trades += len(profits)
        score.wins += len([p for p in profits if p > 0])
        score.realized += segment_profit
        score.held_at_end += len(result.open_positions)
        if profits:
            score.worst = min(score.worst, min(profits))
        if segment_profit > 0:
            score.profitable_segments += 1
        score.worst_segment = min(score.worst_segment, segment_profit)
    return score


def _hold_return_pct(series: list[Bar], window: list[date]) -> float:
    """同じ期間を持ちっぱなしにしたときの騰落率。"""
    bars = [b for b in series if window[0] <= b.day <= window[-1]]
    if len(bars) < 2:
        return 0.0
    return (bars[-1].close - bars[0].open) / bars[0].open * 100


def optimize_symbol(
    config: Config,
    symbol: str,
    series: list[Bar],
    *,
    holdout: int = 60,
    quantity: int = 100,
    grid: list[Candidate] | None = None,
    top: int = 3,
    segment: int = 20,
    min_trades: int = 5,
) -> SymbolReport:
    """1 銘柄ぶんの探索。探索期間で選び、検証期間で確かめる。"""
    grid = grid or default_grid()
    days = sorted({b.day for b in series})
    train_days, holdout_days = split_days(days, holdout)
    train_segments = segment_days(train_days, segment)
    holdout_segments = segment_days(holdout_days, segment)

    report = SymbolReport(symbol=symbol, reference=0.0, quantity=quantity,
                          train=[], holdout=[], hold_return_pct=0.0)
    if len(train_segments) < 3 or not holdout_segments:
        report.skipped = (
            f"データが足りません（探索 {len(train_segments)} 区間 / "
            f"検証 {len(holdout_segments)} 区間。1 区間 {segment} 営業日）"
        )
        return report

    # 表示用の基準価格は、検証期間の入口のもの（実際に線を引く場面に一番近い）。
    reference = _reference_price(series, holdout_segments[0])
    report.reference = reference

    # 単元株（100 株）の代金が 1 回あたりの上限を超えると、どの候補も
    # 発注できずに全滅する。探索する前に弾いて理由を出す。
    cap = config.risk.max_notional_per_order
    if reference * quantity > cap:
        report.skipped = (
            f"1 単元 {reference * quantity:,.0f} 円が 1 回あたりの上限 {cap:,.0f} 円を超えます"
            "（risk.max_notional_per_order を上げるか、対象から外してください）"
        )
        return report

    # 日足では検証できない細かい設定を落としてから総当たりする。
    report.daily_range_pct = median_daily_range_pct(series, train_days)
    grid = wide_enough(grid, report.daily_range_pct)
    report.tested = len(grid)

    scored = [_score(config, symbol, series, c, train_segments, quantity) for c in grid]
    # 数回しか売買していない候補は、たまたま当たっただけの可能性が高い。
    usable = [s for s in scored if s.trades >= min_trades] or scored
    # 「安定して」を優先する: まず儲かった区間の割合、次に合計。
    usable.sort(key=lambda s: (s.steady_pct, s.realized), reverse=True)
    report.train = usable[:top]

    report.holdout = [
        _score(config, symbol, series, s.candidate, holdout_segments, quantity)
        for s in report.train
    ]
    report.hold_return_pct = _hold_return_pct(series, holdout_days)
    return report


def optimize(
    config: Config,
    bars: dict[str, list[Bar]],
    *,
    holdout: int = 60,
    quantity: int = 100,
    top: int = 3,
    segment: int = 20,
) -> list[SymbolReport]:
    return [
        optimize_symbol(config, symbol, series, holdout=holdout,
                        quantity=quantity, top=top, segment=segment)
        for symbol, series in sorted(bars.items())
    ]
