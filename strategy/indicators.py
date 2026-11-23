"""Indicateurs techniques (pandas/NumPy) : EMA, RSI (Wilder), MACD, volume relatif.

Fonctions pures, sans accès réseau : faciles à tester.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def ema(close: pd.Series, span: int) -> pd.Series:
    return close.ewm(span=span, adjust=False).mean()


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """RSI de Wilder. 100 si aucune baisse, 0 si aucune hausse, 50 si prix plat."""
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    out = 100 - 100 / (1 + rs)
    out = out.where(avg_loss != 0.0, 100.0)               # aucune baisse
    out = out.where(~((avg_gain == 0.0) & (avg_loss == 0.0)), 50.0)  # prix plat
    return out.where(avg_gain.notna() & avg_loss.notna())


def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    line = ema(close, fast) - ema(close, slow)
    sig = line.ewm(span=signal, adjust=False).mean()
    return line, sig, line - sig


def volume_ratio(volume: pd.Series, period: int = 20) -> pd.Series:
    """Volume actuel / moyenne des `period` bougies précédentes."""
    return volume / volume.rolling(period).mean().shift(1)


def add_indicators(df: pd.DataFrame, ema_fast: int = 20, ema_slow: int = 50,
                   rsi_period: int = 14, vol_period: int = 20) -> pd.DataFrame:
    """Retourne une copie de df (colonnes close, volume requises) avec les indicateurs."""
    out = df.copy()
    out["ema_fast"] = ema(out["close"], ema_fast)
    out["ema_slow"] = ema(out["close"], ema_slow)
    out["rsi"] = rsi(out["close"], rsi_period)
    out["macd"], out["macd_signal"], out["macd_hist"] = macd(out["close"])
    out["vol_ratio"] = volume_ratio(out["volume"], vol_period)
    return out
