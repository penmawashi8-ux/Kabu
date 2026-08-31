"""実データの解析と再生。

取得そのもの（ネットワーク）はテストしない。壊れやすいのは解析側なので、
実際の提供元が返す形をそのまま書き写した文字列で固定している。
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta

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


# --------------------------------------------------- 現在値（遅延あり）の取得

YAHOO_QUOTE = json.dumps({
    "chart": {"result": [{
        "meta": {"symbol": "7203.T", "currency": "JPY",
                 "regularMarketPrice": 2934.5, "regularMarketTime": 1788150000},
    }], "error": None}
})

STOOQ_QUOTE = (
    "Symbol,Date,Time,Open,High,Low,Close,Volume\n"
    "7203.JP,2026-08-31,15:00:00,2900,2960,2880,2934.5,12345600\n"
)


def test_parses_yahoo_current_price():
    from kabu.marketdata import parse_yahoo_quote

    price, asof = parse_yahoo_quote(YAHOO_QUOTE)
    assert price == 2934.5
    assert asof is not None


def test_yahoo_quote_without_a_price_is_an_error():
    from kabu.marketdata import parse_yahoo_quote

    payload = json.dumps({"chart": {"result": [{"meta": {"symbol": "9999.T"}}]}})
    with pytest.raises(MarketDataError, match="現在値"):
        parse_yahoo_quote(payload)


def test_parses_stooq_current_price():
    from kabu.marketdata import parse_stooq_quote

    price, asof = parse_stooq_quote(STOOQ_QUOTE)
    assert price == 2934.5
    assert asof == datetime(2026, 8, 31, 15, 0, 0)


def test_stooq_quote_with_no_data_is_an_error():
    from kabu.marketdata import parse_stooq_quote

    text = "Symbol,Date,Time,Open,High,Low,Close,Volume\n7203.JP,N/D,N/D,N/D,N/D,N/D,N/D,N/D\n"
    with pytest.raises(MarketDataError):
        parse_stooq_quote(text)


class FakeQuotes:
    """fetch_quote を差し替えて、取得の成否を手で決められるようにする。"""

    def __init__(self, prices: dict[str, float]) -> None:
        self.prices = prices
        self.calls = 0
        self.fail: set[str] = set()

    def __call__(self, symbol, *, provider="yahoo"):
        self.calls += 1
        if symbol in self.fail:
            raise MarketDataError("取得できません")
        return self.prices[symbol], datetime(2026, 8, 31, 14, 45)


def live_feed(monkeypatch, prices, **kwargs):
    from kabu import marketdata

    fake = FakeQuotes(prices)
    monkeypatch.setattr(marketdata, "fetch_quote", fake)
    feed = marketdata.LiveFeed(list(prices), **kwargs)
    return feed, fake


def test_live_feed_serves_the_fetched_price(monkeypatch):
    feed, _ = live_feed(monkeypatch, {"7203.T": 2934.5})
    feed.prime()
    assert feed.read_quotes(["7203.T"]) == {"7203.T": 2934.5}
    assert feed.manual_allowed is False


def test_live_feed_reports_itself_as_delayed(monkeypatch):
    feed, _ = live_feed(monkeypatch, {"7203.T": 2934.5})
    feed.prime()
    info = feed.describe()
    assert info["kind"] == "delayed"
    assert "遅延" in info["label"]
    assert info["asof"] == "14:45"


def test_live_feed_cannot_be_moved_by_hand(monkeypatch):
    feed, _ = live_feed(monkeypatch, {"7203.T": 2934.5})
    feed.prime()
    feed.set_price("7203.T", 1)
    assert feed.read_quotes(["7203.T"]) == {"7203.T": 2934.5}


def test_live_feed_prime_fails_loudly_when_nothing_can_be_fetched(monkeypatch):
    feed, fake = live_feed(monkeypatch, {"7203.T": 2934.5})
    fake.fail = {"7203.T"}
    with pytest.raises(MarketDataError):
        feed.prime()


def test_live_feed_keeps_the_last_price_when_a_refresh_fails(monkeypatch):
    """一時的に取得できなくても、直前の値段を保って動き続ける。"""
    feed, fake = live_feed(monkeypatch, {"7203.T": 2934.5})
    feed.prime()
    fake.fail = {"7203.T"}
    feed.set_auto(True)                       # 押した瞬間に取り直す → 失敗する
    assert feed.read_quotes(["7203.T"]) == {"7203.T": 2934.5}
    assert "取得できません" in feed.describe()["span"]


def test_live_feed_refuses_a_hammering_interval(monkeypatch):
    feed, _ = live_feed(monkeypatch, {"7203.T": 2934.5}, interval=0.1)
    assert feed._interval >= 10      # 取得元に連打をかけない


def test_live_feed_starts_paused(monkeypatch):
    feed, _ = live_feed(monkeypatch, {"7203.T": 2934.5})
    assert feed.auto is False


def test_live_feed_updates_only_the_symbols_it_could_fetch(monkeypatch):
    feed, fake = live_feed(monkeypatch, {"7203.T": 2934.5, "6758.T": 3400.0})
    feed.prime()
    fake.prices["7203.T"] = 3000.0
    fake.fail = {"6758.T"}
    feed.set_auto(True)
    quotes = feed.read_quotes(["7203.T", "6758.T"])
    assert quotes["7203.T"] == 3000.0     # 取れたほうは更新される
    assert quotes["6758.T"] == 3400.0     # 取れなかったほうは据え置き


# ------------------------------------------- 取得元が CSV を返さなかったとき

def test_stooq_rate_limit_message_is_surfaced():
    """CSV のつもりが制限メッセージだった、を利用者がその場で判断できること。"""
    from kabu.marketdata import MarketDataError, parse_stooq_csv

    with pytest.raises(MarketDataError) as exc:
        parse_stooq_csv("Exceeded the daily hits limit\n")
    assert "Exceeded the daily hits limit" in str(exc.value)


def test_stooq_html_response_is_surfaced():
    from kabu.marketdata import MarketDataError, parse_stooq_csv

    with pytest.raises(MarketDataError) as exc:
        parse_stooq_csv("<!doctype html>\n<html><body>Please log in</body></html>")
    assert "html" in str(exc.value).lower()


def test_yahoo_error_payload_is_surfaced():
    from kabu.marketdata import MarketDataError, parse_yahoo_json

    with pytest.raises(MarketDataError) as exc:
        parse_yahoo_json('{"chart":{"result":null,"error":{"code":"Not Found"}}}')
    assert "Not Found" in str(exc.value)


def test_snippet_keeps_errors_readable():
    """長い応答でも 1 行に丸めて出す（ログが埋まらないように）。"""
    from kabu.marketdata import _snippet

    out = _snippet("a\n" * 500)
    assert len(out) <= 210
    assert "\n" not in out
