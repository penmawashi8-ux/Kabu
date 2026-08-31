"""config.yaml の読み込みと検証。

設定ミスは実弾を撃つ前に必ず落とす。読み込み時点で厳しめに検証する。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import time
from pathlib import Path
from typing import Any, Sequence

import yaml

from .errors import ConfigError
from .models import OrderType

_ALLOWED_MODES = ("paper", "live")


def _require(d: dict[str, Any], key: str, where: str) -> Any:
    if key not in d:
        raise ConfigError(f"{where}: 必須項目 '{key}' がありません")
    return d[key]


def _as_time(value: Any, where: str) -> time:
    if isinstance(value, time):
        return value
    if not isinstance(value, str):
        raise ConfigError(f"{where}: 時刻は 'HH:MM' 形式で指定してください (got {value!r})")
    try:
        hh, mm = value.split(":")
        return time(int(hh), int(mm))
    except (ValueError, TypeError) as exc:
        raise ConfigError(f"{where}: 時刻 '{value}' を解釈できません") from exc


def _as_positive(value: Any, where: str, *, integer: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{where}: 数値を指定してください (got {value!r})")
    if integer and int(value) != value:
        raise ConfigError(f"{where}: 整数を指定してください (got {value!r})")
    if value <= 0:
        raise ConfigError(f"{where}: 正の数を指定してください (got {value!r})")
    return value


@dataclass(frozen=True)
class Rule:
    """1 銘柄ぶんの売買ルール。

    買い: 現在値 <= buy_at
    売り: 現在値 >= sell_at、または stop_loss を下回ったら損切り
    """

    id: str
    symbol: str
    quantity: int
    buy_at: float
    sell_at: float
    stop_loss: float | None = None
    order_type: OrderType = OrderType.LIMIT
    max_cycles: int = 1
    enabled: bool = True

    @staticmethod
    def from_dict(raw: dict[str, Any], index: int) -> "Rule":
        where = f"rules[{index}]"
        if not isinstance(raw, dict):
            raise ConfigError(f"{where}: マッピングで指定してください")

        rule_id = str(_require(raw, "id", where))
        symbol = str(_require(raw, "symbol", where))
        quantity = int(_as_positive(_require(raw, "quantity", where), f"{where}.quantity", integer=True))
        buy_at = float(_as_positive(_require(raw, "buy_at", where), f"{where}.buy_at"))
        sell_at = float(_as_positive(_require(raw, "sell_at", where), f"{where}.sell_at"))

        stop_loss_raw = raw.get("stop_loss")
        stop_loss = None
        if stop_loss_raw is not None:
            stop_loss = float(_as_positive(stop_loss_raw, f"{where}.stop_loss"))

        try:
            order_type = OrderType(str(raw.get("order_type", "limit")))
        except ValueError as exc:
            raise ConfigError(f"{where}.order_type: 'limit' か 'market' を指定してください") from exc

        max_cycles = int(_as_positive(raw.get("max_cycles", 1), f"{where}.max_cycles", integer=True))

        if sell_at <= buy_at:
            raise ConfigError(
                f"{where}: sell_at ({sell_at}) は buy_at ({buy_at}) より大きくしてください。"
                "そうでないと買った瞬間に売る無限ループになります"
            )
        if stop_loss is not None and stop_loss >= buy_at:
            raise ConfigError(
                f"{where}: stop_loss ({stop_loss}) は buy_at ({buy_at}) より小さくしてください。"
                "そうでないと約定直後に損切りが発動します"
            )

        return Rule(
            id=rule_id,
            symbol=symbol,
            quantity=quantity,
            buy_at=buy_at,
            sell_at=sell_at,
            stop_loss=stop_loss,
            order_type=order_type,
            max_cycles=max_cycles,
            enabled=bool(raw.get("enabled", True)),
        )


@dataclass(frozen=True)
class RiskConfig:
    max_orders_per_day: int = 20
    max_notional_per_order: float = 300_000
    max_notional_per_day: float = 1_000_000
    min_seconds_between_orders: float = 10
    max_price_deviation_pct: float = 10.0
    quote_max_age_sec: float = 60
    pending_timeout_sec: float = 600
    kill_switch_file: str = "STOP"

    @staticmethod
    def from_dict(raw: dict[str, Any]) -> "RiskConfig":
        defaults = RiskConfig()
        return RiskConfig(
            max_orders_per_day=int(
                _as_positive(raw.get("max_orders_per_day", defaults.max_orders_per_day),
                             "risk.max_orders_per_day", integer=True)
            ),
            max_notional_per_order=float(
                _as_positive(raw.get("max_notional_per_order", defaults.max_notional_per_order),
                             "risk.max_notional_per_order")
            ),
            max_notional_per_day=float(
                _as_positive(raw.get("max_notional_per_day", defaults.max_notional_per_day),
                             "risk.max_notional_per_day")
            ),
            min_seconds_between_orders=float(
                raw.get("min_seconds_between_orders", defaults.min_seconds_between_orders)
            ),
            max_price_deviation_pct=float(
                _as_positive(raw.get("max_price_deviation_pct", defaults.max_price_deviation_pct),
                             "risk.max_price_deviation_pct")
            ),
            quote_max_age_sec=float(
                _as_positive(raw.get("quote_max_age_sec", defaults.quote_max_age_sec), "risk.quote_max_age_sec")
            ),
            pending_timeout_sec=float(
                _as_positive(raw.get("pending_timeout_sec", defaults.pending_timeout_sec), "risk.pending_timeout_sec")
            ),
            kill_switch_file=str(raw.get("kill_switch_file", defaults.kill_switch_file)),
        )


@dataclass(frozen=True)
class MarketConfig:
    timezone: str = "Asia/Tokyo"
    # 東証は 2024/11/05 から後場が 15:30 まで。
    sessions: tuple[tuple[time, time], ...] = ((time(9, 0), time(11, 30)), (time(12, 30), time(15, 30)))
    trade_cutoff: time = time(15, 20)
    close_positions_at: time | None = None
    holidays: tuple[str, ...] = ()

    @staticmethod
    def from_dict(raw: dict[str, Any]) -> "MarketConfig":
        defaults = MarketConfig()
        sessions: tuple[tuple[time, time], ...] = defaults.sessions
        if "sessions" in raw:
            parsed: list[tuple[time, time]] = []
            for i, pair in enumerate(raw["sessions"]):
                if not isinstance(pair, (list, tuple)) or len(pair) != 2:
                    raise ConfigError(f"market.sessions[{i}]: ['HH:MM', 'HH:MM'] の形で指定してください")
                start = _as_time(pair[0], f"market.sessions[{i}][0]")
                end = _as_time(pair[1], f"market.sessions[{i}][1]")
                if end <= start:
                    raise ConfigError(f"market.sessions[{i}]: 終了時刻は開始時刻より後にしてください")
                parsed.append((start, end))
            if not parsed:
                raise ConfigError("market.sessions: 最低 1 つは指定してください")
            sessions = tuple(parsed)

        close_at = raw.get("close_positions_at")
        return MarketConfig(
            timezone=str(raw.get("timezone", defaults.timezone)),
            sessions=sessions,
            trade_cutoff=_as_time(raw.get("trade_cutoff", defaults.trade_cutoff), "market.trade_cutoff"),
            close_positions_at=_as_time(close_at, "market.close_positions_at") if close_at else None,
            holidays=tuple(str(d) for d in raw.get("holidays", ())),
        )


@dataclass(frozen=True)
class ExcelConfig:
    workbook: str = "quotes.xlsx"
    quote_sheet: str = "QUOTES"
    order_sheet: str = "ORDERS"
    visible: bool = True

    @staticmethod
    def from_dict(raw: dict[str, Any]) -> "ExcelConfig":
        defaults = ExcelConfig()
        return ExcelConfig(
            workbook=str(raw.get("workbook", defaults.workbook)),
            quote_sheet=str(raw.get("quote_sheet", defaults.quote_sheet)),
            order_sheet=str(raw.get("order_sheet", defaults.order_sheet)),
            visible=bool(raw.get("visible", defaults.visible)),
        )


@dataclass(frozen=True)
class RssConfig:
    """RSS 側の関数名・引数並びは公式マニュアルに合わせて YAML で差し替えられるようにしてある。

    バージョン差でシグネチャが変わっても、コードではなく設定を直せば追従できる。
    """

    quote_function: str = "RssMarket"
    price_field: str = "現在値"
    time_field: str = "現在値時刻"
    order_function: str = "RssOrderMulti"
    cancel_function: str = "RssOrderCancel"
    order_arg_order: tuple[str, ...] = (
        "order_kind", "symbol", "market", "side", "account_type",
        "quantity", "order_type", "price", "expiry", "password",
    )
    order_defaults: dict[str, Any] = field(default_factory=dict)
    order_columns: dict[str, str] = field(default_factory=dict)
    password_env: str = "KABU_TRADE_PASSWORD"

    @staticmethod
    def from_dict(raw: dict[str, Any]) -> "RssConfig":
        defaults = RssConfig()
        order_raw = raw.get("order", {}) or {}
        arg_order = order_raw.get("arg_order")
        return RssConfig(
            quote_function=str(raw.get("quote_function", defaults.quote_function)),
            price_field=str(raw.get("price_field", defaults.price_field)),
            time_field=str(raw.get("time_field", defaults.time_field)),
            order_function=str(order_raw.get("function", defaults.order_function)),
            cancel_function=str(order_raw.get("cancel_function", defaults.cancel_function)),
            order_arg_order=tuple(arg_order) if arg_order else defaults.order_arg_order,
            order_defaults=dict(order_raw.get("defaults", {}) or {}),
            order_columns=dict(raw.get("order_columns", {}) or {}),
            password_env=str(raw.get("password_env", defaults.password_env)),
        )


@dataclass(frozen=True)
class Config:
    mode: str
    poll_interval_sec: float
    excel: ExcelConfig
    rss: RssConfig
    risk: RiskConfig
    market: MarketConfig
    rules: tuple[Rule, ...]
    state_file: str = "state.json"
    journal_file: str = "journal.csv"
    log_file: str = "logs/kabu.log"

    @property
    def is_live(self) -> bool:
        return self.mode == "live"

    @staticmethod
    def from_dict(raw: dict[str, Any]) -> "Config":
        if not isinstance(raw, dict):
            raise ConfigError("設定ファイルのトップレベルはマッピングにしてください")

        mode = str(raw.get("mode", "paper"))
        if mode not in _ALLOWED_MODES:
            raise ConfigError(f"mode: {_ALLOWED_MODES} のいずれかを指定してください (got {mode!r})")

        poll = float(_as_positive(raw.get("poll_interval_sec", 3), "poll_interval_sec"))
        if poll < 1:
            raise ConfigError("poll_interval_sec: 1 秒未満はサーバ負荷になるので許可していません")

        rules_raw = raw.get("rules") or []
        if not rules_raw:
            raise ConfigError("rules: 最低 1 つルールを定義してください")
        rules = tuple(Rule.from_dict(r, i) for i, r in enumerate(rules_raw))

        seen: set[str] = set()
        for rule in rules:
            if rule.id in seen:
                raise ConfigError(f"rules: id '{rule.id}' が重複しています")
            seen.add(rule.id)

        return Config(
            mode=mode,
            poll_interval_sec=poll,
            excel=ExcelConfig.from_dict(raw.get("excel", {}) or {}),
            rss=RssConfig.from_dict(raw.get("rss", {}) or {}),
            risk=RiskConfig.from_dict(raw.get("risk", {}) or {}),
            market=MarketConfig.from_dict(raw.get("market", {}) or {}),
            rules=rules,
            state_file=str(raw.get("state_file", "state.json")),
            journal_file=str(raw.get("journal_file", "journal.csv")),
            log_file=str(raw.get("log_file", "logs/kabu.log")),
        )


def load_config(path: str | Path) -> Config:
    p = Path(path)
    if not p.exists():
        raise ConfigError(f"設定ファイルが見つかりません: {p}")
    with p.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    return Config.from_dict(raw or {})


def _tidy_number(value: float) -> float | int:
    """2800.0 のような割り切れる値は 2800 と書く（人が書いた見た目に近づける）。"""
    return int(value) if float(value).is_integer() else value


def _rule_to_mapping(rule: Rule) -> dict[str, Any]:
    """Rule を config.yaml の 1 ブロックと同じ見た目の dict に戻す。

    既定値と同じ項目は書かない。人が `config.example.yaml` を見よう見まねで
    書いたときと同じくらい素直な見た目にするため。
    """
    m: dict[str, Any] = {
        "id": rule.id,
        "symbol": rule.symbol,
        "quantity": rule.quantity,
        "buy_at": _tidy_number(rule.buy_at),
        "sell_at": _tidy_number(rule.sell_at),
    }
    if rule.stop_loss is not None:
        m["stop_loss"] = _tidy_number(rule.stop_loss)
    if rule.max_cycles != 1:
        m["max_cycles"] = rule.max_cycles
    if rule.order_type != OrderType.LIMIT:
        m["order_type"] = rule.order_type.value
    if not rule.enabled:
        m["enabled"] = rule.enabled
    return m


def save_rules(path: str | Path, rules: Sequence[Rule]) -> None:
    """画面（`kabu play`）で確定したルールを、config.yaml の `rules:` にだけ書き戻す。

    `rules:` 以外のキー・コメント・並び順は一切変えない
    （ruamel.yaml のラウンドトリップ読み書きでそれを保証している）。
    """
    from ruamel.yaml import YAML  # 依存を使う場所を絞るため遅延 import

    p = Path(path)
    if not p.exists():
        raise ConfigError(
            f"{p} が見つかりません。先に `copy config.example.yaml config.yaml` を実行してください"
        )

    rt = YAML()
    rt.preserve_quotes = True
    rt.width = 4096  # 長い行を勝手に折り返させない
    # config.example.yaml と同じ見た目（`  - id: ...` の 2 マス下げ）にする。
    rt.indent(mapping=2, sequence=4, offset=2)

    with p.open("r", encoding="utf-8") as f:
        data = rt.load(f)
    if data is None:
        raise ConfigError(f"{p}: 中身が空です")
    if not isinstance(data, dict):
        raise ConfigError(f"{p}: トップレベルはマッピングにしてください")

    data["rules"] = [_rule_to_mapping(r) for r in rules]

    with p.open("w", encoding="utf-8") as f:
        rt.dump(data, f)
