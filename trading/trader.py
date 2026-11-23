"""Exécution des ordres (paper/testnet/live), suivi des positions, passage obligatoire par risk_manager.

Un cycle (`tick`) :
  1. reprend les ordres dont le résultat était inconnu (erreur réseau) ;
  2. surveille les positions ouvertes : Stop Loss, Take Profit à chaque cycle ;
  3. sur chaque NOUVELLE bougie clôturée : calcule le signal, l'enregistre, puis
     vend (signal SELL) ou achète (signal BUY, si le gestionnaire de risque accepte).

Règles de sécurité :
- aucun achat sans `RiskManager.check_buy` ;
- un ordre n'est jamais renvoyé automatiquement : après une erreur on interroge
  Binance avec l'identifiant client avant toute autre action ;
- si l'état d'un ordre reste inconnu, les nouveaux achats sont suspendus ;
- on n'achète jamais sur une bougie ancienne (redémarrage) ;
- la quantité enregistrée est la quantité réellement détenue (frais en actif déduits).

Limite connue : le Stop Loss est surveillé par le robot (toutes les LOOP_INTERVAL_SECONDS).
Il ne protège pas si le robot est arrêté. Un ordre stop chez Binance viendra plus tard.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from exchange.binance_client import ExchangeError
from strategy.signals import evaluate

log = logging.getLogger(__name__)

REASONS = {"stop_loss": "Stop Loss", "take_profit": "Take Profit",
           "signal_sell": "Signal de vente"}
ALERT_COOLDOWN_S = 1800


@dataclass
class Fill:
    price: float
    quantity: float   # quantité nette réellement détenue / vendue
    fee: float        # en USDT
    order_id: str


def tf_seconds(tf: str) -> int:
    units = {"m": 60, "h": 3600, "d": 86400}
    return int(tf[:-1]) * units[tf[-1]]


class Trader:
    def __init__(self, settings, db, exchange, risk, state, now_fn=None):
        self.s, self.db, self.ex, self.risk, self.state = settings, db, exchange, risk, state
        self._now = now_fn or (lambda: datetime.now(timezone.utc))
        self._last_candle: dict[str, object] = {}
        self._pending: dict[str, dict] = {}     # ordres au résultat inconnu
        self._alerts: dict[str, datetime] = {}
        self._breaker_day = None

    # ---------- capital ----------
    def cash_and_equity(self) -> tuple[float, float]:
        """Capital alloué = capital de départ + gains réalisés (jamais plus que ce budget,
        même si le compte Binance contient davantage)."""
        positions = self.risk.open_positions()
        invested = sum(p["entry_price"] * p["quantity"] + p["entry_fee"] for p in positions)
        budget = self.s.starting_capital + self.db.performance()["realized_pnl"]
        cash = budget - invested
        if self.ex.can_trade:
            cash = min(cash, self.ex.fetch_free_balance("USDT"))
        return max(cash, 0.0), budget

    # ---------- alertes ----------
    def _alert(self, key: str, text: str, msgs: list[str]) -> None:
        now = self._now()
        last = self._alerts.get(key)
        if last is None or (now - last).total_seconds() >= ALERT_COOLDOWN_S:
            self._alerts[key] = now
            msgs.append(text)

    # ---------- cycle principal ----------
    def tick(self) -> list[str]:
        """Un cycle complet. Retourne les messages à envoyer par Telegram."""
        msgs: list[str] = []
        self._resolve_pending(msgs)
        try:
            prices = self.ex.fetch_prices(list(self.s.pairs))
        except ExchangeError as e:
            self.state.last_error = str(e)
            self._alert("prices", f"<b>Erreur</b> : prix indisponibles ({e})", msgs)
            return msgs
        self.state.last_error = None

        for pos in self.risk.open_positions():
            price = prices.get(pos["symbol"])
            if price and not self._blocked(pos["symbol"]):
                reason = self.risk.exit_reason(pos, price)
                if reason:
                    self._close(pos, price, reason, msgs)

        for symbol in self.s.pairs:
            self._evaluate_symbol(symbol, prices.get(symbol), msgs)
        return msgs

    def _blocked(self, symbol: str) -> bool:
        return any(p["symbol"] == symbol for p in self._pending.values())

    def _evaluate_symbol(self, symbol, price, msgs) -> None:
        if not price:
            return
        try:
            df = self.ex.fetch_ohlcv(symbol, self.s.timeframe)
        except ExchangeError as e:
            log.warning("Bougies indisponibles pour %s : %s", symbol, e)
            return
        if len(df) < 2:
            return
        candle_ts = df.index[-2]                       # dernière bougie CLÔTURÉE
        if self._last_candle.get(symbol) == candle_ts:
            return                                      # déjà traitée
        self._last_candle[symbol] = candle_ts

        sig = evaluate(symbol, df)
        self.db.add_signal(symbol, sig.action, sig.score,
                           {"trend": sig.trend, "reasons": sig.reasons, **sig.snapshot})

        closed_end = candle_ts.to_pydatetime() + timedelta(seconds=tf_seconds(self.s.timeframe))
        age = (self._now() - closed_end).total_seconds()
        if age > max(2 * self.s.loop_interval, 120):
            log.info("Bougie trop ancienne pour %s (%.0f s) : aucune décision", symbol, age)
            return
        if self._blocked(symbol):
            return

        pos = next((p for p in self.risk.open_positions() if p["symbol"] == symbol), None)
        if pos and sig.action == "SELL":
            reason = self.risk.exit_reason(pos, price, "SELL")
            self._close(pos, price, reason or "signal_sell", msgs)
        elif not pos and sig.action == "BUY":
            self._try_buy(symbol, price, msgs)

    # ---------- achat ----------
    def _try_buy(self, symbol, price, msgs) -> None:
        if self.state.paused:
            return
        if self._pending:
            return                                      # état d'un ordre inconnu : on attend
        if self.risk.daily_loss_hit(self._now()):
            day = self._now().date()
            if self._breaker_day != day:
                self._breaker_day = day
                msgs.append(f"<b>Coupe-circuit</b> : perte du jour >= {self.s.max_daily_loss_pct} %. "
                            "Plus aucun achat jusqu'à demain (UTC).")
            return
        try:
            cash, equity = self.cash_and_equity()
        except ExchangeError as e:
            self._alert("balance", f"<b>Erreur</b> : solde indisponible ({e})", msgs)
            return
        decision = self.risk.check_buy(symbol, price, cash, equity, self._now())
        if not decision.allowed:
            log.info("Achat refusé %s : %s", symbol, decision.reason)
            return

        if self.s.mode == "paper":
            fill = self._paper_fill(price, decision.quantity)
        else:
            cid = self._new_cid()
            order = self._send(symbol, "buy", decision.quantity, cid, None, msgs)
            if order is None:
                return
            fill = self._fill_from_order(order, symbol, price, "buy", cid)
        self._record_open(symbol, fill, msgs)

    def _record_open(self, symbol, fill: Fill, msgs) -> None:
        sl, tp = self.risk.levels(fill.price)
        pid = self.db.open_position(symbol, self.s.mode, fill.price, fill.quantity,
                                    sl, tp, fill.fee)
        self.db.add_trade(symbol, "buy", fill.price, fill.quantity, fill.fee, self.s.mode,
                          position_id=pid, order_id=fill.order_id)
        msgs.append(f"<b>ACHAT</b> {symbol} [{self.s.mode}]\n"
                    f"Quantité : {fill.quantity:g} @ {fill.price:,.4f}\n"
                    f"Stop Loss : {sl:,.4f} | Take Profit : {tp:,.4f}".replace(",", " "))

    # ---------- vente ----------
    def _close(self, pos, price, reason, msgs) -> None:
        symbol = pos["symbol"]
        if self.s.mode == "paper":
            fill = self._paper_fill(price, pos["quantity"])
        else:
            cid = self._new_cid()
            order = self._send(symbol, "sell", pos["quantity"], cid, pos["id"], msgs, reason)
            if order is None:
                self._alert(f"sell-{pos['id']}",
                            f"<b>Vente non exécutée</b> {symbol} ({REASONS.get(reason, reason)}). "
                            "Nouvelle tentative au prochain cycle.", msgs)
                return
            fill = self._fill_from_order(order, symbol, price, "sell", cid)
        self._record_close(pos, fill, reason, msgs)

    def _record_close(self, pos, fill: Fill, reason, msgs) -> None:
        pnl = self.db.close_position(pos["id"], fill.price, fill.fee, reason)
        self.db.add_trade(pos["symbol"], "sell", fill.price, fill.quantity, fill.fee,
                          self.s.mode, position_id=pos["id"], order_id=fill.order_id)
        msgs.append(f"<b>VENTE ({REASONS.get(reason, reason)})</b> {pos['symbol']} [{self.s.mode}]\n"
                    f"Sortie : {fill.price:,.4f}\n"
                    f"Gain/perte net : {pnl:+.4f} USDT".replace(",", " "))

    # ---------- exécution ----------
    def _paper_fill(self, price, quantity) -> Fill:
        fee = price * quantity * self.s.fee_rate_pct / 100
        return Fill(price, quantity, fee, f"paper-{uuid.uuid4().hex[:16]}")

    @staticmethod
    def _new_cid() -> str:
        return f"rb{uuid.uuid4().hex[:24]}"             # 26 caractères (limite Binance : 36)

    def _send(self, symbol, side, quantity, cid, position_id, msgs, reason=None):
        """Envoie un ordre. Retourne l'ordre exécuté, ou None s'il n'est PAS passé.
        Si on ne peut pas savoir, l'ordre reste en attente et les achats sont suspendus."""
        try:
            return self.ex.create_market_order(symbol, side, quantity, cid)
        except ExchangeError as e:
            log.warning("Ordre %s %s en erreur (%s) : vérification chez Binance", side, symbol, e)
        try:
            order = self.ex.find_order(symbol, cid)
        except ExchangeError:
            self._pending[cid] = {"symbol": symbol, "side": side, "pid": position_id,
                                  "reason": reason}
            self.state.paused = True
            msgs.append(f"<b>ALERTE</b> : résultat inconnu pour l'ordre {side} {symbol}. "
                        "Achats suspendus ; vérification automatique au prochain cycle. "
                        "Contrôlez aussi votre compte Binance.")
            return None
        if order and (order.get("filled") or 0) > 0:
            return order
        return None

    def _resolve_pending(self, msgs) -> None:
        for cid, p in list(self._pending.items()):
            try:
                order = self.ex.find_order(p["symbol"], cid)
            except ExchangeError:
                self._alert(f"pending-{cid}", f"<b>ALERTE</b> : ordre {p['side']} {p['symbol']} "
                            "toujours d'état inconnu. Vérifiez Binance.", msgs)
                continue
            del self._pending[cid]
            if order and (order.get("filled") or 0) > 0:
                fill = self._fill_from_order(order, p["symbol"], 0.0, p["side"], cid)
                if p["side"] == "buy":
                    self._record_open(p["symbol"], fill, msgs)
                else:
                    pos = next((x for x in self.risk.open_positions() if x["id"] == p["pid"]), None)
                    if pos:
                        self._record_close(pos, fill, p.get("reason") or "signal_sell", msgs)
                msgs.append("Ordre retrouvé chez Binance et enregistré. "
                            "Achats toujours en pause : /resume pour reprendre.")
            else:
                msgs.append(f"Ordre {p['side']} {p['symbol']} introuvable chez Binance : "
                            "considéré comme non passé. /resume pour reprendre.")

    def _fill_from_order(self, order, symbol, fallback_price, side, cid) -> Fill:
        base, quote = symbol.split("/")
        filled = float(order.get("filled") or order.get("amount") or 0.0)
        price = order.get("average") or order.get("price")
        if not price and order.get("cost") and filled:
            price = float(order["cost"]) / filled
        price = float(price or fallback_price)
        notional = price * filled

        fees = order.get("fees") or ([order["fee"]] if order.get("fee") else [])
        fee_quote, fee_base, unknown = 0.0, 0.0, not fees
        for f in fees:
            cost, cur = f.get("cost"), f.get("currency")
            if cost is None:
                continue
            if cur == quote:
                fee_quote += float(cost)
            elif cur == base:
                fee_base += float(cost)
                fee_quote += float(cost) * price
            else:
                unknown = True                          # ex. frais payés en BNB
        if unknown:
            fee_quote = notional * self.s.fee_rate_pct / 100

        qty = filled
        if side == "buy" and fee_base:
            qty = self.ex.amount_to_precision(symbol, filled - fee_base)   # ce qu'on détient vraiment
        return Fill(price, qty, fee_quote, str(order.get("id") or cid))
