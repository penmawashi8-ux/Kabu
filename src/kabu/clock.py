"""立会時間の判定。

「取引時間外なのに発注してしまう」事故を防ぐ最初の砦。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from .config import MarketConfig


class MarketClock:
    def __init__(self, config: MarketConfig) -> None:
        self._config = config
        self._tz = ZoneInfo(config.timezone)
        self._holidays = frozenset(config.holidays)

    @property
    def timezone(self) -> ZoneInfo:
        return self._tz

    def now(self) -> datetime:
        return datetime.now(self._tz)

    def is_holiday(self, day: date) -> bool:
        # 土日 + 設定ファイルに書かれた祝日・年末年始。
        return day.weekday() >= 5 or day.isoformat() in self._holidays

    def is_open(self, at: datetime | None = None) -> bool:
        at = at or self.now()
        if self.is_holiday(at.date()):
            return False
        t = at.time()
        return any(start <= t < end for start, end in self._config.sessions)

    def accepts_new_entry(self, at: datetime | None = None) -> bool:
        """新規建て（買い）を出してよい時間か。引け間際は入らない。"""
        at = at or self.now()
        if not self.is_open(at):
            return False
        return at.time() < self._config.trade_cutoff

    def should_close_positions(self, at: datetime | None = None) -> bool:
        """保有中のものを強制手仕舞いする時刻を過ぎたか。"""
        if self._config.close_positions_at is None:
            return False
        at = at or self.now()
        if self.is_holiday(at.date()):
            return False
        return at.time() >= self._config.close_positions_at and self.is_open(at)

    def seconds_until_open(self, at: datetime | None = None) -> float:
        """次のセッション開始までの秒数。閉場中のスリープ時間を決めるのに使う。"""
        at = at or self.now()
        t = at.time()
        if not self.is_holiday(at.date()):
            for start, _end in self._config.sessions:
                if t < start:
                    today_start = at.replace(
                        hour=start.hour, minute=start.minute, second=0, microsecond=0
                    )
                    return max(0.0, (today_start - at).total_seconds())
        # 当日のセッションは終了。翌日以降の最初の営業日まで待つ。
        first = self._config.sessions[0][0]
        midnight = at.replace(hour=0, minute=0, second=0, microsecond=0)
        for offset in range(1, 15):
            day = midnight + timedelta(days=offset)
            if not self.is_holiday(day.date()):
                nxt = day.replace(hour=first.hour, minute=first.minute)
                return max(0.0, (nxt - at).total_seconds())
        return 3600.0
