from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from database.database import Database
from trading.risk_manager import RiskManager

NOW = datetime(2026, 11, 23, 12, 0, tzinfo=timezone.utc)


def make_settings(**over):
    base = dict(mode="paper", starting_capital=80.0, risk_per_trade_pct=1.0,
                stop_loss_pct=2.0, take_profit_pct=4.0, max_open_positions=2,
                max_daily_loss_pct=5.0, cooldown_minutes=60, fee_rate_pct=0.1)
    base.update(over)
    return SimpleNamespace(**base)


class FakeEx:
    """Imite la précision Binance : arrondi à la baisse à 5 décimales."""
    def __init__(self, min_cost=5.0, min_amount=0.00001):
        self.limits = {"min_amount": min_amount, "min_cost": min_cost}

    def amount_to_precision(self, symbol, amount):
        return int(amount * 1e5) / 1e5

    def market_limits(self, symbol):
        return self.limits


@pytest.fixture
def db():
    return Database(":memory:")


def rm(db, ex=None, **over):
    return RiskManager(make_settings(**over), db, ex or FakeEx())


def test_sizing_with_80_usdt(db):
    d = rm(db).check_buy("ETH/USDT", 2000.0, cash=80, equity=80, now=NOW)
    assert d.allowed
    # risque 1 % de 80 = 0.8 USDT ; SL 2 % -> position de 40 USDT
    assert d.notional == pytest.approx(40.0, abs=0.05)
    assert d.quantity == pytest.approx(0.02, abs=1e-5)
    assert d.stop_loss == pytest.approx(1960.0)
    assert d.take_profit == pytest.approx(2080.0)


def test_quantity_is_rounded_down(db):
    d = rm(db).check_buy("BTC/USDT", 61297.82, cash=80, equity=80, now=NOW)
    assert d.allowed
    assert d.quantity * 61297.82 <= 40.0  # jamais au-dessus de la cible
    assert d.quantity == int(d.quantity * 1e5) / 1e5


def test_notional_capped_by_equity_share(db):
    # risque 5 % / SL 2 % = 200 % du capital : plafonné à capital / positions max
    d = rm(db, risk_per_trade_pct=5.0).check_buy("ETH/USDT", 2000.0, 80, 80, NOW)
    assert d.allowed and d.notional <= 40.0 + 1e-6


def test_notional_capped_by_cash_with_fees(db):
    d = rm(db).check_buy("ETH/USDT", 2000.0, cash=10, equity=80, now=NOW)
    assert d.allowed
    assert d.notional * 1.001 <= 10


def test_refuses_order_below_exchange_minimum(db):
    d = rm(db, ex=FakeEx(min_cost=50.0)).check_buy("ETH/USDT", 2000.0, 80, 80, NOW)
    assert not d.allowed and "trop petit" in d.reason


def test_refuses_without_cash(db):
    assert not rm(db).check_buy("ETH/USDT", 2000.0, cash=0, equity=80, now=NOW).allowed


def test_max_open_positions(db):
    db.open_position("BTC/USDT", "paper", 100, 1, 98, 104)
    db.open_position("ETH/USDT", "paper", 100, 1, 98, 104)
    d = rm(db).check_buy("SOL/USDT", 70.0, 80, 80, NOW)
    assert not d.allowed and "maximum" in d.reason


def test_no_duplicate_position_on_same_symbol(db):
    db.open_position("BTC/USDT", "paper", 100, 1, 98, 104)
    d = rm(db).check_buy("BTC/USDT", 61000.0, 80, 80, NOW)
    assert not d.allowed and "déjà ouverte" in d.reason


def test_positions_of_other_mode_are_ignored(db):
    db.open_position("BTC/USDT", "live", 100, 1, 98, 104)
    assert rm(db, mode="paper").check_buy("BTC/USDT", 61000.0, 80, 80, NOW).allowed


def test_cooldown_after_close(db):
    pid = db.open_position("BTC/USDT", "paper", 100, 1, 98, 104)
    db.close_position(pid, 99, 0, "stop_loss")
    closed = datetime.fromisoformat(db.last_closed_at("BTC/USDT"))
    soon = closed + timedelta(minutes=10)
    later = closed + timedelta(minutes=61)
    assert "Pause" in rm(db).check_buy("BTC/USDT", 61000.0, 80, 80, soon).reason
    assert rm(db).check_buy("BTC/USDT", 61000.0, 80, 80, later).allowed


def test_daily_loss_circuit_breaker(db):
    pid = db.open_position("ETH/USDT", "paper", 100, 1, 98, 104)
    db.close_position(pid, 95.0, 0, "stop_loss")          # -5 USDT > 5 % de 80 (= 4)
    now = datetime.now(timezone.utc)
    r = rm(db, cooldown_minutes=0)
    assert r.daily_loss_hit(now)
    assert "Coupe-circuit" in r.check_buy("SOL/USDT", 70.0, 80, 80, now).reason


def test_small_loss_does_not_trigger_breaker(db):
    pid = db.open_position("ETH/USDT", "paper", 100, 1, 98, 104)
    db.close_position(pid, 99.0, 0, "stop_loss")          # -1 USDT < 4
    assert not rm(db).daily_loss_hit(datetime.now(timezone.utc))


def test_refuses_when_exchange_limits_unavailable(db):
    class Broken(FakeEx):
        def market_limits(self, symbol):
            raise RuntimeError("marchés non chargés")
    d = rm(db, ex=Broken()).check_buy("ETH/USDT", 2000.0, 80, 80, NOW)
    assert not d.allowed


def test_exit_reasons_priority():
    r = RiskManager(make_settings(), None, FakeEx())
    pos = {"stop_loss": 98.0, "take_profit": 104.0}
    assert r.exit_reason(pos, 97.9) == "stop_loss"
    assert r.exit_reason(pos, 98.0) == "stop_loss"        # SL touché pile
    assert r.exit_reason(pos, 104.0) == "take_profit"
    assert r.exit_reason(pos, 100.0) is None
    assert r.exit_reason(pos, 100.0, "SELL") == "signal_sell"
    assert r.exit_reason(pos, 97.0, "SELL") == "stop_loss"  # le SL passe avant le signal


def test_realized_pnl_since(db):
    pid = db.open_position("ETH/USDT", "paper", 100, 1, 98, 104)
    db.close_position(pid, 103.0, 0, "take_profit")
    assert db.realized_pnl_since("2000-01-01T00:00:00+00:00") == pytest.approx(3.0)
    assert db.realized_pnl_since("2999-01-01T00:00:00+00:00") == 0.0
