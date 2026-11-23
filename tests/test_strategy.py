import numpy as np
import pandas as pd
import pytest

from strategy import indicators as ind
from strategy.signals import MIN_CANDLES, SignalParams, evaluate


def make_df(close, volume=None):
    close = np.asarray(close, dtype=float)
    vol = np.full(len(close), 100.0) if volume is None else np.asarray(volume, dtype=float)
    idx = pd.date_range("2026-01-01", periods=len(close), freq="15min", tz="UTC")
    return pd.DataFrame({"open": close, "high": close, "low": close,
                         "close": close, "volume": vol}, index=idx)


def test_rsi_extremes():
    up = pd.Series(np.arange(1, 60, dtype=float))
    assert ind.rsi(up).iloc[-1] == 100.0
    down = pd.Series(np.arange(60, 1, -1, dtype=float))
    assert ind.rsi(down).iloc[-1] < 1.0
    flat = pd.Series(np.full(60, 10.0))
    assert ind.rsi(flat).iloc[-1] == 50.0


def test_rsi_always_between_0_and_100():
    rng = np.random.default_rng(1)
    s = pd.Series(100 + rng.normal(0, 1, 300).cumsum())
    r = ind.rsi(s).dropna()
    assert ((r >= 0) & (r <= 100)).all()


def test_ema_of_constant_is_constant():
    assert ind.ema(pd.Series(np.full(50, 7.0)), 20).iloc[-1] == pytest.approx(7.0)


def test_macd_hist_positive_in_accelerating_uptrend():
    s = pd.Series(np.linspace(100, 200, 120) ** 1.0 + np.arange(120) ** 1.3)
    _, _, hist = ind.macd(s)
    assert hist.iloc[-1] > 0


def test_insufficient_data_is_hold():
    sig = evaluate("BTC/USDT", make_df(np.linspace(100, 110, MIN_CANDLES - 1)))
    assert sig.action == "HOLD" and sig.trend == "unknown"


def test_downtrend_never_buys():
    close = np.linspace(200, 100, 150)
    sig = evaluate("BTC/USDT", make_df(close))
    assert sig.action != "BUY"


def test_strong_overbought_uptrend_is_not_a_buy():
    close = np.linspace(100, 400, 150)  # hausse continue : RSI ~100
    sig = evaluate("BTC/USDT", make_df(close))
    assert sig.action != "BUY"


def test_buy_on_pullback_recovery_with_volume():
    rng = np.random.default_rng(7)
    base = np.linspace(100, 150, 120) + rng.normal(0, 0.4, 120)
    pullback = np.linspace(150, 141, 15)
    recovery = np.linspace(141, 145, 8)
    close = np.concatenate([base, pullback, recovery, [145.3]])  # dernière = bougie en cours
    vol = np.full(len(close), 100.0)
    vol[-9:] = 160.0
    sig = evaluate("ETH/USDT", make_df(close, vol))
    assert sig.action == "BUY", (sig.action, sig.reasons, sig.snapshot)
    assert sig.score == 1.0


def test_incomplete_candle_is_ignored():
    close = np.linspace(100, 110, 100)
    df = make_df(close)
    a = evaluate("X", df)
    spiked = df.copy()
    spiked.iloc[-1, spiked.columns.get_loc("close")] = 1.0  # bougie en cours aberrante
    b = evaluate("X", spiked)
    assert (a.action, a.snapshot["close"]) == (b.action, b.snapshot["close"])


def test_sell_signal_on_confirmed_downtrend():
    close = np.concatenate([np.linspace(100, 150, 80), np.linspace(150, 110, 60)])
    sig = evaluate("SOL/USDT", make_df(close), SignalParams())
    assert sig.action == "SELL"
