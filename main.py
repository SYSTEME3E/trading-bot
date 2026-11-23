"""Point d'entrée.

  python main.py --check   : vérifie la config, Binance et la base (sans Telegram)
  python main.py           : lance le bot Telegram (boucle de trading : étapes 5-7)
"""
from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

import config
from database.database import Database
from exchange.binance_client import BinanceClient
from telegram_bot.commands import RobotState, build_application


class RedactFilter(logging.Filter):
    """Masque les secrets (clés API, token Telegram) dans tous les logs."""

    def __init__(self, secrets):
        super().__init__()
        self.secrets = [s for s in secrets if s]

    def filter(self, record):
        msg = record.getMessage()
        for s in self.secrets:
            msg = msg.replace(s, "***")
        record.msg, record.args = msg, ()
        return True


def setup_logging(s: config.Settings) -> None:
    Path(s.log_file).parent.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s : %(message)s")
    handlers = [RotatingFileHandler(s.log_file, maxBytes=2_000_000, backupCount=3),
                logging.StreamHandler()]
    redact = RedactFilter([s.api_key, s.api_secret, s.telegram_token,
                           s.supabase_key, s.ai_api_key])
    for h in handlers:
        h.setFormatter(fmt)
        h.addFilter(redact)
        logging.getLogger().addHandler(h)
    logging.getLogger().setLevel(logging.INFO)
    # httpx affiche les URL Telegram, qui contiennent le token : on le réduit au silence.
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def main() -> int:
    s = config.load_settings()
    setup_logging(s)
    log = logging.getLogger("main")
    db = Database(s.db_path, sync_enabled=s.supabase_sync)
    ex = BinanceClient(s.mode, s.api_key, s.api_secret)

    if "--check" in sys.argv:
        print(f"Mode : {s.mode}")
        print(f"Binance (heure serveur ms) : {ex.check_connection()}")
        print(f"Prix : {ex.fetch_prices(list(s.pairs))}")
        if ex.can_trade:
            print(f"Solde USDT libre : {ex.fetch_free_balance('USDT')}")
        print(f"Base SQLite OK : {s.db_path}")
        return 0

    if not s.telegram_token or not s.allowed_ids:
        log.error("TELEGRAM_BOT_TOKEN et TELEGRAM_ALLOWED_IDS sont requis.")
        return 1
    app = build_application(s, db, ex, RobotState())
    log.info("Bot Telegram démarré (mode=%s).", s.mode)
    app.run_polling()
    return 0


if __name__ == "__main__":
    sys.exit(main())
