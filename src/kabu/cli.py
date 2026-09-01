"""コマンドライン入口。

    kabu doctor      接続と設定の健康診断（発注はしない）
    kabu init-sheet  RSS 用の Excel ブックの雛形を作る
    kabu status      現在の状態を表示する
    kabu run         売買ループを開始する
    kabu backtest    過去の実データでルールを検証する（発注しない）
    kabu optimize    過去データから良かった値幅設定を総当たりで探す
    kabu compare     売買手法そのもの（トレンド追随・逆張り等）を比べる
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
from .logging_setup import quiet_analysis, setup_logging
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


def _rotating_rules(config: Config) -> list[str]:
    """往復売買・損切りをするルールの id を返す。

    NISA と相性が悪いのは「繰り返し買い直すこと」と「損切りすること」であって、
    NISA 口座そのものではない。判定基準をこの 2 つに絞る:

        max_cycles > 1   何度も往復する → 買うたびに非課税枠を食う
        stop_loss あり   損切りする     → NISA では損益通算・繰越控除ができない

    どちらも無いルール（買って持ち続けるだけ）は、むしろ NISA の本来の使い道。
    無効化されたルールは実際には発注しないので数えない。
    """
    return [
        rule.id
        for rule in config.rules
        if rule.enabled and (rule.max_cycles > 1 or rule.stop_loss is not None)
    ]


def _nisa_warning(config: Config) -> str:
    """NISA 枠で回転売買をしようとしていたら警告する。

    買って持ち続けるだけの設定（config.core.yaml 系）では黙る。そこで警告を出すと
    「NISA では何もするな」という誤ったメッセージになり、本当に危ない往復売買の
    ときの警告まで読み飛ばされるようになる。
    """
    account = str(config.rss.order_defaults.get("account_type", ""))
    if "NISA" not in account.upper() and "ニーサ" not in account:
        return ""

    rotating = _rotating_rules(config)
    if not rotating:
        return ""

    return (
        f" 口座区分が「{account}」のまま、往復売買・損切りをするルールが有効になっています:\n"
        f"   {', '.join(rotating)}\n"
        "   NISA 枠でこれをやると次の不利があります:\n"
        "   * 売買を繰り返すと年間の投資枠をすぐ使い切ります（売っても枠が戻るのは翌年）\n"
        "   * 損益通算・繰越控除ができないため、損切りしても税金面の救済がありません\n"
        "   往復させる枠は特定口座を勧めます。NISA で回すなら、買って持ち続ける\n"
        "   ルール（max_cycles: 1・stop_loss なし）だけにしてください。"
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


def _resolve_symbols(config: Config, args: argparse.Namespace) -> list[str]:
    """調べる銘柄を決める。--symbols-file > --symbols > --universe > config.yaml。"""
    from .universe import from_file, symbols as universe_symbols

    if getattr(args, "symbols_file", None):
        return from_file(args.symbols_file)
    if getattr(args, "symbols", None):
        return [s.strip() for s in args.symbols.split(",") if s.strip()]
    if getattr(args, "universe", None):
        return universe_symbols(args.universe)
    return sorted({r.symbol for r in config.rules})


def _add_symbol_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--symbols", help="対象銘柄をカンマ区切りで")
    parser.add_argument("--symbols-file", help="銘柄を書いたファイル（1 行 1 銘柄）")
    parser.add_argument("--universe", choices=("major", "large"),
                        help="内蔵の銘柄リスト。major=主要 16 銘柄 / large=主要 116 銘柄")


def _fetch_with_fallback(symbols: list[str], args: argparse.Namespace):
    """実データを取ってくる。片方の取得元が制限をかけていたらもう片方を試す。"""
    from .marketdata import MarketDataError, fetch_many

    order = [args.provider] + [p for p in ("yahoo", "stooq") if p != args.provider]
    many = len(symbols) > 8

    def progress(done: int, total: int, symbol: str) -> None:
        # 100 銘柄を取ると数分かかる。止まって見えないように出す。
        print(f"\r  {done}/{total} {symbol:<12}", end="", flush=True)

    for i, provider in enumerate(order):
        listed = f"{len(symbols)} 銘柄" if many else ", ".join(symbols)
        print(f"実データを取得しています（{provider}）… {listed}")
        try:
            bars = fetch_many(
                symbols, provider=provider, cache_dir=args.cache_dir,
                # まとめて取るときは間を置く（取得元に制限をかけられないように）。
                pause=0.2 if many else 0.0,
                on_progress=progress if many else None,
            )
            if many:
                print(f"\r  {len(bars)}/{len(symbols)} 銘柄ぶん取得しました" + " " * 20)
            return bars
        except MarketDataError as exc:
            print(f"\n{provider} からは取得できませんでした:\n  {exc}\n")
            if i + 1 < len(order):
                print(f"{order[i + 1]} で取り直します。\n")
    print("どの取得元からも実データを取得できませんでした。"
          "ネットワーク（社内プロキシなど）を確認してください。")
    return None


def cmd_backtest(args: argparse.Namespace) -> int:
    """過去の実データで「このルールならどうなっていたか」を確かめる（発注しない）。"""
    from datetime import date as _date

    from .backtest import run_backtest

    config = load_config(args.config)
    setup_logging(config.log_file, verbose=args.verbose, timezone=config.market.timezone)
    quiet_analysis(args.verbose)   # 1 周ごとのログで結果が流れないようにする

    symbols = sorted({r.symbol for r in config.rules})
    bars = _fetch_with_fallback(symbols, args)
    if bars is None:
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


def cmd_optimize(args: argparse.Namespace) -> int:
    """値幅の候補を総当たりし、探索期間で選んで検証期間で確かめる（発注しない）。"""
    from .optimize import default_grid, optimize_symbol

    config = load_config(args.config)
    setup_logging(config.log_file, verbose=args.verbose, timezone=config.market.timezone)
    quiet_analysis(args.verbose)   # 候補ごとに何千周も回すのでログは画面に出さない

    symbols = _resolve_symbols(config, args)
    bars = _fetch_with_fallback(symbols, args)
    if bars is None:
        return 1

    grid = default_grid()
    print(f"\n値幅の候補 {len(grid)} 通りを、銘柄ごとに総当たりします…")

    reports = []
    for symbol in sorted(bars):
        print(f"  {symbol} を計算中…", flush=True)
        reports.append(optimize_symbol(
            config, symbol, bars[symbol],
            holdout=args.holdout, quantity=args.quantity, grid=grid, top=args.top,
            segment=args.segment,
        ))
    _print_optimize(reports, holdout=args.holdout, segment=args.segment)
    return 0


def _print_optimize(reports, *, holdout: int, segment: int) -> None:
    for report in reports:
        print(f"\n{'=' * 78}")
        if report.skipped:
            print(f"■ {report.symbol} — 対象外: {report.skipped}")
            continue

        capital = report.reference * report.quantity
        print(f"■ {report.symbol}   基準価格 {report.reference:,.0f} 円 × "
              f"{report.quantity} 株 = {capital:,.0f} 円")
        print(f"  1 日の値幅（中央値）{report.daily_range_pct:.1f}% より広い "
              f"{report.tested} 通りを、{segment} 営業日ずつの区間で総当たり")
        print(f"{'-' * 78}")
        for i, (train, hold) in enumerate(zip(report.train, report.holdout), start=1):
            c = train.candidate
            print(f"\n  {i}. {c.label()}")
            print(f"     1 往復で狙う値幅 {c.gain_pct:+.1f}% / 損切りまで {c.loss_pct:+.1f}%"
                  f"  → ±ゼロに必要な勝率 {c.breakeven_win_rate:.0f}%")
            print(f"     探索期間  {train.realized:+,.0f}円  "
                  f"勝ち区間 {train.steady_pct:.0f}%  売買 {train.trades}回")
            note = ""
            if hold.trades == 0:
                note = "  ← 1 度も売買が成立していません"
            elif hold.trades < 3:
                note = "  ← 売買が少なすぎて判断材料になりません"
            print(f"     検証期間  {hold.realized:+,.0f}円  "
                  f"勝ち区間 {hold.steady_pct:.0f}%  売買 {hold.trades}回{note}")

        if report.train and report.train[0].realized <= 0:
            print("\n  ※ 探索期間では、どの設定でも利益が出ていません。"
                  "後知恵で選んでも勝てない相場だった、ということです。")
            print("     この銘柄をこのやり方で売買するのは見送るのが妥当です。")

        best = report.holdout[0] if report.holdout else None
        if best:
            print(f"\n  検証期間（1 位の設定）: {best.return_pct:+.1f}%"
                  f"（{best.realized:+,.0f} 円 / 投下 {capital:,.0f} 円）"
                  f"  最悪の区間 {best.worst_segment:+,.0f} 円")
        print(f"  同じ期間を持ちっぱなし  : {report.hold_return_pct:+.1f}%")

    print(f"\n{'=' * 78}")
    print("読み方")
    print(f"{'-' * 78}")
    print(f"  * 期間は {segment} 営業日ずつに区切り、区間ごとに直前の終値を基準に線を引き直しています。")
    print("    このボットの線は固定の値段なので、株価が離れれば置き去りになります。")
    print("    長い期間を一続きで測ると、値幅の良し悪しではなく株価のドリフトを測ることになります。")
    print("  * 「勝ち区間」= 利益で終わった区間の割合。**これが「安定して」の中身**です。")
    print("    合計だけでは、1 区間の大当たりで持ち上がっていても見分けられません。")
    print(f"  * 検証期間 = 直近 {holdout} 営業日。候補の選定には一切使っていません。")
    print("    実運用で期待できる姿に近いのはこちらです。探索期間の数字は後知恵です。")
    print("  * 持ちっぱなしに負けているなら、売買を繰り返す意味はありません。")
    print("  * 手数料・税金・滑りは計算に含まれていません。楽天証券の「ゼロコース」を")
    print("    選んでいれば国内株の売買手数料は 0 円ですが、利益には約 20.315% の税金が")
    print("    かかります（特定口座）。損切りの成行注文で滑るぶんも実質的なコストです。")
    print("  * 日足の 4 点でなぞった概算です。往復の回数は実際とずれます。")
    print("  * 過去にうまくいった設定が、これから儲かる保証はありません。")


def _shown_width(text: str) -> int:
    """画面に出したときの桁数（全角は 2 桁）。"""
    import unicodedata

    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)


def _pad(text: str, width: int) -> str:
    """表示幅で左寄せ。

    Python の f"{s:<30}" は文字数で数えるため、日本語混じりの表は桁がずれる。
    """
    return text + " " * max(0, width - _shown_width(text))


def _rpad(text: str, width: int) -> str:
    """表示幅で右寄せ（見出しを数字の桁に合わせる）。"""
    return " " * max(0, width - _shown_width(text)) + text


def cmd_compare(args: argparse.Namespace) -> int:
    """売買手法そのものを並べて比べる（値幅の調整ではなく、やり方の比較）。"""
    import statistics

    from .research import compare
    from .universe import name_of

    config = load_config(args.config)
    setup_logging(config.log_file, verbose=args.verbose, timezone=config.market.timezone)
    quiet_analysis(args.verbose)

    symbols = _resolve_symbols(config, args)
    bars = _fetch_with_fallback(symbols, args)
    if bars is None:
        return 1

    # 銘柄ごとの結果を集める（画面には出さず、まず全体を集計する）。
    per_symbol: dict[str, list] = {}
    for symbol in sorted(bars):
        series = bars[symbol][-args.days:] if args.days else bars[symbol]
        if len(series) < 220:
            continue
        per_symbol[symbol] = compare(series)

    if not per_symbol:
        print("\n比べられる銘柄がありませんでした（各銘柄 220 営業日ぶん以上のデータが要ります）。")
        return 1

    any_symbol = next(iter(per_symbol))
    names = [o.name for o in per_symbol[any_symbol]]
    span = f"{len(per_symbol)} 銘柄"

    print(f"\n{'=' * 78}")
    print(f"■ 手法ごとの成績（{span}の平均）")
    print(f"{'-' * 78}")
    print(_pad("手法", 40) + _rpad("平均損益", 9) + _rpad("中央値", 8)
          + _rpad("勝った銘柄", 11) + _rpad("平均下落", 9))

    hold_by_symbol = {sym: outs[0].return_pct for sym, outs in per_symbol.items()}
    ranking: list[tuple[str, float, int]] = []
    for i, name in enumerate(names):
        returns = [outs[i].return_pct for outs in per_symbol.values()]
        drawdowns = [outs[i].max_drawdown_pct for outs in per_symbol.values()]
        beat_hold = sum(
            1 for sym, outs in per_symbol.items()
            if outs[i].return_pct > hold_by_symbol[sym]
        )
        average = sum(returns) / len(returns)
        ranking.append((name, average, beat_hold))
        won = len([r for r in returns if r > 0])
        print(_pad(name, 40)
              + f"{average:>+8.1f}%{statistics.median(returns):>+7.1f}%"
              + f"{won:>6}/{len(returns):<4}"
              + f"{sum(drawdowns) / len(drawdowns):>8.1f}%")

    print(f"\n  「勝った銘柄」= 損益がプラスだった銘柄数 / 全体")
    print(f"{'-' * 78}")
    print("  持ちっぱなしに勝てた銘柄数")
    for name, _average, beat_hold in ranking:
        if name == "持ちっぱなし":
            continue
        print(f"    {_pad(name, 44)}{beat_hold:>3} / {len(per_symbol)} 銘柄")

    # 一番成績の良かった手法（持ちっぱなしを除く）で、銘柄ごとの上位を出す。
    best_name, _best_avg, _ = max(
        (r for r in ranking if r[0] != "持ちっぱなし"), key=lambda r: r[1]
    )
    best_index = names.index(best_name)
    rows = sorted(
        (
            (sym, outs[best_index].return_pct, hold_by_symbol[sym], outs[best_index].trades)
            for sym, outs in per_symbol.items()
        ),
        key=lambda row: row[1] - row[2], reverse=True,
    )
    print(f"\n{'=' * 78}")
    print(f"■ {best_name} が持ちっぱなしに勝った銘柄（上位 {min(args.top, len(rows))} 件）")
    print(f"{'-' * 78}")
    print(_pad("銘柄", 30) + _rpad("この手法", 10) + _rpad("持ちっぱなし", 14) + _rpad("差", 9))
    for symbol, mine, hold, _trades in rows[:args.top]:
        label = f"{symbol} {name_of(symbol)}".strip()
        print(_pad(label, 30) + f"{mine:>+9.1f}%{hold:>+13.1f}%{mine - hold:>+8.1f}%")

    if args.detail:
        for symbol, outcomes in per_symbol.items():
            print(f"\n{'=' * 78}")
            print(f"■ {symbol} {name_of(symbol)}")
            print(f"{'-' * 78}")
            print(_pad("手法", 40) + _rpad("損益", 7) + _rpad("売買", 7)
                  + _rpad("勝率", 7) + _rpad("最大下落", 9) + _rpad("保有", 6))
            for outcome in outcomes:
                print(_pad(outcome.name, 40)
                      + f"{outcome.return_pct:>+6.1f}%{outcome.trades:>5}回"
                      + f"{outcome.win_rate:>6.0f}%{outcome.max_drawdown_pct:>8.1f}%"
                      + f"{outcome.exposure_pct:>5.0f}%")

    print(f"\n{'=' * 78}")
    print("読み方")
    print(f"{'-' * 78}")
    print("  * 判断は当日の終値まで、約定は翌日の寄り付き。未来を覗いていません。")
    print("  * 各手法のパラメータは教科書的な既定値で固定しています。この期間に")
    print("    合わせて調整はしていません（調整すると必ず良く見えてしまうため）。")
    print("  * 「平均下落」= 評価額の山からの最大下落率の平均。どれだけ含み損に")
    print("    耐える必要があったか。損益が同じなら、小さいほうが良い手法です。")
    print("  * 税金（利益の約 20.315%）と滑りは含んでいません。売買が多い手法ほど不利です。")
    print("  * 持ちっぱなしに勝てない手法は、手間とリスクを増やしているだけです。")
    print("  * 銘柄ごとの内訳は --detail を付けると出ます。")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="kabu", description="楽天証券 RSS 自動売買ボット")
    parser.add_argument("-c", "--config", default="config.yaml", help="設定ファイル (既定: config.yaml)")
    parser.add_argument("-v", "--verbose", action="store_true", help="詳細ログ")

    # -c と -v はサブコマンドの後ろに書いても効くようにする。
    #   kabu -c config.core.yaml play    ← 前
    #   kabu play -c config.core.yaml    ← 後ろ（こちらのほうが自然に打ってしまう）
    # default を SUPPRESS にしているのが肝で、こうしないと「サブコマンド側の
    # 既定値」が「前に書いて実際に指定された値」を上書きしてしまう。
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-c", "--config", default=argparse.SUPPRESS,
                        help="設定ファイル (既定: config.yaml)")
    common.add_argument("-v", "--verbose", action="store_true", default=argparse.SUPPRESS,
                        help="詳細ログ")

    sub = parser.add_subparsers(dest="command", required=True)

    def add(name: str, **kwargs) -> argparse.ArgumentParser:
        return sub.add_parser(name, parents=[common], **kwargs)

    run = add("run", help="売買ループを開始する")
    run.add_argument("--yes", action="store_true", help="実弾モードの確認プロンプトを省略する")
    run.set_defaults(func=cmd_run)

    play = add("play", help="ブラウザで動きが見えるシミュレーターを開く")
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

    back = add(
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

    opt = add(
        "optimize", help="過去データから、どの値幅設定が良かったかを総当たりで探す"
    )
    _add_symbol_options(opt)
    opt.add_argument("--holdout", type=int, default=60,
                     help="検証用に取り分ける直近の営業日数 (既定: 60)")
    opt.add_argument("--quantity", type=int, default=100, help="1 回の株数 (既定: 100)")
    opt.add_argument("--top", type=int, default=3, help="銘柄ごとに見る上位件数 (既定: 3)")
    opt.add_argument("--segment", type=int, default=20,
                     help="何営業日ごとに線を引き直すか (既定: 20)")
    opt.add_argument("--provider", choices=("stooq", "yahoo"), default="yahoo",
                     help="株価データの取得元 (既定: yahoo)")
    opt.add_argument("--cache-dir", default=".kabu_cache", help="取得したデータの保存先")
    opt.set_defaults(func=cmd_optimize)

    cmp_ = add("compare", help="売買手法そのものを並べて比べる")
    _add_symbol_options(cmp_)
    cmp_.add_argument("--days", type=int, default=0,
                      help="直近何営業日ぶんで比べるか (既定: 0 = 取得できた全期間)")
    cmp_.add_argument("--top", type=int, default=15, help="銘柄別に並べる件数 (既定: 15)")
    cmp_.add_argument("--detail", action="store_true", help="銘柄ごとの内訳も全部出す")
    cmp_.add_argument("--provider", choices=("stooq", "yahoo"), default="yahoo",
                      help="株価データの取得元 (既定: yahoo)")
    cmp_.add_argument("--cache-dir", default=".kabu_cache", help="取得したデータの保存先")
    cmp_.set_defaults(func=cmd_compare)

    add("status", help="現在の状態を表示する").set_defaults(func=cmd_status)
    add("doctor", help="接続と設定を診断する").set_defaults(func=cmd_doctor)
    add("init-sheet", help="RSS 用 Excel ブックの雛形を作る").set_defaults(func=cmd_init_sheet)
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
