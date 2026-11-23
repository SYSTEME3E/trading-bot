"""Configuration centralisée. Lit .env, valide, et expose un objet immuable.

Aucune clé n'est écrite dans le code. Le mode 'live' est refusé sauf
confirmation explicite (LIVE_CONFIRMATION=JE_COMPRENDS_LES_RISQUES).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

LIVE_PHRASE = "JE_COMPRENDS_LES_RISQUES"
VALID_MODES = {"paper", "testnet", "live"}


class ConfigError(Exception):
    pass


def _f(name: str, default: str) -> float:
    try:
        return float(os.getenv(name, default))
    except ValueError as e:
        raise ConfigError(f"{name} doit être un nombre") from e


def _i(name: str, default: str) -> int:
    try:
        return int(os.getenv(name, default))
    except ValueError as e:
        raise ConfigError(f"{name} doit être un entier") from e


def _list(name: str, default: str = "") -> list[str]:
    return [x.strip() for x in os.getenv(name, default).split(",") if x.strip()]


@dataclass(frozen=True)
class Settings:
    mode: str
    api_key: str = field(repr=False)
    api_secret: str = field(repr=False)
    telegram_token: str = field(repr=False)
    allowed_ids: tuple[int, ...]
    pairs: tuple[str, ...]
    timeframe: str
    loop_interval: int
    starting_capital: float
    risk_per_trade_pct: float
    stop_loss_pct: float
    take_profit_pct: float
    max_open_positions: int
    max_daily_loss_pct: float
    cooldown_minutes: int
    fee_rate_pct: float
    ai_enabled: bool
    ai_provider: str
    ai_api_key: str = field(repr=False)
    db_path: str
    log_file: str
    supabase_sync: bool = False
    supabase_url: str = ""
    supabase_key: str = field(default="", repr=False)


def load_settings() -> Settings:
    mode = os.getenv("TRADING_MODE", "paper").lower()
    if mode not in VALID_MODES:
        raise ConfigError(f"TRADING_MODE invalide : {mode}")
    if mode == "live" and os.getenv("LIVE_CONFIRMATION") != LIVE_PHRASE:
        raise ConfigError("Mode live refusé : confirmation explicite absente.")

    try:
        ids = tuple(int(x) for x in _list("TELEGRAM_ALLOWED_IDS"))
    except ValueError as e:
        raise ConfigError("TELEGRAM_ALLOWED_IDS doit contenir des entiers") from e

    s = Settings(
        mode=mode,
        api_key=os.getenv("BINANCE_API_KEY", ""),
        api_secret=os.getenv("BINANCE_API_SECRET", ""),
        telegram_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
        allowed_ids=ids,
        pairs=tuple(_list("PAIRS", "BTC/USDT,ETH/USDT,BNB/USDT,SOL/USDT,XRP/USDT")),
        timeframe=os.getenv("TIMEFRAME", "15m"),
        loop_interval=_i("LOOP_INTERVAL_SECONDS", "60"),
        starting_capital=_f("STARTING_CAPITAL_USDT", "80"),
        risk_per_trade_pct=_f("RISK_PER_TRADE_PCT", "1.0"),
        stop_loss_pct=_f("STOP_LOSS_PCT", "2.0"),
        take_profit_pct=_f("TAKE_PROFIT_PCT", "4.0"),
        max_open_positions=_i("MAX_OPEN_POSITIONS", "2"),
        max_daily_loss_pct=_f("MAX_DAILY_LOSS_PCT", "5.0"),
        cooldown_minutes=_i("COOLDOWN_MINUTES", "60"),
        fee_rate_pct=_f("FEE_RATE_PCT", "0.1"),
        ai_enabled=os.getenv("AI_ENABLED", "false").lower() == "true",
        ai_provider=os.getenv("AI_PROVIDER", ""),
        ai_api_key=os.getenv("AI_API_KEY", ""),
        db_path=os.getenv("DB_PATH", "data/robot.db"),
        log_file=os.getenv("LOG_FILE", "logs/trading.log"),
        supabase_sync=os.getenv("SUPABASE_SYNC", "false").lower() == "true",
        supabase_url=os.getenv("SUPABASE_URL", ""),
        supabase_key=os.getenv("SUPABASE_SERVICE_KEY", ""),
    )
    _validate(s)
    return s


def _validate(s: Settings) -> None:
    if s.stop_loss_pct <= 0:
        raise ConfigError("STOP_LOSS_PCT doit être > 0 (Stop Loss obligatoire)")
    if s.take_profit_pct <= s.stop_loss_pct:
        raise ConfigError("TAKE_PROFIT_PCT doit être > STOP_LOSS_PCT")
    if not 0 < s.risk_per_trade_pct <= 5:
        raise ConfigError("RISK_PER_TRADE_PCT doit être entre 0 et 5")
    if s.max_open_positions < 1:
        raise ConfigError("MAX_OPEN_POSITIONS doit être >= 1")
    if not s.pairs:
        raise ConfigError("PAIRS ne peut pas être vide")
    if s.supabase_sync and not (s.supabase_url and s.supabase_key):
        raise ConfigError("SUPABASE_URL et SUPABASE_SERVICE_KEY requis si SUPABASE_SYNC=true")
    if s.mode in {"testnet", "live"} and not (s.api_key and s.api_secret):
        raise ConfigError("Clés Binance requises pour testnet/live")
