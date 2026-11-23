"""Moteur de signaux BUY / SELL / HOLD par confirmation multi-indicateurs.

ACHAT : il faut une tendance haussière (obligatoire) ET au moins
`min_confirmations` conditions sur 4. Des filtres bloquants (tendance baissière,
RSI suracheté, données insuffisantes) empêchent tout achat.
VENTE : signal de sortie complémentaire (le Stop Loss / Take Profit restent
gérés par risk_manager / trader).

Ce module ne passe aucun ordre. Il décrit seulement le marché.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from strategy.indicators import add_indicators

MIN_CANDLES = 60


@dataclass(frozen=True)
class SignalParams:
    ema_fast: int = 20
    ema_slow: int = 50
    rsi_period: int = 14
    vol_period: int = 20
    rsi_buy_min: float = 40.0       # en dessous : momentum trop faible
    rsi_overbought: float = 70.0    # au-dessus : achat interdit
    rsi_sell: float = 75.0          # au-dessus : signal de sortie
    min_volume_ratio: float = 1.0
    min_confirmations: int = 3      # sur 4, dont la tendance haussière


@dataclass
class Signal:
    symbol: str
    action: str                     # "BUY" | "SELL" | "HOLD"
    score: float                    # confirmations d'achat / 4
    trend: str                      # "up" | "down" | "range" | "unknown"
    reasons: list[str] = field(default_factory=list)
    snapshot: dict = field(default_factory=dict)


def evaluate(symbol: str, df: pd.DataFrame, params: SignalParams = SignalParams(),
             drop_incomplete: bool = True) -> Signal:
    """Analyse la dernière bougie CLÔTURÉE.

    CCXT renvoie la bougie en cours en dernier : on l'ignore par défaut pour ne
    pas décider sur une bougie incomplète.
    """
    data = df.iloc[:-1] if drop_incomplete else df
    if len(data) < MIN_CANDLES:
        return Signal(symbol, "HOLD", 0.0, "unknown",
                      [f"Données insuffisantes ({len(data)}/{MIN_CANDLES} bougies)"])

    d = add_indicators(data, params.ema_fast, params.ema_slow,
                       params.rsi_period, params.vol_period)
    cur, prev = d.iloc[-1], d.iloc[-2]
    if cur[["ema_fast", "ema_slow", "rsi", "macd_hist", "vol_ratio"]].isna().any():
        return Signal(symbol, "HOLD", 0.0, "unknown", ["Indicateurs incomplets"])

    slow_rising = cur["ema_slow"] > d["ema_slow"].iloc[-6]
    if cur["ema_fast"] > cur["ema_slow"] and slow_rising:
        trend = "up"
    elif cur["ema_fast"] < cur["ema_slow"]:
        trend = "down"
    else:
        trend = "range"

    snap = {k: float(cur[k]) for k in
            ("close", "ema_fast", "ema_slow", "rsi", "macd_hist", "vol_ratio")}

    # --- signal de sortie ---
    if (trend == "down" and cur["macd_hist"] < 0) or cur["rsi"] >= params.rsi_sell:
        why = ("RSI très élevé (surachat)" if cur["rsi"] >= params.rsi_sell
               else "Tendance baissière confirmée par le MACD")
        return Signal(symbol, "SELL", 0.0, trend, [why], snap)

    # --- conditions d'achat ---
    checks = {
        "Tendance haussière (EMA20 > EMA50, EMA50 en hausse)": trend == "up",
        f"RSI entre {params.rsi_buy_min:.0f} et {params.rsi_overbought:.0f} et en hausse":
            params.rsi_buy_min <= cur["rsi"] < params.rsi_overbought and cur["rsi"] > prev["rsi"],
        "MACD positif et en progression":
            cur["macd_hist"] > 0 and cur["macd_hist"] > prev["macd_hist"],
        f"Volume ≥ {params.min_volume_ratio:.1f}x la moyenne":
            cur["vol_ratio"] >= params.min_volume_ratio,
    }
    ok = [k for k, v in checks.items() if v]
    score = len(ok) / len(checks)

    # --- filtres bloquants ---
    if trend == "down":
        return Signal(symbol, "HOLD", score, trend, ["Tendance baissière : achat interdit"], snap)
    if cur["rsi"] >= params.rsi_overbought:
        return Signal(symbol, "HOLD", score, trend, ["RSI suracheté : achat interdit"], snap)

    if checks["Tendance haussière (EMA20 > EMA50, EMA50 en hausse)"] \
            and len(ok) >= params.min_confirmations:
        return Signal(symbol, "BUY", score, trend, ok, snap)
    return Signal(symbol, "HOLD", score, trend,
                  [f"Confirmations insuffisantes ({len(ok)}/{len(checks)})"], snap)
