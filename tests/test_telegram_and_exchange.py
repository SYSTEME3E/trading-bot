import asyncio
from types import SimpleNamespace

import pytest

from database.database import Database
from exchange.binance_client import BinanceClient, ExchangeError
from telegram_bot import commands as c


def test_is_authorized():
    assert c.is_authorized(1, (1, 2))
    assert not c.is_authorized(3, (1, 2))
    assert not c.is_authorized(None, (1,))
    assert not c.is_authorized(1, ())  # liste vide = personne


class FakeMsg:
    def __init__(self):
        self.sent = []

    async def reply_text(self, text, **kw):
        self.sent.append(text)


def make_update(uid):
    msg = FakeMsg()
    return SimpleNamespace(effective_user=SimpleNamespace(id=uid), effective_message=msg), msg


def make_ctx(allowed=(42,)):
    settings = SimpleNamespace(allowed_ids=allowed, mode="paper", starting_capital=80.0,
                               pairs=("BTC/USDT",), max_open_positions=2, timeframe="15m",
                               risk_per_trade_pct=1, stop_loss_pct=2, take_profit_pct=4,
                               max_daily_loss_pct=5, cooldown_minutes=60,
                               ai_enabled=False, supabase_sync=False)
    ex = SimpleNamespace(can_trade=False)
    return SimpleNamespace(bot_data={"settings": settings, "db": Database(":memory:"),
                                     "exchange": ex, "state": c.RobotState()})


def test_unauthorized_user_gets_no_reply():
    upd, msg = make_update(999)
    asyncio.run(c.cmd_status(upd, make_ctx()))
    assert msg.sent == []


def test_pause_resume_by_authorized_user():
    ctx = make_ctx()
    upd, msg = make_update(42)
    asyncio.run(c.cmd_pause(upd, ctx))
    assert ctx.bot_data["state"].paused is True
    asyncio.run(c.cmd_resume(upd, ctx))
    assert ctx.bot_data["state"].paused is False
    assert len(msg.sent) == 2


def test_paper_balance_reply():
    upd, msg = make_update(42)
    asyncio.run(c.cmd_balance(upd, make_ctx()))
    assert "80.00" in msg.sent[0]


def test_paper_mode_refuses_orders():
    cl = BinanceClient("paper", exchange=object())
    with pytest.raises(ExchangeError):
        cl.create_market_order("BTC/USDT", "buy", 0.001)


def test_invalid_order_inputs():
    cl = BinanceClient("testnet", exchange=object())
    with pytest.raises(ValueError):
        cl.create_market_order("BTC/USDT", "short", 1)
    with pytest.raises(ValueError):
        cl.create_market_order("BTC/USDT", "buy", 0)


def test_no_withdraw_method_exposed():
    assert not hasattr(BinanceClient, "withdraw")
    assert not hasattr(BinanceClient, "transfer")
