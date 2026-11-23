"""Client Binance Spot via CCXT.

Règles de sécurité :
- Aucune méthode de retrait n'est exposée (withdraw/transfer absents).
- Testnet : set_sandbox_mode(True) -> https://testnet.binance.vision/api/v3
- Mode paper : données de marché publiques réelles, AUCUN ordre envoyé.
- Erreurs réseau : nouvelles tentatives avec attente croissante.
"""
from __future__ import annotations

import logging
import time
from functools import wraps

import ccxt
import pandas as pd

log = logging.getLogger(__name__)

RETRYABLE = (ccxt.NetworkError, ccxt.RateLimitExceeded, ccxt.RequestTimeout)


class ExchangeError(Exception):
    pass


def with_retry(attempts: int = 3, base_delay: float = 1.0):
    def deco(fn):
        @wraps(fn)
        def wrapper(*a, **kw):
            for i in range(attempts):
                try:
                    return fn(*a, **kw)
                except RETRYABLE as e:
                    if i == attempts - 1:
                        raise ExchangeError(f"{fn.__name__} : {type(e).__name__}") from e
                    time.sleep(base_delay * 2 ** i)
                except ccxt.AuthenticationError as e:
                    raise ExchangeError("Authentification Binance refusée (clé/IP/droits)") from e
                except ccxt.BaseError as e:
                    raise ExchangeError(f"{fn.__name__} : {type(e).__name__}") from e
        return wrapper
    return deco


class BinanceClient:
    def __init__(self, mode: str, api_key: str = "", api_secret: str = "", exchange=None):
        if mode not in {"paper", "testnet", "live"}:
            raise ValueError("mode invalide")
        self.mode = mode
        if exchange is not None:  # injection pour les tests
            self.ex = exchange
            return
        params = {"enableRateLimit": True, "options": {"defaultType": "spot"}}
        if mode in {"testnet", "live"}:
            params.update(apiKey=api_key, secret=api_secret)
        self.ex = ccxt.binance(params)
        if mode == "testnet":
            self.ex.set_sandbox_mode(True)

    @property
    def can_trade(self) -> bool:
        return self.mode in {"testnet", "live"}

    @with_retry()
    def check_connection(self) -> int:
        """Retourne l'heure serveur Binance (ms). Lève ExchangeError si injoignable."""
        return self.ex.fetch_time()

    @with_retry()
    def fetch_prices(self, symbols: list[str]) -> dict[str, float]:
        tickers = self.ex.fetch_tickers(symbols)
        return {s: float(t["last"]) for s, t in tickers.items() if t.get("last")}

    @with_retry()
    def fetch_ohlcv(self, symbol: str, timeframe: str = "15m", limit: int = 200) -> pd.DataFrame:
        raw = self.ex.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
        df = pd.DataFrame(raw, columns=["ts", "open", "high", "low", "close", "volume"])
        df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
        return df.set_index("ts")

    @with_retry()
    def fetch_free_balance(self, asset: str = "USDT") -> float:
        if not self.can_trade:
            raise ExchangeError("Solde réel indisponible en mode paper")
        bal = self.ex.fetch_balance()
        return float(bal.get(asset, {}).get("free", 0.0))

    def market_limits(self, symbol: str) -> dict:
        """Minimums de l'exchange (quantité, valeur minimale d'un ordre)."""
        self._ensure_markets()
        m = self.ex.market(symbol)
        return {
            "min_amount": (m["limits"]["amount"] or {}).get("min") or 0.0,
            "min_cost": (m["limits"]["cost"] or {}).get("min") or 0.0,
        }

    def amount_to_precision(self, symbol: str, amount: float) -> float:
        self._ensure_markets()
        return float(self.ex.amount_to_precision(symbol, amount))

    @with_retry()
    def _ensure_markets(self) -> None:
        if not getattr(self.ex, "markets", None):
            self.ex.load_markets()

    @with_retry(attempts=1)  # pas de retry automatique sur un ordre : risque de doublon
    def create_market_order(self, symbol: str, side: str, amount: float,
                            client_order_id: str | None = None) -> dict:
        if not self.can_trade:
            raise ExchangeError("Ordres interdits en mode paper")
        if side not in {"buy", "sell"}:
            raise ValueError("side doit être 'buy' ou 'sell'")
        if amount <= 0:
            raise ValueError("quantité invalide")
        params = {"newClientOrderId": client_order_id} if client_order_id else {}
        return self.ex.create_order(symbol, "market", side, amount, None, params)
