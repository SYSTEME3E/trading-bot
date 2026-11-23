import pytest
from database.database import Database
from database.supabase_sync import SupabaseSync


@pytest.fixture
def db():
    return Database(":memory:", sync_enabled=True)


def test_pnl_includes_fees(db):
    pid = db.open_position("BTC/USDT", "paper", 100.0, 1.0, 98.0, 104.0, entry_fee=0.1)
    pnl = db.close_position(pid, 104.0, 0.1, "take_profit")
    assert pnl == pytest.approx(3.8)
    perf = db.performance()
    assert perf["closed_trades"] == 1 and perf["win_rate_pct"] == 100.0
    assert perf["total_fees"] == pytest.approx(0.2)


def test_cannot_close_twice(db):
    pid = db.open_position("ETH/USDT", "paper", 10, 1, 9, 12)
    db.close_position(pid, 12, 0, "tp")
    with pytest.raises(ValueError):
        db.close_position(pid, 12, 0, "tp")


def test_duplicate_order_id_rejected(db):
    assert db.add_trade("BTC/USDT", "buy", 1, 1, 0, "paper", order_id="A1")
    assert db.add_trade("BTC/USDT", "buy", 1, 1, 0, "paper", order_id="A1") is None
    assert len(db.get_trades()) == 1


class FakeTable:
    def __init__(self, client, name, fail):
        self.c, self.name, self.fail = client, name, fail

    def upsert(self, rows, on_conflict=None):
        self.rows = rows
        return self

    def execute(self):
        if self.fail:
            raise ConnectionError("réseau coupé")
        self.c.sent.setdefault(self.name, []).extend(self.rows)


class FakeClient:
    def __init__(self, fail=False):
        self.sent, self.fail = {}, fail

    def table(self, name):
        return FakeTable(self, name, self.fail)


def test_sync_success_clears_queue(db):
    pid = db.open_position("BTC/USDT", "paper", 100, 1, 98, 104)
    db.close_position(pid, 104, 0, "tp")  # 2 versions de la même ligne
    client = FakeClient()
    assert SupabaseSync(db, client).flush() == 1  # dédupliquées
    assert client.sent["positions"][0]["status"] == "closed"
    assert db.pending_sync() == []


def test_sync_failure_keeps_queue_and_never_raises(db):
    db.add_signal("BTC/USDT", "BUY", 0.7, {"rsi": 40})
    assert SupabaseSync(db, FakeClient(fail=True)).flush() == 0
    assert len(db.pending_sync()) == 1
