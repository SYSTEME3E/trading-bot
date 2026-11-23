from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pandas as pd
import pytest

import trading.trader as tr
from database.database import Database
from exchange.binance_client import BinanceClient, ExchangeError
from strategy.signals import Signal
from telegram_bot.commands import RobotState
from trading.risk_manager import RiskManager

NOW = datetime(2026, 11, 23, 12, 0, 20, tzinfo=timezone.utc)


def make_settings(**over):
    base = dict(mode="paper", starting_capital=80.0, risk_per_trade_pct=1.0,
                stop_loss_pct=2.0, take_profit_pct=4.0, max_open_positions=2,
                max_daily_loss_pct=5.0, cooldown_minutes=60, fee_rate_pct=0.1,
                pairs=("ETH/USDT",), timeframe="15m", loop_interval=60)
    base.update(over)
    return SimpleNamespace(**base)


def candles(last_closed_end=NOW - timedelta(seconds=20)):
    """3 bougies : la dernière (en cours) est ignorée, l'avant-dernière vient de se clôturer."""
    closed_start = last_closed_end - timedelta(minutes=15)
    idx = pd.DatetimeIndex([closed_start - timedelta(minutes=15), closed_start,
                            last_closed_end], tz="UTC")
    return pd.DataFrame({"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1.0},
                        index=idx)


class FakeEx:
    can_trade = False

    def __init__(self, price=2000.0, df=None):
        self.price, self.df = price, df if df is not None else candles()
        self.orders, self.order_calls = {}, 0
        self.create_error = None      # exception à lever à l'envoi
        self.find_error = None        # exception à lever à la recherche
        self.fee = {"cost": 0.04, "currency": "USDT"}
        self.balance = 1000.0

    def fetch_prices(self, symbols):
        return {s: self.price for s in symbols}

    def fetch_ohlcv(self, symbol, timeframe="15m", limit=200):
        return self.df

    def market_limits(self, symbol):
        return {"min_amount": 0.00001, "min_cost": 5.0}

    def amount_to_precision(self, symbol, amount):
        return int(amount * 1e5) / 1e5

    def fetch_free_balance(self, asset="USDT"):
        return self.balance

    def create_market_order(self, symbol, side, amount, client_order_id=None):
        self.order_calls += 1
        order = {"id": f"X{self.order_calls}", "filled": amount, "average": self.price,
                 "fee": dict(self.fee), "clientOrderId": client_order_id}
        self.orders[client_order_id] = order            # Binance l'a enregistré
        if self.create_error:
            raise self.create_error
        return order

    def find_order(self, symbol, client_order_id):
        if self.find_error:
            raise self.find_error
        return self.orders.get(client_order_id)


class Clock:
    def __init__(self, t=NOW):
        self.t = t

    def __call__(self):
        return self.t


def build(monkeypatch, action="BUY", ex=None, **over):
    s = make_settings(**over)
    db, state = Database(":memory:"), RobotState()
    ex = ex or FakeEx()
    ex.can_trade = s.mode != "paper"
    clock = Clock()
    holder = {"action": action}
    monkeypatch.setattr(tr, "evaluate", lambda sym, df: Signal(
        sym, holder["action"], 0.75, "up", ["test"], {"close": 1.0}))
    t = tr.Trader(s, db, ex, RiskManager(s, db, ex), state, now_fn=clock)
    return t, db, ex, state, clock, holder


# ---------- paper ----------
def test_paper_buy_uses_simulated_capital(monkeypatch):
    t, db, ex, *_ = build(monkeypatch)
    msgs = t.tick()
    pos = db.get_open_positions()
    assert len(pos) == 1 and pos[0]["mode"] == "paper"
    assert pos[0]["entry_price"] == 2000.0
    assert pos[0]["entry_price"] * pos[0]["quantity"] == pytest.approx(40.0, abs=0.05)
    assert pos[0]["stop_loss"] == pytest.approx(1960.0)
    assert pos[0]["take_profit"] == pytest.approx(2080.0)
    assert pos[0]["entry_fee"] == pytest.approx(0.04, abs=0.001)
    assert db.get_trades()[0]["side"] == "buy"
    assert any("ACHAT" in m for m in msgs)
    cash, equity = t.cash_and_equity()
    assert equity == 80.0 and cash == pytest.approx(80 - 40 - 0.04, abs=0.06)


def test_stop_loss_closes_with_loss(monkeypatch):
    t, db, ex, *_ = build(monkeypatch)
    t.tick()
    ex.price = 1950.0
    msgs = t.tick()
    assert db.get_open_positions() == []
    perf = db.performance()
    assert perf["realized_pnl"] < 0
    assert db.get_trades(1)[0]["side"] == "sell"
    assert any("Stop Loss" in m for m in msgs)


def test_take_profit_closes_with_gain(monkeypatch):
    t, db, ex, *_ = build(monkeypatch)
    t.tick()
    ex.price = 2100.0
    t.tick()
    assert db.performance()["realized_pnl"] > 0


def test_sell_signal_closes_on_new_candle(monkeypatch):
    t, db, ex, _, _, holder = build(monkeypatch)
    t.tick()
    holder["action"] = "SELL"
    ex.df = candles(NOW - timedelta(seconds=5))          # nouvelle bougie clôturée
    t.tick()
    assert db.get_open_positions() == []
    assert db.performance()["closed_trades"] == 1


def test_same_candle_is_evaluated_once(monkeypatch):
    t, db, *_ = build(monkeypatch, action="HOLD")
    t.tick()
    t.tick()
    assert len(db.get_signals(10)) == 1


def test_paused_blocks_buy_but_not_stop_loss(monkeypatch):
    t, db, ex, state, *_ = build(monkeypatch)
    t.tick()
    state.paused = True
    ex.price = 1950.0
    t.tick()
    assert db.get_open_positions() == []                 # SL exécuté malgré la pause
    ex.df = candles(NOW - timedelta(seconds=5))
    t.tick()
    assert db.get_open_positions() == []                 # pas de nouvel achat


def test_no_buy_on_stale_candle(monkeypatch):
    stale = FakeEx(df=candles(NOW - timedelta(minutes=10)))
    t, db, *_ = build(monkeypatch, ex=stale)
    t.tick()
    assert db.get_open_positions() == []
    assert len(db.get_signals()) == 1                    # le signal est quand même enregistré


def test_no_duplicate_buy_on_same_symbol(monkeypatch):
    t, db, ex, *_ = build(monkeypatch)
    t.tick()
    ex.df = candles(NOW - timedelta(seconds=5))
    t.tick()
    assert len(db.get_open_positions()) == 1


def test_circuit_breaker_blocks_buy_and_alerts_once(monkeypatch):
    t, db, ex, *_ = build(monkeypatch)
    pid = db.open_position("BTC/USDT", "paper", 100, 1, 98, 104)
    db.close_position(pid, 95.0, 0, "stop_loss")          # perte > 5 % du jour (heure réelle)
    t._now = lambda: datetime.now(timezone.utc)
    ex.df = candles(datetime.now(timezone.utc) - timedelta(seconds=5))
    m1 = t.tick()
    assert db.get_open_positions() == []
    assert any("Coupe-circuit" in m for m in m1)
    ex.df = candles(datetime.now(timezone.utc) - timedelta(seconds=2))
    assert not any("Coupe-circuit" in m for m in t.tick())


def test_price_outage_returns_alert_without_crash(monkeypatch):
    t, _, ex, state, *_ = build(monkeypatch)
    ex.fetch_prices = lambda s: (_ for _ in ()).throw(ExchangeError("fetch_tickers : NetworkError"))
    msgs = t.tick()
    assert state.last_error and any("Erreur" in m for m in msgs)


# ---------- testnet / live ----------
def test_live_buy_sends_client_order_id_and_records_trade(monkeypatch):
    t, db, ex, *_ = build(monkeypatch, mode="testnet")
    t.tick()
    assert ex.order_calls == 1
    cid = next(iter(ex.orders))
    assert cid.startswith("rb") and len(cid) <= 36
    assert db.get_open_positions()[0]["mode"] == "testnet"


def test_fee_paid_in_base_asset_reduces_held_quantity(monkeypatch):
    ex = FakeEx()
    ex.fee = {"cost": 0.00002, "currency": "ETH"}
    t, db, *_ = build(monkeypatch, mode="testnet", ex=ex)
    t.tick()
    pos = db.get_open_positions()[0]
    assert pos["quantity"] == pytest.approx(0.01999, abs=1e-5)   # 0.02 - 0.00002 -> arrondi bas
    assert pos["entry_fee"] == pytest.approx(0.00002 * 2000)


def test_live_cash_is_capped_by_allocated_budget(monkeypatch):
    t, db, ex, *_ = build(monkeypatch, mode="testnet")
    ex.balance = 10000.0                                  # le compte contient bien plus
    cash, equity = t.cash_and_equity()
    assert cash == 80.0 and equity == 80.0


def test_live_cash_is_capped_by_real_balance(monkeypatch):
    t, db, ex, *_ = build(monkeypatch, mode="testnet")
    ex.balance = 12.0
    assert t.cash_and_equity()[0] == 12.0


def test_order_timeout_but_order_exists_is_recorded_without_resend(monkeypatch):
    ex = FakeEx()
    ex.create_error = ExchangeError("create_order : RequestTimeout")
    t, db, *_ = build(monkeypatch, mode="testnet", ex=ex)
    t.tick()
    assert ex.order_calls == 1                           # jamais renvoyé
    assert len(db.get_open_positions()) == 1             # retrouvé chez Binance


def test_order_error_and_order_unknown_to_binance_means_not_placed(monkeypatch):
    ex = FakeEx()
    ex.create_error = ExchangeError("create_order : InsufficientFunds")
    ex.orders = {}

    def failing(symbol, side, amount, client_order_id=None):
        ex.order_calls += 1
        raise ex.create_error                            # rien enregistré chez Binance
    ex.create_market_order = failing
    t, db, *_ = build(monkeypatch, mode="testnet", ex=ex)
    t.tick()
    assert db.get_open_positions() == [] and ex.order_calls == 1


def test_unknown_order_state_pauses_and_resolves_next_cycle(monkeypatch):
    ex = FakeEx()
    ex.create_error = ExchangeError("create_order : RequestTimeout")
    ex.find_error = ExchangeError("fetch_order : NetworkError")
    t, db, _, state, *_ = build(monkeypatch, mode="testnet", ex=ex)
    msgs = t.tick()
    assert state.paused and any("ALERTE" in m for m in msgs)
    assert db.get_open_positions() == [] and ex.order_calls == 1
    ex.find_error = None                                  # la connexion revient
    t.tick()
    assert len(db.get_open_positions()) == 1              # ordre retrouvé et enregistré
    assert state.paused                                   # reprise manuelle seulement
    assert ex.order_calls == 1


def test_failed_sell_keeps_position_open_and_retries(monkeypatch):
    t, db, ex, *_ = build(monkeypatch, mode="testnet")
    t.tick()
    ex.price = 1950.0
    ex.create_error = ExchangeError("create_order : NetworkError")
    ex.orders.clear()

    def failing(symbol, side, amount, client_order_id=None):
        ex.order_calls += 1
        raise ex.create_error
    ex.create_market_order = failing
    msgs = t.tick()
    assert len(db.get_open_positions()) == 1
    assert any("non exécutée" in m for m in msgs)
    assert not any("non exécutée" in m for m in t.tick())  # pas de spam dans les 30 min


def test_positions_of_other_mode_are_not_managed(monkeypatch):
    t, db, ex, *_ = build(monkeypatch, action="HOLD", mode="testnet")
    db.open_position("ETH/USDT", "paper", 2000, 0.02, 1960, 2080)
    ex.price = 1000.0                                     # SL paper dépassé
    t.tick()
    assert len(db.get_open_positions()) == 1 and ex.order_calls == 0


def test_tf_seconds():
    assert tr.tf_seconds("15m") == 900 and tr.tf_seconds("1h") == 3600


# ---------- client Binance : find_order ----------
def test_find_order_returns_none_when_not_found():
    import ccxt

    class Ex:
        def fetch_order(self, *a, **k):
            raise ccxt.OrderNotFound("unknown order")
    assert BinanceClient("testnet", exchange=Ex()).find_order("ETH/USDT", "rbabc") is None


def test_find_order_raises_when_unknowable():
    import ccxt

    class Ex:
        def fetch_order(self, *a, **k):
            raise ccxt.AuthenticationError("bad key")
    with pytest.raises(ExchangeError):
        BinanceClient("testnet", exchange=Ex()).find_order("ETH/USDT", "rbabc")


def test_find_order_passes_client_id():
    seen = {}

    class Ex:
        def fetch_order(self, oid, symbol, params):
            seen.update(oid=oid, symbol=symbol, params=params)
            return {"id": "1", "filled": 1}
    BinanceClient("testnet", exchange=Ex()).find_order("ETH/USDT", "rbabc")
    assert seen["params"] == {"origClientOrderId": "rbabc"} and seen["oid"] is None


def test_find_order_in_paper_mode_is_noop():
    assert BinanceClient("paper", exchange=object()).find_order("ETH/USDT", "x") is None
