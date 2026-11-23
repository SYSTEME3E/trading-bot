"""Taille de position, SL/TP, limites, anti-doublon, coupe-circuit. Autorité finale sur tout ordre.

Aucun ordre n'est passé ici : le gestionnaire de risque répond seulement
« oui, voici la quantité et les niveaux » ou « non, voici pourquoi ».
Le trader ne peut acheter que si `check_buy` renvoie `allowed=True`.

Règles appliquées à chaque achat :
- coupe-circuit : perte réalisée du jour >= MAX_DAILY_LOSS_PCT du capital de départ ;
- nombre maximum de positions ouvertes ;
- une seule position par paire (anti-doublon) ;
- pause après la clôture d'une paire (COOLDOWN_MINUTES) ;
- taille = risque par trade / distance du Stop Loss, plafonnée à capital/max positions
  et au cash disponible (frais compris) ;
- quantité arrondie à la baisse selon la précision de Binance, minimums respectés.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str
    quantity: float = 0.0
    notional: float = 0.0
    stop_loss: float = 0.0
    take_profit: float = 0.0


def _refuse(reason: str) -> Decision:
    return Decision(False, reason)


class RiskManager:
    def __init__(self, settings, db, exchange):
        self.s = settings
        self.db = db
        self.ex = exchange

    # ---------- utilitaires ----------
    def open_positions(self) -> list[dict]:
        """Positions ouvertes du mode courant (on ne mélange pas paper et réel)."""
        return [p for p in self.db.get_open_positions() if p["mode"] == self.s.mode]

    def levels(self, entry_price: float) -> tuple[float, float]:
        sl = entry_price * (1 - self.s.stop_loss_pct / 100)
        tp = entry_price * (1 + self.s.take_profit_pct / 100)
        return sl, tp

    def daily_loss_hit(self, now: datetime | None = None) -> bool:
        now = now or datetime.now(timezone.utc)
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        pnl = self.db.realized_pnl_since(start.isoformat(timespec="seconds"))
        limit = self.s.starting_capital * self.s.max_daily_loss_pct / 100
        return pnl <= -limit

    # ---------- sortie ----------
    def exit_reason(self, position: dict, price: float, signal_action: str | None = None):
        """Le Stop Loss passe avant tout, puis le Take Profit, puis le signal de vente."""
        if price <= position["stop_loss"]:
            return "stop_loss"
        if price >= position["take_profit"]:
            return "take_profit"
        if signal_action == "SELL":
            return "signal_sell"
        return None

    # ---------- entrée ----------
    def check_buy(self, symbol: str, price: float, cash: float, equity: float,
                  now: datetime | None = None) -> Decision:
        now = now or datetime.now(timezone.utc)
        s = self.s
        if price <= 0 or equity <= 0:
            return _refuse("Prix ou capital invalide")
        if self.daily_loss_hit(now):
            return _refuse(f"Coupe-circuit : perte du jour >= {s.max_daily_loss_pct} %")

        open_pos = self.open_positions()
        if len(open_pos) >= s.max_open_positions:
            return _refuse(f"Positions maximum atteintes ({len(open_pos)}/{s.max_open_positions})")
        if any(p["symbol"] == symbol for p in open_pos):
            return _refuse(f"Position déjà ouverte sur {symbol}")

        last = self.db.last_closed_at(symbol)
        if last:
            elapsed = (now - datetime.fromisoformat(last)).total_seconds() / 60
            if elapsed < s.cooldown_minutes:
                return _refuse(f"Pause après vente : encore {s.cooldown_minutes - elapsed:.0f} min")

        # Taille : on risque `risk_per_trade_pct` du capital jusqu'au Stop Loss.
        fee = s.fee_rate_pct / 100
        risk_amount = equity * s.risk_per_trade_pct / 100
        notional = min(risk_amount / (s.stop_loss_pct / 100),
                       equity / s.max_open_positions,
                       cash / (1 + fee))
        if notional <= 0:
            return _refuse("Cash disponible insuffisant")

        try:
            qty = self.ex.amount_to_precision(symbol, notional / price)
            limits = self.ex.market_limits(symbol)
        except Exception as e:  # marché inconnu, précision impossible...
            log.warning("Précision/minimums indisponibles pour %s : %s", symbol, type(e).__name__)
            return _refuse("Minimums de l'exchange indisponibles")

        cost = qty * price
        if qty <= 0 or qty < limits["min_amount"]:
            return _refuse("Quantité sous le minimum de l'exchange")
        if cost < limits["min_cost"]:
            return _refuse(f"Ordre trop petit ({cost:.2f} < {limits['min_cost']:.2f} USDT minimum)")
        if cost * (1 + fee) > cash:
            return _refuse("Cash disponible insuffisant (frais compris)")

        sl, tp = self.levels(price)
        return Decision(True, "OK", quantity=qty, notional=cost, stop_loss=sl, take_profit=tp)
