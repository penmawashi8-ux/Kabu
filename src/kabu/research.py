"""売買手法そのものを比べる（値幅の調整ではなく、やり方の比較）。

`kabu optimize` は「固定ラインの往復売買」という 1 つのやり方の中で値幅を探す。
ここではやり方そのものを並べて、同じ期間・同じ資金・同じ執行ルールで比べる。

**公平に比べるための約束**

  * 判断は当日の終値まで、執行は翌日の寄り付き。当日の終値を見て当日の終値で
    売買する（未来を覗く）ことはしない
  * 使うパラメータは各手法の教科書的な既定値で固定する。この期間に合わせて
    調整はしない（調整するなら optimize と同じく検証期間の取り分けが要る）
  * 全手法とも同じ株数・同じ期間。最後に持っていれば最終日の終値で評価する
  * 持ちっぱなし（buy & hold）を必ず並べる

**ここで測れないもの**: 日足しか無いので、日中の値動きは扱えない。損切りは
翌日の寄りで実行するため、実際にはもっと不利な値段になることがある。
税金（利益の約 20.315%）は含めていない。
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import date

from .marketdata import Bar


@dataclass
class Position:
    entry_day: date
    entry_price: float
    exit_day: date | None = None
    exit_price: float | None = None

    @property
    def return_pct(self) -> float:
        if self.exit_price is None:
            return 0.0
        return (self.exit_price - self.entry_price) / self.entry_price * 100


@dataclass
class Outcome:
    """1 つの手法を 1 銘柄・1 期間で回した結果。"""

    name: str
    positions: list[Position] = field(default_factory=list)
    equity: list[float] = field(default_factory=list)   # 1 株あたりの評価額の推移
    exposure_days: int = 0
    total_days: int = 0

    @property
    def trades(self) -> int:
        return len([p for p in self.positions if p.exit_price is not None])

    @property
    def wins(self) -> int:
        return len([p for p in self.positions if p.exit_price is not None and p.return_pct > 0])

    @property
    def win_rate(self) -> float:
        return self.wins / self.trades * 100 if self.trades else 0.0

    @property
    def return_pct(self) -> float:
        """期間全体の損益率（複利ではなく、投下額に対する単純合計）。"""
        return sum(p.return_pct for p in self.positions)

    def net_return_pct(self, *, tax_pct: float = 20.315, slippage_pct: float = 0.05) -> float:
        """税金と滑りを引いた後の損益率。

        滑りは 1 回の売買につき買いと売りの 2 回分を引く。
        税金は特定口座に合わせ、同じ期間の損と益を通算した後の利益にだけかける
        （負けた年に課税されることはない）。売買が多い手法ほど不利に出るが、
        それが実際に起きることなので、比較はこの数字で行うべき。
        """
        nets = [p.return_pct - slippage_pct * 2 for p in self.positions if p.exit_price is not None]
        total = sum(nets)
        return total * (1 - tax_pct / 100) if total > 0 else total

    @property
    def max_drawdown_pct(self) -> float:
        """評価額の山からの最大下落率。「どれだけ含み損に耐える必要があったか」。"""
        peak = float("-inf")
        worst = 0.0
        for value in self.equity:
            peak = max(peak, value)
            if peak > 0:
                worst = min(worst, (value - peak) / peak * 100)
        return worst

    @property
    def exposure_pct(self) -> float:
        """期間のうち、実際に株を持っていた日の割合。"""
        return self.exposure_days / self.total_days * 100 if self.total_days else 0.0


# ------------------------------------------------------------------ 指標

def sma(values: list[float], period: int) -> list[float | None]:
    """単純移動平均。期間に満たない先頭は None。"""
    out: list[float | None] = []
    for i in range(len(values)):
        if i + 1 < period:
            out.append(None)
        else:
            out.append(sum(values[i + 1 - period:i + 1]) / period)
    return out


def rsi(values: list[float], period: int = 2) -> list[float | None]:
    """RSI。短期（2〜3）は逆張りの目安として広く使われている。"""
    out: list[float | None] = [None] * len(values)
    gains: list[float] = []
    losses: list[float] = []
    for i in range(1, len(values)):
        change = values[i] - values[i - 1]
        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))
        if len(gains) < period:
            continue
        avg_gain = sum(gains[-period:]) / period
        avg_loss = sum(losses[-period:]) / period
        if avg_loss == 0:
            out[i] = 100.0
        else:
            rs = avg_gain / avg_loss
            out[i] = 100 - 100 / (1 + rs)
    return out


def atr(bars: list[Bar], period: int = 14) -> list[float | None]:
    """平均的な 1 日の値幅（Average True Range）。ボラティリティの物差し。"""
    out: list[float | None] = [None] * len(bars)
    ranges: list[float] = []
    for i, bar in enumerate(bars):
        if i == 0:
            true_range = bar.high - bar.low
        else:
            prev_close = bars[i - 1].close
            true_range = max(
                bar.high - bar.low, abs(bar.high - prev_close), abs(bar.low - prev_close)
            )
        ranges.append(true_range)
        if len(ranges) >= period:
            out[i] = sum(ranges[-period:]) / period
    return out


# ------------------------------------------------------------------ 手法

# 各手法は「その日の終値までを見て、翌日の寄りで売買する」形に揃えてある。
# signal(i) は bars[i] の終値時点での判断。執行は bars[i + 1].open。


def _walk(bars: list[Bar], name: str, decide) -> Outcome:
    """共通の執行ループ。decide(i, holding) -> "buy" / "sell" / None。

    未来を覗かないよう、判断は bars[:i + 1] だけを見て行い、
    約定は必ず翌日の寄り付きで行う。
    """
    outcome = Outcome(name=name, total_days=len(bars))
    open_position: Position | None = None

    for i in range(len(bars) - 1):
        action = decide(i, open_position is not None)
        execution = bars[i + 1]

        if action == "buy" and open_position is None:
            open_position = Position(entry_day=execution.day, entry_price=execution.open)
            outcome.positions.append(open_position)
        elif action == "sell" and open_position is not None:
            open_position.exit_day = execution.day
            open_position.exit_price = execution.open
            open_position = None

        if open_position is not None:
            outcome.exposure_days += 1
            outcome.equity.append(execution.close / open_position.entry_price * 100)
        else:
            outcome.equity.append(outcome.equity[-1] if outcome.equity else 100.0)

    # 最終日に持っていたら、その日の終値で評価する（塩漬けを勝ちに見せない）。
    if open_position is not None:
        open_position.exit_day = bars[-1].day
        open_position.exit_price = bars[-1].close
    return outcome


def buy_and_hold(bars: list[Bar]) -> Outcome:
    """初日に買って最終日まで持つ。比較の基準。"""
    outcome = Outcome(name="持ちっぱなし", total_days=len(bars))
    if len(bars) < 2:
        return outcome
    position = Position(entry_day=bars[1].day, entry_price=bars[1].open,
                        exit_day=bars[-1].day, exit_price=bars[-1].close)
    outcome.positions.append(position)
    outcome.exposure_days = len(bars) - 1
    outcome.equity = [b.close / position.entry_price * 100 for b in bars[1:]]
    return outcome


def fixed_band(bars: list[Bar], *, buy_pct=-2.0, sell_pct=1.0, stop_pct=-6.0) -> Outcome:
    """いまのボットのやり方。期間の入口の終値を基準に、固定の値段で線を引く。"""
    if not bars:
        return Outcome(name="固定ライン（現行）", total_days=0)
    reference = bars[0].close
    buy_at = reference * (1 + buy_pct / 100)
    sell_at = reference * (1 + sell_pct / 100)
    stop_at = reference * (1 + stop_pct / 100)

    def decide(i: int, holding: bool) -> str | None:
        price = bars[i].close
        if not holding:
            # 損切りラインも割っている値段では買わない（買った瞬間に切られるため）。
            return "buy" if stop_at < price <= buy_at else None
        return "sell" if price >= sell_at or price <= stop_at else None

    return _walk(bars, "固定ライン（現行）", decide)


def sma_trend(bars: list[Bar], *, fast=25, slow=75) -> Outcome:
    """移動平均のトレンドフォロー。上向きの間だけ持ち、下向いたら降りる。

    「上がっているものを持ち、下がったら降りる」。利確の上限を設けないので、
    伸びる相場ではそのまま乗り続ける。固定ラインとは逆の発想。
    """
    closes = [b.close for b in bars]
    fast_line, slow_line = sma(closes, fast), sma(closes, slow)

    def decide(i: int, holding: bool) -> str | None:
        f, s = fast_line[i], slow_line[i]
        if f is None or s is None:
            return None
        if not holding:
            return "buy" if f > s else None
        return "sell" if f < s else None

    return _walk(bars, f"移動平均トレンド（{fast}/{slow}）", decide)


def rsi_reversion(bars: list[Bar], *, period=2, entry=10.0, exit_level=60.0, trend=200) -> Outcome:
    """短期の下げを拾う逆張り。ただし長期の上昇トレンド中に限る。

    固定ラインの逆張りとの違いは、(1) 下げ幅ではなく「下げの勢い」で測るため
    ボラティリティに自動で追随する、(2) 長期の移動平均を下回る局面では買わない
    （落ちるナイフを掴まない）の 2 点。
    """
    closes = [b.close for b in bars]
    strength = rsi(closes, period)
    trend_line = sma(closes, trend)

    def decide(i: int, holding: bool) -> str | None:
        value = strength[i]
        if value is None:
            return None
        if not holding:
            # 長期線がまだ計算できない序盤は買わない。フィルタが無い期間だけ
            # 素通りさせると、一番危ない「下げの初期」で買うことになる。
            above_trend = trend_line[i] is not None and closes[i] > trend_line[i]
            return "buy" if value <= entry and above_trend else None
        return "sell" if value >= exit_level else None

    return _walk(bars, f"RSI({period}) 逆張り + {trend}日線フィルタ", decide)


def atr_band(bars: list[Bar], *, period=14, buy_mult=1.0, sell_mult=2.0, stop_mult=2.0) -> Outcome:
    """値幅をボラティリティ（ATR）に合わせて動かす、固定ラインの改良版。

    固定ラインの弱点は「その銘柄が普段どれだけ動くか」を無視している点。
    ATR の何倍という決め方にすると、動きの荒い銘柄では自動で幅が広がる。
    """
    ranges = atr(bars, period)
    closes = [b.close for b in bars]

    entry_price: list[float | None] = [None] * len(bars)

    def decide(i: int, holding: bool) -> str | None:
        band = ranges[i]
        if band is None:
            return None
        if not holding:
            # 直近 5 日の高値から ATR × buy_mult 下げたら押し目とみなす。
            recent_high = max(closes[max(0, i - 4):i + 1])
            if closes[i] <= recent_high - band * buy_mult:
                entry_price[i] = closes[i]
                return "buy"
            return None
        # 保有中は、買った値段から ATR の何倍動いたかで決める。
        bought = next((p for p in reversed(entry_price) if p is not None), None)
        if bought is None:
            return None
        if closes[i] >= bought + band * sell_mult:
            return "sell"
        if closes[i] <= bought - band * stop_mult:
            return "sell"
        return None

    return _walk(bars, f"ATR({period}) 連動バンド", decide)


def trailing_stop(bars: list[Bar], *, fast=25, slow=75, trail_pct=8.0) -> Outcome:
    """トレンドで入って、高値から一定 % 下げたら降りる（利確の上限を作らない）。

    固定ラインは「+1% で利確、-4% で損切り」のように、**利益は小さく切り、損失は
    大きく許す**形になりがち。トレーリングストップはその逆で、伸びるだけ伸ばす。
    """
    closes = [b.close for b in bars]
    fast_line, slow_line = sma(closes, fast), sma(closes, slow)
    peak: list[float] = [0.0]

    def decide(i: int, holding: bool) -> str | None:
        f, s = fast_line[i], slow_line[i]
        if not holding:
            peak[0] = 0.0
            if f is None or s is None:
                return None
            return "buy" if f > s else None
        peak[0] = max(peak[0], closes[i])
        if peak[0] > 0 and closes[i] <= peak[0] * (1 - trail_pct / 100):
            return "sell"
        return None

    return _walk(bars, f"トレンド + トレーリングストップ（-{trail_pct:g}%）", decide)


STRATEGIES = (buy_and_hold, fixed_band, sma_trend, rsi_reversion, atr_band, trailing_stop)


def compare(bars: list[Bar]) -> list[Outcome]:
    """全手法を同じ期間で回して結果を並べる。"""
    return [strategy(bars) for strategy in STRATEGIES]


def split_periods(bars: list[Bar], count: int) -> list[list[Bar]]:
    """期間を count 等分する。

    1 つの期間の成績だけでは、その相場つきに合っていただけなのか、
    どんな相場でも通用するのかが分からない。上げ相場・下げ相場を
    またいで見るために、期間を割って別々に測る。
    """
    if count <= 1 or len(bars) < count:
        return [bars]
    size = len(bars) // count
    return [bars[i * size:(i + 1) * size] for i in range(count)]


def summarize(outcomes: list[Outcome]) -> dict[str, float]:
    """比較しやすいように、全体の中央値などを返す。"""
    returns = [o.return_pct for o in outcomes]
    return {"median_return": statistics.median(returns) if returns else 0.0}

def random_trades(bars: list[Bar], *, trades: int = 20, hold_days: int = 10, seed: int = 0) -> Outcome:
    """でたらめに売買する。**手法に意味があるかを確かめるための対照実験。**

    同じ回数・同じ保有日数でランダムに売買したときの成績。ある手法がこれに
    勝てないなら、勝っていたのは「判断」ではなく「株式市場に居た時間」で
    説明がつく、ということになる（薬の試験でいう偽薬）。
    """
    import random

    rng = random.Random(seed)
    entries = sorted(rng.sample(range(len(bars) - 1), min(trades, max(1, len(bars) - 2))))
    schedule = {i: False for i in entries}
    held_until = [0]

    def decide(i: int, holding: bool) -> str | None:
        if not holding:
            return "buy" if i in schedule else None
        if i >= held_until[0]:
            return "sell"
        return None

    outcome = Outcome(name=f"ランダム売買（{trades}回・{hold_days}日保有）", total_days=len(bars))
    position: Position | None = None
    for i in range(len(bars) - 1):
        execution = bars[i + 1]
        if position is None and i in schedule:
            position = Position(entry_day=execution.day, entry_price=execution.open)
            outcome.positions.append(position)
            held_until[0] = i + hold_days
        elif position is not None and i >= held_until[0]:
            position.exit_day = execution.day
            position.exit_price = execution.open
            position = None
        if position is not None:
            outcome.exposure_days += 1
            outcome.equity.append(execution.close / position.entry_price * 100)
        else:
            outcome.equity.append(outcome.equity[-1] if outcome.equity else 100.0)
    if position is not None:
        position.exit_day = bars[-1].day
        position.exit_price = bars[-1].close
    return outcome


def atr_variants() -> list[tuple[str, dict]]:
    """ATR 連動バンドのパラメータをずらした組み合わせ。

    少し動かしただけで結果が崩れるなら、その成績はたまたま噛み合っただけ。
    どれで回しても同じ傾向なら、手法そのものの性質と考えてよい。
    """
    out: list[tuple[str, dict]] = []
    for period in (10, 14, 20):
        for buy_mult in (0.5, 1.0, 1.5):
            for sell_mult in (1.5, 2.0, 3.0):
                out.append((
                    f"ATR({period}) 買{buy_mult:g}/売{sell_mult:g}",
                    dict(period=period, buy_mult=buy_mult, sell_mult=sell_mult, stop_mult=2.0),
                ))
    return out
