"""Bot Telegram : commandes + contrôle d'accès strict par identifiant.

Un utilisateur non autorisé est ignoré silencieusement (aucune réponse).
Le bot ne peut ni passer d'ordre ni modifier les limites de risque :
il consulte l'état, et met en pause / reprend les NOUVELLES transactions.
"""
from __future__ import annotations

import html
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import wraps

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

log = logging.getLogger(__name__)


@dataclass
class RobotState:
    paused: bool = False
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    strategy: str = "RSI14 + EMA20/50 + MACD + volume"
    last_error: str | None = None


def is_authorized(user_id: int | None, allowed: tuple[int, ...]) -> bool:
    return user_id is not None and user_id in allowed


def authorized(fn):
    @wraps(fn)
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        uid = update.effective_user.id if update.effective_user else None
        if not is_authorized(uid, ctx.bot_data["settings"].allowed_ids):
            log.warning("Accès Telegram refusé (id=%s)", uid)
            return
        return await fn(update, ctx)
    return wrapper


async def _reply(update: Update, text: str) -> None:
    await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML)


def _fmt(n: float, d: int = 2) -> str:
    return f"{n:,.{d}f}".replace(",", " ")


HELP = (
    "<b>Commandes</b>\n"
    "/status - état du robot\n/balance - solde\n/price - prix\n"
    "/signals - derniers signaux\n/trade - dernières transactions\n"
    "/profit - performances\n/pause - suspendre les nouveaux achats\n"
    "/resume - reprendre\n/settings - paramètres\n/help - cette aide"
)


@authorized
async def cmd_start(update, ctx):
    await _reply(update, "Robot de trading prêt.\n\n" + HELP)


@authorized
async def cmd_help(update, ctx):
    await _reply(update, HELP)


@authorized
async def cmd_status(update, ctx):
    s, st, db = ctx.bot_data["settings"], ctx.bot_data["state"], ctx.bot_data["db"]
    up = datetime.now(timezone.utc) - st.started_at
    etat = "EN PAUSE" if st.paused else "ACTIF"
    txt = (f"<b>État :</b> {etat}\n<b>Mode :</b> {s.mode}\n"
           f"<b>Stratégie :</b> {html.escape(st.strategy)}\n"
           f"<b>Positions ouvertes :</b> {len(db.get_open_positions())}/{s.max_open_positions}\n"
           f"<b>Actif depuis :</b> {str(up).split('.')[0]}")
    if st.last_error:
        txt += f"\n<b>Dernière erreur :</b> {html.escape(st.last_error)}"
    await _reply(update, txt)


@authorized
async def cmd_balance(update, ctx):
    s, db, ex = ctx.bot_data["settings"], ctx.bot_data["db"], ctx.bot_data["exchange"]
    if ex.can_trade:
        try:
            usdt = ex.fetch_free_balance("USDT")
        except Exception as e:
            await _reply(update, f"Solde indisponible : {html.escape(type(e).__name__)}")
            return
        await _reply(update, f"<b>USDT disponible ({s.mode}) :</b> {_fmt(usdt)}")
        return
    perf = db.performance()
    invested = sum(p["entry_price"] * p["quantity"] for p in db.get_open_positions())
    cash = s.starting_capital + perf["realized_pnl"] - invested
    await _reply(update, f"<b>Capital simulé (paper) :</b> {_fmt(cash)} USDT disponibles\n"
                         f"Investi : {_fmt(invested)} USDT")


@authorized
async def cmd_price(update, ctx):
    s, ex = ctx.bot_data["settings"], ctx.bot_data["exchange"]
    try:
        prices = ex.fetch_prices(list(s.pairs))
    except Exception as e:
        await _reply(update, f"Prix indisponibles : {html.escape(type(e).__name__)}")
        return
    lines = [f"{sym} : {_fmt(p, 4 if p < 10 else 2)}" for sym, p in prices.items()]
    await _reply(update, "<b>Prix</b>\n" + "\n".join(lines))


@authorized
async def cmd_signals(update, ctx):
    rows = ctx.bot_data["db"].get_signals(10)
    if not rows:
        await _reply(update, "Aucun signal enregistré pour l'instant.")
        return
    lines = [f"{r['created_at'][5:16]} {r['symbol']} {r['signal']}" for r in rows]
    await _reply(update, "<b>Derniers signaux</b>\n" + "\n".join(lines))


@authorized
async def cmd_trade(update, ctx):
    rows = ctx.bot_data["db"].get_trades(10)
    if not rows:
        await _reply(update, "Aucune transaction pour l'instant.")
        return
    lines = [f"{r['created_at'][5:16]} {r['side'].upper()} {r['symbol']} "
             f"{r['quantity']:g} @ {_fmt(r['price'], 4)}" for r in rows]
    await _reply(update, "<b>Dernières transactions</b>\n" + "\n".join(lines))


@authorized
async def cmd_profit(update, ctx):
    p = ctx.bot_data["db"].performance()
    await _reply(update,
        "<b>Performances (réalisées, frais inclus)</b>\n"
        f"Transactions clôturées : {p['closed_trades']}\n"
        f"Gain/perte net : {_fmt(p['realized_pnl'], 4)} USDT\n"
        f"Taux de réussite : {_fmt(p['win_rate_pct'], 1)} %\n"
        f"Frais totaux : {_fmt(p['total_fees'], 4)} USDT\n"
        "Gains non réalisés : disponibles avec le moteur de trading (étape 7).")


@authorized
async def cmd_pause(update, ctx):
    ctx.bot_data["state"].paused = True
    await _reply(update, "Nouvelles transactions suspendues. Les Stop Loss et Take Profit restent actifs.")


@authorized
async def cmd_resume(update, ctx):
    ctx.bot_data["state"].paused = False
    await _reply(update, "Nouvelles transactions reprises.")


@authorized
async def cmd_settings(update, ctx):
    s = ctx.bot_data["settings"]
    await _reply(update,
        "<b>Paramètres</b>\n"
        f"Mode : {s.mode}\nPaires : {', '.join(s.pairs)}\nTimeframe : {s.timeframe}\n"
        f"Risque/transaction : {s.risk_per_trade_pct} %\nStop Loss : {s.stop_loss_pct} %\n"
        f"Take Profit : {s.take_profit_pct} %\nPositions max : {s.max_open_positions}\n"
        f"Perte max/jour : {s.max_daily_loss_pct} %\nPause après vente : {s.cooldown_minutes} min\n"
        f"IA : {'activée' if s.ai_enabled else 'désactivée'}\n"
        f"Sync Supabase : {'oui' if s.supabase_sync else 'non'}")


HANDLERS = {
    "start": cmd_start, "help": cmd_help, "status": cmd_status, "balance": cmd_balance,
    "price": cmd_price, "signals": cmd_signals, "trade": cmd_trade, "profit": cmd_profit,
    "pause": cmd_pause, "resume": cmd_resume, "settings": cmd_settings,
}


def build_application(settings, db, exchange, state: RobotState) -> Application:
    app = Application.builder().token(settings.telegram_token).build()
    app.bot_data.update(settings=settings, db=db, exchange=exchange, state=state)
    for name, fn in HANDLERS.items():
        app.add_handler(CommandHandler(name, fn))
    return app


async def notify(app: Application, text: str) -> None:
    """Notification automatique vers tous les IDs autorisés (achat, vente, SL, TP, erreur)."""
    for uid in app.bot_data["settings"].allowed_ids:
        try:
            await app.bot.send_message(uid, text, parse_mode=ParseMode.HTML)
        except Exception as e:
            log.warning("Notification échouée (%s)", type(e).__name__)
