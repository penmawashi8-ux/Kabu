"""コマンドライン入口。

    kabu doctor      接続と設定の健康診断（発注はしない）
    kabu init-sheet  RSS 用の Excel ブックの雛形を作る
    kabu status      現在の状態を表示する
    kabu run         売買ループを開始する
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


def cmd_run(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    setup_logging(config.log_file, verbose=args.verbose, timezone=config.market.timezone)

    if config.is_live:
        print("=" * 68)
        print(" 実弾モードです。実際に楽天証券へ注文が送信されます。")
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

    symbols = sorted({r.symbol for r in config.rules})
    try:
        from .rss import ExcelBridge

        bridge = ExcelBridge(config)
        bridge.build_quote_sheet(symbols)
        quotes = bridge.read_quotes(symbols)
        print("Excel / RSS 接続  : OK")
        for symbol in symbols:
            price = quotes.get(symbol)
            print(f"  {symbol:>10} : {price if price is not None else '取得できず'}")
        orders = bridge.read_order_table()
        print(f"注文一覧シート    : {len(orders)} 行読めました")
    except KabuError as exc:
        print(f"Excel / RSS 接続  : NG — {exc}")
        return 1 if config.is_live else 0
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
    serve(config, host=args.host, port=args.port)
    return 0


def cmd_init_sheet(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    from .sheet_template import write_template

    path = write_template(config)
    print(f"雛形を作成しました: {path}")
    print("Excel で開き、ORDERS シートの A1 に RSS の注文一覧関数を入力してください。")
    print("（README の「注文一覧シートの用意」を参照）")
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
    play.add_argument("--host", default="127.0.0.1", help="待ち受けアドレス (既定: 127.0.0.1)")
    play.add_argument("--market-hours", dest="anytime", action="store_false",
                      help="立会時間内でしか動かさない（既定はいつでも動く）")
    play.add_argument("--state", default="play_state.json", help="シミュレーション用の状態ファイル")
    play.add_argument("--journal", default="play_journal.csv", help="シミュレーション用の記録ファイル")
    play.set_defaults(func=cmd_play, anytime=True)

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
