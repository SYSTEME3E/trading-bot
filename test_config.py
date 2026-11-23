import pytest
import config


def test_live_refused_without_confirmation(monkeypatch):
    monkeypatch.setenv("TRADING_MODE", "live")
    monkeypatch.delenv("LIVE_CONFIRMATION", raising=False)
    with pytest.raises(config.ConfigError):
        config.load_settings()


def test_paper_defaults(monkeypatch):
    monkeypatch.setenv("TRADING_MODE", "paper")
    s = config.load_settings()
    assert s.stop_loss_pct > 0 and s.take_profit_pct > s.stop_loss_pct


def test_tp_must_exceed_sl(monkeypatch):
    monkeypatch.setenv("TRADING_MODE", "paper")
    monkeypatch.setenv("STOP_LOSS_PCT", "5")
    monkeypatch.setenv("TAKE_PROFIT_PCT", "3")
    with pytest.raises(config.ConfigError):
        config.load_settings()
