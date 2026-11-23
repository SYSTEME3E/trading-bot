"""Point d'entrée.

  python main.py --check   : vérifie la config, Binance et la base (sans Telegram)
  python main.py           : lance le bot Telegram et la boucle de trading
"""
from __future__ import annotations

import asyncio
import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

import config
from database.database import Database
from exchange.binance_client import BinanceClient
from telegram_bot.commands import RobotState, build_application, notify
from trading.risk_manager import RiskManager
from trading.trader import Trader


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
    state = RobotState()
    app = build_application(s, db, ex, state)
    trader = Trader(s, db, ex, RiskManager(s, db, ex), state)

    async def trading_job(context):
        try:
            msgs = await asyncio.to_thread(trader.tick)   # ccxt est synchrone : hors boucle asyncio
        except Exception as e:
            log.exception("Erreur dans le cycle de trading")
            state.last_error = type(e).__name__
            return
        for m in msgs:
            await notify(context.application, m)

    async def startup_job(context):
        await notify(context.application,
                     f"Robot démarré en mode <b>{s.mode}</b>. Capital alloué : "
                     f"{s.starting_capital:g} USDT. Cycle : {s.loop_interval} s.")

    app.job_queue.run_once(startup_job, 3)
    app.job_queue.run_repeating(trading_job, interval=s.loop_interval, first=5,
                                job_kwargs={"max_instances": 1, "coalesce": True})
    log.info("Bot Telegram démarré (mode=%s).", s.mode)
    app.run_polling()
    return 0


if __name__ == "__main__":
    sys.exit(main())
