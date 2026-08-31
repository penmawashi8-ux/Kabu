"""コマンドライン入口。

    kabu doctor      接続と設定の健康診断（発注はしない）
    kabu init-sheet  RSS 用の Excel ブックの雛形を作る
    kabu status      現在の状態を表示する
    kabu run         売買ループを開始する
    kabu backtest    過去の実データでルールを検証する（発注しない）
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from .broker import Broker, PaperBroker
from .clock import MarketClock
from .config import Config, load_config
from .errors import KabuError
from .logging_setup import setup_logging
from .runner import Runner

log = logging.getLogger(__name__)


def _build_broker_and_quotes(config: Config) -> tuple[Broker, object]:
    """モードに応じて発注経路と株価取得元を組み立てる。"""
    if config.is_live:
        from .rss import ExcelBridge, RssBroker  # Windows 専用なので遅延 import

        bridge = ExcelBridge(config)
        symbols = sorted({r.symbol for r in config.rules})
        bridge.build_quote_sheet(symbols)
        return RssBroker(config, bridge), bridge.read_quotes

    # ペーパーモードでも株価は本物を使いたい。Excel があれば読み、無ければ手入力ファイル。
    try:
        from .rss import ExcelBridge

        bridge = ExcelBridge(config)
        bridge.build_quote_sheet(sorted({r.symbol for r in config.rules}))
        log.info("ペーパートレード: 株価は Excel(RSS) から取得、発注はシミュレーションです")
        return PaperBroker(), bridge.read_quotes
    except KabuError as exc:
        log.warning("Excel に接続できないため擬似株価で動かします: %s", exc)
        from .simfeed import SimulatedFeed

        return PaperBroker(), SimulatedFeed(config).read_quotes


def _nisa_warning(config: Config) -> str:
    """NISA 枠で回転売買をしようとしていたら警告する。

    このボットは「買って売ってを繰り返す」「損切りする」前提で作ってある。
    どちらも NISA とは相性が悪いので、気づかないまま実弾に進ませない。
    """
    account = str(config.rss.order_defaults.get("account_type", ""))
    if "NISA" not in account.upper() and "ニーサ" not in account:
        return ""
    return (
        f" 口座区分が「{account}」になっています。NISA 枠での自動売買には次の不利があります:\n"
        "   * 売買を繰り返すと年間の投資枠をすぐ使い切ります（売っても枠が戻るのは翌年）\n"
        "   * 損益通算・繰越控除ができないため、損切りしても税金面の救済がありません\n"
        "   本ボットは繰り返しと損切りを前提にしています。特定口座を勧めます。"
    )


def cmd_run(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    setup_logging(config.log_file, verbose=args.verbose, timezone=config.market.timezone)

    if config.is_live:
        print("=" * 68)
        print(" 実弾モードです。実際に楽天証券へ注文が送信されます。")
        warning = _nisa_warning(config)
        if warning:
            print("-" * 68)
            print(warning)
            print("-" * 68)
        print(f" ルール {len(config.rules)} 件 / 1 回あたり上限 "
              f"{config.risk.max_notional_per_order:,.0f} 円 / "
              f"1 日 {config.risk.max_orders_per_day} 件まで")
        print("=" * 68)
        if not args.yes:
            answer = input("続行するには 'LIVE' と入力してください: ").strip()
            if answer != "LIVE":
                print("中止しました。")
                return 1

    broker, quote_source = _build_broker_and_quotes(config)
    runner = Runner(config, broker, quote_source)  # type: ignore[arg-type]
    runner.run_forever()
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    setup_logging(config.log_file, timezone=config.market.timezone)
    runner = Runner(config, PaperBroker(), lambda _symbols: {})
    for line in runner.status_lines():
        print(line)
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    setup_logging(config.log_file, verbose=args.verbose, timezone=config.market.timezone)
    clock = MarketClock(config.market)
    now = clock.now()

    print(f"設定ファイル      : OK ({args.config})")
    print(f"モード            : {config.mode}")
    print(f"ルール            : {len(config.rules)} 件")
    print(f"現在時刻          : {now:%Y-%m-%d %H:%M:%S %Z}")
    print(f"立会中か          : {'はい' if clock.is_open(now) else 'いいえ'}")
    print(f"新規建て可能か    : {'はい' if clock.accepts_new_entry(now) else 'いいえ'}")
    print(f"停止ファイル      : {config.risk.kill_switch_file} "
          f"({'存在します（起動しません）' if Path(config.risk.kill_switch_file).exists() else '無し'})")

    # RSS アドインは Excel のビット数に合うものを登録しないと読み込めない。
    # 一番よく間違えるところなので、こちらで判定して名指しする。
    from .excelinfo import describe_addin_setup

    for line in describe_addin_setup():
        print(line)

    # 株価取得と発注は別物なので、別々に判定する。株価だけ使いたい場合
    # （--source rss でシミュレーションするだけ）に、注文シートが未整備でも
    # 「NG」と出て混乱しないようにする。
    symbols = sorted({r.symbol for r in config.rules})
    try:
        from .rss import ExcelBridge

        bridge = ExcelBridge(config)
        bridge.build_quote_sheet(symbols)
        quotes = bridge.read_quotes(symbols)
    except KabuError as exc:
        print(f"株価の取得        : NG — {exc}")
        return 1 if config.is_live else 0

    print("株価の取得        : OK（リアルタイム）")
    for symbol in symbols:
        price = quotes.get(symbol)
        print(f"  {symbol:>10} : {price if price is not None else '取得できず'}")

    try:
        orders = bridge.read_order_table()
        print(f"注文一覧シート    : OK（{len(orders)} 行）")
    except KabuError as exc:
        print(f"注文一覧シート    : 未整備 — {exc}")
        if config.is_live:
            print("  実弾モードには注文一覧シートが必要です（README の「Excel ブックの用意」）。")
            return 1
        print("  株価を見るだけなら、このままで問題ありません。")
    return 0


def cmd_play(args: argparse.Namespace) -> int:
    """ブラウザで見えるシミュレーター。実弾モードの設定でも必ず paper に落とす。"""
    from dataclasses import replace
    from datetime import time as dtime

    from .webui import serve

    config = load_config(args.config)
    setup_logging(config.log_file, verbose=args.verbose, timezone=config.market.timezone)

    # シミュレーターは時間帯に関係なく遊べるようにする（実弾では絶対にしない）。
    market = config.market
    if args.anytime:
        market = replace(
            market,
            sessions=((dtime(0, 0), dtime(23, 59)),),
            trade_cutoff=dtime(23, 58),
            close_positions_at=None,
            holidays=(),
        )

    config = replace(
        config,
        mode="paper",
        market=market,
        state_file=args.state,
        journal_file=args.journal,
    )
    factory, banner = _price_source(config, args)
    serve(
        config, host=args.host, port=args.port, feed_factory=factory, banner=banner,
        config_path=Path(args.config),
    )
    return 0


def _price_source(config: Config, args: argparse.Namespace):
    """株価をどこから取るかを決める。取れなければ擬似株価に落とす。"""
    from .webui import GameFeed

    if args.source == "sim":
        return (lambda cfg: GameFeed(cfg)), "株価: 擬似（実際の値段ではありません）"

    if args.source == "rss":
        # 実弾と同じ経路。マーケットスピードII にログインしていれば本物の
        # リアルタイム株価が流れてくる。発注だけはシミュレーションのまま。
        import threading

        from .rss import ExcelBridge

        # 起動時の疎通確認と QUOTES シートの用意はここ（メインスレッド）で済ませ、
        # 繋がらなければサーバを立てる前に落とす。
        ExcelBridge(config).build_quote_sheet(sorted({r.symbol for r in config.rules}))

        class _RssFeed:
            auto = True
            manual_allowed = False

            def __init__(self) -> None:
                # COM の接続はスレッドをまたいで使い回せない。売買ループは
                # 別スレッドで回るので、実際に読むスレッドごとに繋ぎ直す。
                self._local = threading.local()

            def _bridge(self) -> ExcelBridge:
                bridge = getattr(self._local, "bridge", None)
                if bridge is None:
                    bridge = ExcelBridge(config)
                    self._local.bridge = bridge
                return bridge

            def set_auto(self, flag): pass
            def set_price(self, symbol, price): pass
            def reset(self): pass
            def read_quotes(self, symbols): return self._bridge().read_quotes(symbols)
            def describe(self):
                return {"kind": "rss", "label": "マーケットスピードII RSS（リアルタイム）",
                        "asof": None, "span": "", "progress": 0.0}

        return (lambda cfg: _RssFeed()), "株価: マーケットスピードII RSS のリアルタイム値"

    if args.source == "delayed":
        # 無料で取れる「いまの値段」。取引所の生値ではなく 20 分ほど遅れた配信。
        from .marketdata import LiveFeed, MarketDataError

        symbols = sorted({r.symbol for r in config.rules})
        provider = "yahoo" if args.provider == "stooq" else args.provider
        print(f"現在値を取得しています（{provider}）… {', '.join(symbols)}")
        feed = LiveFeed(symbols, provider=provider, interval=args.poll_live)
        try:
            feed.prime()
        except MarketDataError as exc:
            print(f"\n現在値を取得できませんでした:\n  {exc}\n")
            print("擬似株価に切り替えて起動します。"
                  "（リアルタイムが必要なら --source rss を使ってください）\n")
            return (lambda cfg: GameFeed(cfg)), "株価: 擬似（現在値の取得に失敗）"
        feed.start()
        got = feed.read_quotes(symbols)
        detail = " / ".join(f"{s} {p:,.1f}" for s, p in got.items())
        return (lambda cfg: feed), f"株価: 実際の値段（遅延・{provider}） {detail}"

    # args.source == "real"
    from .marketdata import MarketDataError, ReplayFeed, fetch_many

    symbols = sorted({r.symbol for r in config.rules})
    print(f"実際の株価を取得しています（{args.provider}）… {', '.join(symbols)}")
    try:
        bars = fetch_many(symbols, provider=args.provider, cache_dir=args.cache_dir)
    except MarketDataError as exc:
        print(f"\n実データを取得できませんでした:\n  {exc}\n")
        print("擬似株価に切り替えて起動します。"
              "（社内ネットワークやプロキシで外に出られない場合は "
              "--source rss か --source sim を使ってください）\n")
        return (lambda cfg: GameFeed(cfg)), "株価: 擬似（実データの取得に失敗）"

    span = ""
    for series in bars.values():
        if series:
            span = f"{series[0].day:%Y-%m-%d} 〜 {series[-1].day:%Y-%m-%d}"
            break
    feed = ReplayFeed(bars, speed=args.speed)
    return (lambda cfg: feed), f"株価: 実データ再生 {span}（{args.provider}・日足）"


def cmd_init_sheet(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    from .sheet_template import write_template

    path = write_template(config)
    print(f"雛形を作成しました: {path}")
    print("Excel で開き、ORDERS シートの A1 に RSS の注文一覧関数を入力してください。")
    print("（README の「注文一覧シートの用意」を参照）")
    return 0


def cmd_backtest(args: argparse.Namespace) -> int:
    """過去の実データで「このルールならどうなっていたか」を確かめる（発注しない）。"""
    from datetime import date as _date

    from .backtest import run_backtest
    from .marketdata import MarketDataError, fetch_many

    config = load_config(args.config)
    setup_logging(config.log_file, verbose=args.verbose, timezone=config.market.timezone)

    symbols = sorted({r.symbol for r in config.rules})
    # 片方の取得元が制限をかけていることがあるので、駄目ならもう片方を試す。
    order = [args.provider] + [p for p in ("yahoo", "stooq") if p != args.provider]
    bars = None
    for i, provider in enumerate(order):
        print(f"実データを取得しています（{provider}）… {', '.join(symbols)}")
        try:
            bars = fetch_many(symbols, provider=provider, cache_dir=args.cache_dir)
            break
        except MarketDataError as exc:
            print(f"\n{provider} からは取得できませんでした:\n  {exc}\n")
            if i + 1 < len(order):
                print(f"{order[i + 1]} で取り直します。\n")
    if bars is None:
        print("どの取得元からも実データを取得できませんでした。"
              "ネットワーク（社内プロキシなど）を確認してください。")
        return 1

    since = _date.fromisoformat(args.since) if args.since else None
    until = _date.fromisoformat(args.until) if args.until else None
    days = None if (since or until) else args.days

    result = run_backtest(config, bars, days=days, since=since, until=until)
    _print_backtest(result, config)
    return 0


def _print_backtest(result, config: Config) -> None:
    """バックテストの結果を人が読める形で出す。"""
    if not result.days:
        print("\nその期間のデータがありませんでした。")
        return

    span = (f"{result.days[0]}" if len(result.days) == 1
            else f"{result.days[0]} 〜 {result.days[-1]}（{len(result.days)} 営業日）")
    print(f"\n=== {span} のペーパートレード結果 ===\n")

    for symbol, series in sorted(result.bars.items()):
        for bar in series:
            if bar.day in result.days:
                print(f"  {symbol} {bar.day}  寄り {bar.open:,.1f} / 高値 {bar.high:,.1f}"
                      f" / 安値 {bar.low:,.1f} / 引け {bar.close:,.1f}")

    print()
    if result.trades:
        print("  売買（買って売るまでを 1 件）")
        for t in result.trades:
            mark = "＋" if t.profit >= 0 else "−"
            why = "損切り" if "損切り" in t.exit_reason else "売りライン"
            print(f"    [{t.rule_id}] {t.symbol} {t.quantity}株  "
                  f"買 {t.entry_price:,.1f} → 売 {t.exit_price:,.1f}（{why}）  "
                  f"{mark}{abs(t.profit):,.0f} 円")
    else:
        print("  売買: なし")

    _print_untriggered(result, config)

    if result.open_positions:
        print("\n  持ち越し（買ったが売れずに終わった）")
        for t in result.open_positions:
            print(f"    [{t.rule_id}] {t.symbol} {t.quantity}株  買 {t.entry_price:,.1f}")

    blocked = [e for e in result.events if e["event"] == "BLOCKED"]
    if blocked:
        print("\n  安全弁で見送った発注")
        seen: set[str] = set()
        for event in blocked:
            line = f"    [{event['rule_id']}] {event['note']}"
            if line not in seen:
                print(line)
                seen.add(line)

    print(f"\n  発注 {result.orders} 件 / 約定 {result.fills} 件")
    print(f"  実現損益: {result.realized:+,.0f} 円（手数料・税金は含みません）")
    if result.open_positions:
        print("  ※ 持ち越しぶんは含みません")
    print("\n  ※ 日足の 4 点（寄り・高値・安値・引け）でなぞった概算です。")
    print("     高値と安値のどちらが先だったかは日足からは分からないため、")
    print("     往復の回数は実際とずれることがあります。")


def _print_untriggered(result, config: Config) -> None:
    """一度も買えなかったルールについて、値段がどれだけ足りなかったかを出す。

    「何も起きなかった」だけでは、ルールが悪いのか相場が動かなかったのかが
    分からない。買いラインと期間中の安値の差を出して判断できるようにする。
    """
    traded = {t.rule_id for t in result.trades} | {t.rule_id for t in result.open_positions}
    lines: list[str] = []
    for rule in config.rules:
        if rule.id in traded:
            continue
        series = [b for b in result.bars.get(rule.symbol, []) if b.day in result.days]
        if not series:
            lines.append(f"    [{rule.id}] {rule.symbol}: この期間のデータがありません")
            continue
        low = min(b.low for b in series)
        head = f"    [{rule.id}] {rule.symbol}  買いライン {rule.buy_at:,.0f} / 期間の安値 {low:,.0f}"
        if low <= rule.buy_at:
            # 値段は届いていたのに買えていない = 安全弁か時間帯で止まっている。
            lines.append(f"{head}  → 値段は届いていました（上の見送り理由を確認してください）")
            continue
        gap = low - rule.buy_at
        pct = gap / low * 100          # 「いまの水準からあと何 % 下げれば」を出す
        lines.append(f"{head}  → あと {gap:,.0f} 円（{pct:.1f}%）下げれば届きます")
    if lines:
        print("\n  買えなかったルール")
        print("\n".join(lines))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="kabu", description="楽天証券 RSS 自動売買ボット")
    parser.add_argument("-c", "--config", default="config.yaml", help="設定ファイル (既定: config.yaml)")
    parser.add_argument("-v", "--verbose", action="store_true", help="詳細ログ")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="売買ループを開始する")
    run.add_argument("--yes", action="store_true", help="実弾モードの確認プロンプトを省略する")
    run.set_defaults(func=cmd_run)

    play = sub.add_parser("play", help="ブラウザで動きが見えるシミュレーターを開く")
    play.add_argument("--port", type=int, default=8765, help="待ち受けポート (既定: 8765)")
    play.add_argument("--host", default="127.0.0.1",
                      help="待ち受けアドレス (既定: 127.0.0.1)。"
                           "スマホなど同じ Wi-Fi の別端末から見るには 0.0.0.0")
    play.add_argument("--lan", dest="host", action="store_const", const="0.0.0.0",
                      help="--host 0.0.0.0 の短縮。スマホから見たいときはこれ")
    play.add_argument("--market-hours", dest="anytime", action="store_false",
                      help="立会時間内でしか動かさない（既定はいつでも動く）")
    play.add_argument("--state", default="play_state.json", help="シミュレーション用の状態ファイル")
    play.add_argument("--journal", default="play_journal.csv", help="シミュレーション用の記録ファイル")
    play.add_argument(
        "--source", choices=("sim", "real", "delayed", "rss"), default="sim",
        help="株価の出どころ。sim=擬似（既定） / real=実際の日足を再生 / "
             "delayed=実際の現在値（20 分ほど遅延・口座不要） / "
             "rss=マーケットスピードII のリアルタイム",
    )
    play.add_argument("--provider", choices=("stooq", "yahoo"), default="stooq",
                      help="--source real のときの取得元 (既定: stooq)")
    play.add_argument("--speed", type=int, default=4,
                      help="--source real の再生速度。1 秒あたり何点進むか (既定: 4)")
    play.add_argument("--cache-dir", default=".kabu_cache",
                      help="取得した株価データの保存先")
    play.add_argument("--poll-live", type=float, default=30.0,
                      help="--source delayed で現在値を取り直す間隔（秒・最短 10）")
    play.set_defaults(func=cmd_play, anytime=True)

    back = sub.add_parser(
        "backtest", help="過去の実データで、このルールならどうなっていたかを見る"
    )
    back.add_argument("--days", type=int, default=1,
                      help="直近から何営業日ぶん回すか (既定: 1 = 直近の取引日だけ)")
    back.add_argument("--since", help="この日から回す (YYYY-MM-DD)")
    back.add_argument("--until", help="この日まで回す (YYYY-MM-DD)")
    back.add_argument("--provider", choices=("stooq", "yahoo"), default="stooq",
                      help="株価データの取得元 (既定: stooq)")
    back.add_argument("--cache-dir", default=".kabu_cache",
                      help="取得した株価データの保存先")
    back.set_defaults(func=cmd_backtest)

    sub.add_parser("status", help="現在の状態を表示する").set_defaults(func=cmd_status)
    sub.add_parser("doctor", help="接続と設定を診断する").set_defaults(func=cmd_doctor)
    sub.add_parser("init-sheet", help="RSS 用 Excel ブックの雛形を作る").set_defaults(func=cmd_init_sheet)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except KabuError as exc:
        print(f"エラー: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
