"""実データの解析と再生。

取得そのもの（ネットワーク）はテストしない。壊れやすいのは解析側なので、
実際の提供元が返す形をそのまま書き写した文字列で固定している。
"""

from __future__ import annotations

import json
from datetime import date, timedelta

import pytest

from kabu.marketdata import (
    Bar,
    MarketDataError,
    ReplayFeed,
    parse_stooq_csv,
    parse_yahoo_json,
)

STOOQ_CSV = """Date,Open,High,Low,Close,Volume
2026-08-26,2600,2655,2590,2640,12345600
2026-08-27,2645,2700,2630,2688,9876500
2026-08-28,2690,2712,2601,2610,11223300
"""

YAHOO_JSON = json.dumps({
    "chart": {
        "result": [{
            "meta": {"symbol": "7203.T", "currency": "JPY"},
            "timestamp": [1787000000, 1787086400, 1787172800],
            "indicators": {"quote": [{
                "open":  [2600, 2645, None],
                "high":  [2655, 2700, None],
                "low":   [2590, 2630, None],
                "close": [2640, 2688, None],
            }]},
        }],
        "error": None,
    }
})


def test_parses_stooq_daily_csv():
    bars = parse_stooq_csv(STOOQ_CSV)
    assert len(bars) == 3
    assert bars[0].day == date(2026, 8, 26)
    assert bars[0].open == 2600
    assert bars[0].high == 2655
    assert bars[-1].close == 2610


def test_stooq_rows_are_sorted_by_date():
    shuffled = "\n".join([
        "Date,Open,High,Low,Close,Volume",
        "2026-08-28,2690,2712,2601,2610,1",
        "2026-08-26,2600,2655,2590,2640,1",
    ])
    bars = parse_stooq_csv(shuffled)
    assert [b.day.day for b in bars] == [26, 28]


def test_stooq_skips_unparseable_rows():
    """存在しない銘柄では N/D が混ざる。読めた行だけ使う。"""
    text = STOOQ_CSV + "2026-08-31,N/D,N/D,N/D,N/D,N/D\n"
    assert len(parse_stooq_csv(text)) == 3


def test_stooq_empty_response_is_an_error():
    with pytest.raises(MarketDataError):
        parse_stooq_csv("")


def test_stooq_all_rows_bad_is_an_error():
    with pytest.raises(MarketDataError, match="1 日ぶんも"):
        parse_stooq_csv("Date,Open,High,Low,Close,Volume\n2026-08-31,N/D,N/D,N/D,N/D,N/D\n")


def test_parses_yahoo_chart_json():
    bars = parse_yahoo_json(YAHOO_JSON)
    assert len(bars) == 2          # 3 日目は値が None（立会なし）なので落ちる
    assert bars[0].open == 2600
    assert bars[1].close == 2688


def test_yahoo_unexpected_shape_is_an_error():
    with pytest.raises(MarketDataError, match="形式"):
        parse_yahoo_json('{"chart": {"result": []}}')


def test_yahoo_broken_json_is_an_error():
    with pytest.raises(MarketDataError):
        parse_yahoo_json("<html>error</html>")


# ------------------------------------------------------------------- 再生

BASE = date(2026, 8, 1)


def bars(*closes: float) -> list[Bar]:
    return [
        Bar(day=BASE + timedelta(days=i), open=c, high=c + 10, low=c - 10, close=c)
        for i, c in enumerate(closes)
    ]


def test_walk_visits_the_days_four_prices():
    bar = Bar(day=date(2026, 8, 26), open=2600, high=2655, low=2590, close=2640)
    assert bar.walk() == [2600, 2655, 2590, 2640]


def test_replay_holds_still_until_played():
    feed = ReplayFeed({"7203.T": bars(2600, 2700)}, speed=1)
    first = feed.read_quotes(["7203.T"])
    assert feed.read_quotes(["7203.T"]) == first   # 再生していないので動かない


def test_replay_advances_through_the_real_prices():
    feed = ReplayFeed({"7203.T": bars(2600, 2700)}, speed=1)
    feed.set_auto(True)
    seen = [feed.read_quotes(["7203.T"])["7203.T"] for _ in range(8)]
    # 寄り→高値→安値→引け を日ごとになぞる
    assert 2610 in seen and 2590 in seen and 2700 in seen


def test_replay_speed_skips_ahead():
    slow = ReplayFeed({"7203.T": bars(*range(2600, 2640))}, speed=1)
    fast = ReplayFeed({"7203.T": bars(*range(2600, 2640))}, speed=8)
    slow.set_auto(True)
    fast.set_auto(True)
    for _ in range(5):
        slow.read_quotes(["7203.T"])
        fast.read_quotes(["7203.T"])
    assert fast.describe()["progress"] > slow.describe()["progress"]


def test_replay_wraps_around_at_the_end():
    feed = ReplayFeed({"7203.T": bars(2600, 2700)}, speed=3)
    feed.set_auto(True)
    for _ in range(40):
        quotes = feed.read_quotes(["7203.T"])
        assert quotes["7203.T"] > 0     # 端を超えても落ちない


def test_replay_ignores_manual_price_changes():
    feed = ReplayFeed({"7203.T": bars(2600)}, speed=1)
    before = feed.read_quotes(["7203.T"])
    feed.set_price("7203.T", 9999)
    assert feed.read_quotes(["7203.T"]) == before
    assert feed.manual_allowed is False


def test_replay_reports_the_date_being_shown():
    feed = ReplayFeed({"7203.T": bars(2600, 2700)}, speed=1)
    info = feed.describe()
    assert info["asof"] == "2026-08-01"
    assert info["span"] == "2026-08-01 〜 2026-08-02"


def test_replay_reset_returns_to_the_start():
    feed = ReplayFeed({"7203.T": bars(2600, 2700, 2800)}, speed=2)
    feed.set_auto(True)
    for _ in range(3):
        feed.read_quotes(["7203.T"])
    feed.reset()
    assert feed.describe()["progress"] == 0.0


def test_replay_handles_a_symbol_with_no_data():
    feed = ReplayFeed({"7203.T": bars(2600)}, speed=1)
    assert feed.read_quotes(["6758.T"]) == {}
