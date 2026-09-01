"""コマンドライン入口。

    kabu doctor      接続と設定の健康診断（発注はしない）
    kabu init-sheet  RSS 用の Excel ブックの雛形を作る
    kabu status      現在の状態を表示する
    kabu run         売買ループを開始する
    kabu backtest    過去の実データでルールを検証する（発注しない）
    kabu optimize    過去データから良かった値幅設定を総当たりで探す
    kabu compare     売買手法そのもの（トレンド追随・逆張り等）を比べる
    kabu validate    その手法の優位が本物か、感度・対照実験・期間別で確かめる
    kabu portfolio   複数銘柄を同時に持つ場合（分散）を検証する
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
                years=getattr(args, "years", 10),
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
    trimmed: dict[str, list] = {}
    for symbol in sorted(bars):
        series = bars[symbol][-args.days:] if args.days else bars[symbol]
        if len(series) < 220:
            continue
        trimmed[symbol] = series
        per_symbol[symbol] = compare(series)

    def net(outcome) -> float:
        return outcome.net_return_pct(tax_pct=args.tax, slippage_pct=args.slippage)

    if not per_symbol:
        print("\n比べられる銘柄がありませんでした（各銘柄 220 営業日ぶん以上のデータが要ります）。")
        return 1

    any_symbol = next(iter(per_symbol))
    names = [o.name for o in per_symbol[any_symbol]]
    span = f"{len(per_symbol)} 銘柄"

    print(f"\n{'=' * 78}")
    print(f"■ 手法ごとの成績（{span}の平均・税金 {args.tax:g}% と滑り {args.slippage:g}%/回 を引いた後）")
    print(f"{'-' * 78}")
    print(_pad("手法", 40) + _rpad("平均損益", 9) + _rpad("中央値", 8)
          + _rpad("勝った銘柄", 11) + _rpad("平均下落", 9))

    hold_by_symbol = {sym: net(outs[0]) for sym, outs in per_symbol.items()}
    ranking: list[tuple[str, float, int]] = []
    for i, name in enumerate(names):
        returns = [net(outs[i]) for outs in per_symbol.values()]
        drawdowns = [outs[i].max_drawdown_pct for outs in per_symbol.values()]
        beat_hold = sum(
            1 for sym, outs in per_symbol.items()
            if net(outs[i]) > hold_by_symbol[sym]
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

    if args.periods > 1:
        from .research import split_periods

        print(f"\n{'=' * 78}")
        print(f"■ 期間を {args.periods} 等分したときの平均損益（相場つきに左右されていないか）")
        print(f"{'-' * 78}")
        chunks: list[list[list[float]]] = [[] for _ in range(args.periods)]
        for symbol, series in trimmed.items():
            for slot, part in enumerate(split_periods(series, args.periods)):
                if len(part) < 220 // args.periods:
                    continue
                chunks[slot].append([net(o) for o in compare(part)])
        header = "".join(_rpad(f"期間{i + 1}", 10) for i in range(args.periods))
        print(_pad("手法", 40) + header)
        for i, name in enumerate(names):
            cells = ""
            for slot in range(args.periods):
                values = [row[i] for row in chunks[slot]]
                cells += f"{sum(values) / len(values):>+9.1f}%" if values else _rpad("—", 10)
            print(_pad(name, 40) + cells)
        print("\n  どの期間でも持ちっぱなしを上回る手法だけが、相場つきに関係なく効いています。")
        print("  ※ 200 日線のように長い準備期間が要る手法は、区間が短いと売買が成立せず")
        print("     0.0% と出ます。強い/弱いではなく「測れていない」という意味です。")

    # 一番成績の良かった手法（持ちっぱなしを除く）で、銘柄ごとの上位を出す。
    best_name, _best_avg, _ = max(
        (r for r in ranking if r[0] != "持ちっぱなし"), key=lambda r: r[1]
    )
    best_index = names.index(best_name)
    rows = sorted(
        (
            (sym, net(outs[best_index]), hold_by_symbol[sym], outs[best_index].trades)
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
    print(f"  * 数字は税金 {args.tax:g}% と滑り {args.slippage:g}%/回 を引いた後です。")
    print("    税金は同じ期間の損益を通算した後の利益にだけかけています（特定口座に合わせています）。")
    print("    持ちっぱなしは最後に 1 回課税されるだけなので、売買が多い手法ほど不利になります。")
    print("  * 持ちっぱなしに勝てない手法は、手間とリスクを増やしているだけです。")
    print("  * 銘柄ごとの内訳は --detail、税引前で見たいときは --tax 0 --slippage 0 を付けてください。")
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    """ある手法の優位が本物か、3 つの角度から確かめる（発注しない）。

    1. パラメータをずらしても成立するか（たまたま噛み合っただけではないか）
    2. でたらめな売買（対照実験）に勝てるか
    3. どの期間でも成立するか（上げ相場だけで勝っていないか）
    """
    import statistics

    from .research import atr_band, atr_variants, buy_and_hold, random_trades, split_periods

    config = load_config(args.config)
    setup_logging(config.log_file, verbose=args.verbose, timezone=config.market.timezone)
    quiet_analysis(args.verbose)

    symbols = _resolve_symbols(config, args)
    bars = _fetch_with_fallback(symbols, args)
    if bars is None:
        return 1

    series_by_symbol = {
        sym: (data[-args.days:] if args.days else data)
        for sym, data in bars.items() if len(data) >= 260
    }
    if not series_by_symbol:
        print("\n検証できる銘柄がありませんでした（1 銘柄あたり 260 営業日ぶん以上が要ります）。")
        return 1

    def net(outcome) -> float:
        return outcome.net_return_pct(tax_pct=args.tax, slippage_pct=args.slippage)

    days = statistics.median(len(v) for v in series_by_symbol.values())
    print(f"\n{len(series_by_symbol)} 銘柄 × 約 {days:.0f} 営業日で検証します"
          f"（税金 {args.tax:g}% と滑り {args.slippage:g}%/回 を引いた後）")

    hold = {sym: net(buy_and_hold(v)) for sym, v in series_by_symbol.items()}
    hold_median = statistics.median(hold.values())

    # ---------------------------------------------------------- 1. 感度
    print(f"\n{'=' * 78}")
    print("■ 1. パラメータをずらしても成立するか")
    print(f"{'-' * 78}")
    print("  少し動かしただけで崩れるなら、その成績はたまたま噛み合っただけです。")
    print(f"  比較の基準 — 持ちっぱなしの中央値: {hold_median:+.1f}%\n")
    print(_pad("設定", 34) + _rpad("中央値", 9) + _rpad("持ちに勝った銘柄", 18))

    survived = 0
    variants = atr_variants()
    for name, params in variants:
        results = {sym: net(atr_band(v, **params)) for sym, v in series_by_symbol.items()}
        median = statistics.median(results.values())
        beat = sum(1 for sym, value in results.items() if value > hold[sym])
        if median > hold_median:
            survived += 1
        mark = "○" if median > hold_median else "×"
        print(_pad(f"{mark} {name}", 34) + f"{median:>+8.1f}%"
              + f"{beat:>10} / {len(results):<6}")
    print(f"\n  {len(variants)} 通り中 {survived} 通りで、持ちっぱなしの中央値を上回りました。")
    if survived >= len(variants) * 0.7:
        print("  → パラメータに依存していません。手法そのものの性質と考えてよさそうです。")
    elif survived <= len(variants) * 0.3:
        print("  → ほとんどの設定で負けています。最初の結果はたまたまです。")
    else:
        print("  → 設定によって結果が変わります。優位があるとしても弱いものです。")

    # ------------------------------------------------------- 2. 対照実験
    print(f"\n{'=' * 78}")
    print("■ 2. でたらめな売買（対照実験）に勝てるか")
    print(f"{'-' * 78}")
    print("  同じ回数・同じ保有日数でランダムに売買した場合と比べます。")
    print("  これに勝てないなら、勝因は判断ではなく「市場に居た時間」です。\n")

    real_values, random_values = [], []
    for sym, v in series_by_symbol.items():
        outcome = atr_band(v)
        real_values.append(net(outcome))
        holds = [
            (p.exit_day - p.entry_day).days
            for p in outcome.positions if p.exit_day and p.entry_day
        ]
        # 同じ条件に揃えるため、実際の売買回数と保有日数を対照実験に渡す。
        placebo = [
            net(random_trades(v, trades=max(1, outcome.trades),
                              hold_days=max(1, int(statistics.median(holds))) if holds else 10,
                              seed=seed))
            for seed in range(args.rounds)
        ]
        random_values.append(statistics.median(placebo))

    real_median = statistics.median(real_values)
    random_median = statistics.median(random_values)
    print(f"  ATR 連動バンド      : {real_median:+.1f}%")
    print(f"  ランダム売買（{args.rounds} 回の中央値）: {random_median:+.1f}%")
    print(f"  差                  : {real_median - random_median:+.1f}%")
    if real_median > random_median:
        print("  → でたらめより良い結果です。判断に意味がある可能性があります。")
    else:
        print("  → でたらめに勝てていません。この手法の売買判断には意味がありません。")

    # ------------------------------------------------------- 3. 期間別
    print(f"\n{'=' * 78}")
    print(f"■ 3. どの期間でも成立するか（{args.periods} 等分）")
    print(f"{'-' * 78}")
    print(_pad("期間", 12) + _rpad("ATR 連動", 12) + _rpad("持ちっぱなし", 16) + _rpad("差", 9))
    wins = 0
    for slot in range(args.periods):
        mine, theirs = [], []
        for v in series_by_symbol.values():
            parts = split_periods(v, args.periods)
            if slot >= len(parts) or len(parts[slot]) < 60:
                continue
            mine.append(net(atr_band(parts[slot])))
            theirs.append(net(buy_and_hold(parts[slot])))
        if not mine:
            continue
        a, b = statistics.median(mine), statistics.median(theirs)
        wins += 1 if a > b else 0
        print(_pad(f"期間 {slot + 1}", 12) + f"{a:>+11.1f}%{b:>+15.1f}%{a - b:>+8.1f}%")
    print(f"\n  {args.periods} 期間中 {wins} 期間で持ちっぱなしを上回りました。")

    print(f"\n{'=' * 78}")
    print("結論の出し方")
    print(f"{'-' * 78}")
    print("  3 つとも通ったときだけ、実装に進む価値があります。")
    print("  1 つでも落ちたら、その優位は偶然か、特定の相場つきに限った話です。")
    print("  なお、ここで通っても将来を保証するものではありません。")
    print("  過去に成立した性質が、これからも続くとは限らないためです。")
    return 0


def cmd_portfolio(args: argparse.Namespace) -> int:
    """複数銘柄を同時に持つ場合を検証する（発注しない）。

    これまでの検証は 1 銘柄ずつの話だった。ここでは分散の効果を見る。
    """
    from .portfolio import equal_weight_index, run_portfolio, single_stock_median

    config = load_config(args.config)
    setup_logging(config.log_file, verbose=args.verbose, timezone=config.market.timezone)
    quiet_analysis(args.verbose)

    symbols = _resolve_symbols(config, args)
    bars = _fetch_with_fallback(symbols, args)
    if bars is None:
        return 1
    if args.days:
        bars = {s: v[-args.days:] for s, v in bars.items()}
    bars = {s: v for s, v in bars.items() if len(v) > args.lookback + 40}
    if not bars:
        print("\n検証できる銘柄がありませんでした（履歴が足りません）。")
        return 1

    days = min(len(v) for v in bars.values())
    print(f"\n{len(bars)} 銘柄 × 約 {days} 営業日 / 資金 {args.capital:,.0f} 円"
          f"（税金 {args.tax:g}% と滑り {args.slippage:g}%/回 を引いた後）")

    plans = [
        ("equal", args.hold),
        ("momentum", args.hold),
        ("momentum", max(1, args.hold * 2)),
    ]
    results = [
        run_portfolio(
            bars, capital=args.capital, hold=hold, rebalance_days=args.rebalance,
            select=select, lookback=args.lookback,
            tax_pct=args.tax, slippage_pct=args.slippage,
        )
        for select, hold in plans
    ]

    print(f"\n{'=' * 78}")
    print("■ 組み方ごとの成績")
    print(f"{'-' * 78}")
    print(_pad("組み方", 40) + _rpad("損益", 9) + _rpad("最大下落", 10)
          + _rpad("平均銘柄数", 12) + _rpad("入替", 7))
    for result in results:
        if not result.equity:
            print(_pad(result.name, 40) + "  履歴が足りず評価できません")
            continue
        print(_pad(result.name, 40)
              + f"{result.return_pct:>+8.1f}%{result.max_drawdown_pct:>9.1f}%"
              + f"{result.average_names:>10.1f}{result.rebalances:>6}回")

    baseline = single_stock_median(bars, tax_pct=args.tax, slippage_pct=args.slippage)
    index_return, index_drawdown = equal_weight_index(bars, tax_pct=args.tax)
    print(f"\n{'-' * 78}")
    print("  比較の基準")
    print(f"    1 銘柄を持ちっぱなし（中央値）        : {baseline:+.1f}%")
    print(f"    全 {len(bars)} 銘柄を等ウェイトで持ちっぱなし : "
          f"{index_return:+.1f}%   最大下落 {index_drawdown:.1f}%")
    print("    ※ 後者が「分散だけ」を効かせた基準です。単元株の制約は入れていないので、")
    print("       実際にはこの通りには組めません（指数に投資する代わりの目安）。")

    best = max((r for r in results if r.equity), key=lambda r: r.return_pct, default=None)
    if best:
        print(f"\n  一番良かった組み方: {best.name}（{best.return_pct:+.1f}%）")
        if best.return_pct <= index_return:
            print("  → 全銘柄等ウェイトに勝てていません。**選び方や入れ替えに意味はなく、")
            print("     効いていたのは分散だけ**、という結果です。")
        elif best.return_pct <= baseline:
            print("  → 1 銘柄持ちっぱなしの中央値にも勝てていません。")
        else:
            print("  → 両方の基準を上回りました。ただし 1 期間の結果にすぎません。")

    if any(r.skipped_capital for r in results):
        print(f"\n  ※ 資金 {args.capital:,.0f} 円では買えなかった銘柄があります。")
        print("     単元株（100 株）単位でしか買えないためです。分散したいなら")
        print("     資金を増やすか、単元未満株の扱いがある証券会社の仕組みを検討してください。")

    print(f"\n{'=' * 78}")
    print("読み方")
    print(f"{'-' * 78}")
    print("  * 分散の効果は「損益」より「最大下落」に出ます。損益が同じで下落が")
    print("    小さいなら、それは改善です（同じ利益をより小さいブレで得ている）。")
    print("  * 入れ替えのたびに利益が確定して課税されます。回数が多い組み方ほど不利です。")
    print("  * 判断は当日の終値まで、約定は翌日の寄り付き。未来を覗いていません。")
    print("  * 単元株の制約を入れてあります。資金が小さいほど銘柄数は増やせません。")
    print("  * 過去に良かった組み方が、これから良いとは限りません。")
    return 0


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
    back.add_argument("--years", type=int, default=10,
                      help="何年ぶんのデータを取るか (既定: 10)")
    back.add_argument("--cache-dir", default=".kabu_cache",
                      help="取得した株価データの保存先")
    back.set_defaults(func=cmd_backtest)

    opt = sub.add_parser(
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
    opt.add_argument("--years", type=int, default=10,
                      help="何年ぶんのデータを取るか (既定: 10)")
    opt.add_argument("--cache-dir", default=".kabu_cache", help="取得したデータの保存先")
    opt.set_defaults(func=cmd_optimize)

    cmp_ = sub.add_parser("compare", help="売買手法そのものを並べて比べる")
    _add_symbol_options(cmp_)
    cmp_.add_argument("--days", type=int, default=0,
                      help="直近何営業日ぶんで比べるか (既定: 0 = 取得できた全期間)")
    cmp_.add_argument("--top", type=int, default=15, help="銘柄別に並べる件数 (既定: 15)")
    cmp_.add_argument("--detail", action="store_true", help="銘柄ごとの内訳も全部出す")
    cmp_.add_argument("--tax", type=float, default=20.315,
                      help="利益にかかる税率%% (既定: 20.315。0 にすると税引前)")
    cmp_.add_argument("--slippage", type=float, default=0.05,
                      help="1 回の売買で滑る%% (既定: 0.05。買いと売りで 2 回ぶん引く)")
    cmp_.add_argument("--periods", type=int, default=3,
                      help="期間を何等分して安定性を見るか (既定: 3。1 で無効)")
    cmp_.add_argument("--provider", choices=("stooq", "yahoo"), default="yahoo",
                      help="株価データの取得元 (既定: yahoo)")
    cmp_.add_argument("--years", type=int, default=10,
                      help="何年ぶんのデータを取るか (既定: 10)")
    cmp_.add_argument("--cache-dir", default=".kabu_cache", help="取得したデータの保存先")
    cmp_.set_defaults(func=cmd_compare)

    val = sub.add_parser("validate", help="手法の優位が本物か、感度・対照実験・期間別で確かめる")
    _add_symbol_options(val)
    val.add_argument("--days", type=int, default=0, help="直近何営業日ぶんで見るか (既定: 全期間)")
    val.add_argument("--periods", type=int, default=4, help="期間を何等分するか (既定: 4)")
    val.add_argument("--rounds", type=int, default=5, help="対照実験を何回まわすか (既定: 5)")
    val.add_argument("--tax", type=float, default=20.315, help="利益にかかる税率%% (既定: 20.315)")
    val.add_argument("--slippage", type=float, default=0.05, help="1 回の売買で滑る%% (既定: 0.05)")
    val.add_argument("--years", type=int, default=10, help="何年ぶんのデータを取るか (既定: 10)")
    val.add_argument("--provider", choices=("stooq", "yahoo"), default="yahoo",
                     help="株価データの取得元 (既定: yahoo)")
    val.add_argument("--cache-dir", default=".kabu_cache", help="取得したデータの保存先")
    val.set_defaults(func=cmd_validate)

    pf = sub.add_parser("portfolio", help="複数銘柄を同時に持つ場合を検証する")
    _add_symbol_options(pf)
    pf.add_argument("--capital", type=float, default=1_000_000, help="資金 (既定: 100 万円)")
    pf.add_argument("--hold", type=int, default=5, help="同時に持つ銘柄数 (既定: 5)")
    pf.add_argument("--rebalance", type=int, default=20,
                    help="何営業日ごとに入れ替えるか (既定: 20)")
    pf.add_argument("--lookback", type=int, default=120,
                    help="モメンタムを測る期間 (既定: 120 営業日)")
    pf.add_argument("--days", type=int, default=0, help="直近何営業日ぶんで見るか (既定: 全期間)")
    pf.add_argument("--tax", type=float, default=20.315, help="利益にかかる税率%% (既定: 20.315)")
    pf.add_argument("--slippage", type=float, default=0.05, help="1 回の売買で滑る%% (既定: 0.05)")
    pf.add_argument("--years", type=int, default=10, help="何年ぶんのデータを取るか (既定: 10)")
    pf.add_argument("--provider", choices=("stooq", "yahoo"), default="yahoo",
                    help="株価データの取得元 (既定: yahoo)")
    pf.add_argument("--cache-dir", default=".kabu_cache", help="取得したデータの保存先")
    pf.set_defaults(func=cmd_portfolio)

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
