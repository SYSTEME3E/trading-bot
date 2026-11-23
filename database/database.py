"""Base SQLite (source de vérité locale) avec file d'attente de synchronisation.

Le robot n'écrit QUE dans SQLite. Chaque écriture est placée dans `sync_queue`
(pattern "outbox") ; supabase_sync.py l'envoie ensuite en arrière-plan.
Une panne réseau ou Supabase ne bloque donc jamais le trading.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

SYNCED_TABLES = ("positions", "trades", "signals")

SCHEMA = """
CREATE TABLE IF NOT EXISTS positions (
    id TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    mode TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    entry_price REAL NOT NULL,
    quantity REAL NOT NULL,
    stop_loss REAL NOT NULL,
    take_profit REAL NOT NULL,
    entry_fee REAL NOT NULL DEFAULT 0,
    opened_at TEXT NOT NULL,
    exit_price REAL,
    exit_fee REAL,
    closed_at TEXT,
    close_reason TEXT,
    pnl_net REAL
);
CREATE TABLE IF NOT EXISTS trades (
    id TEXT PRIMARY KEY,
    position_id TEXT,
    order_id TEXT UNIQUE,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    price REAL NOT NULL,
    quantity REAL NOT NULL,
    fee REAL NOT NULL DEFAULT 0,
    mode TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS signals (
    id TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    signal TEXT NOT NULL,
    score REAL,
    details TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sync_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tbl TEXT NOT NULL,
    row_id TEXT NOT NULL,
    payload TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_pos_status ON positions(status);
CREATE INDEX IF NOT EXISTS idx_trades_created ON trades(created_at);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: str = "data/robot.db", sync_enabled: bool = False):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self.sync_enabled = sync_enabled
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    # ---------- utilitaires internes ----------
    def _enqueue(self, tbl: str, row_id: str) -> None:
        if not self.sync_enabled:
            return
        row = self._conn.execute(f"SELECT * FROM {tbl} WHERE id=?", (row_id,)).fetchone()
        if row:
            self._conn.execute(
                "INSERT INTO sync_queue (tbl,row_id,payload) VALUES (?,?,?)",
                (tbl, row_id, json.dumps(dict(row))),
            )

    def enqueue_all(self) -> None:
        """Rattrapage : met en file tout l'existant (à appeler à l'activation du sync)."""
        with self._lock:
            for tbl in SYNCED_TABLES:
                for r in self._conn.execute(f"SELECT id FROM {tbl}").fetchall():
                    self._enqueue(tbl, r["id"])
            self._conn.commit()

    # ---------- positions ----------
    def open_position(self, symbol, mode, entry_price, quantity, stop_loss,
                      take_profit, entry_fee=0.0) -> str:
        pid = str(uuid.uuid4())
        with self._lock:
            self._conn.execute(
                "INSERT INTO positions (id,symbol,mode,entry_price,quantity,stop_loss,"
                "take_profit,entry_fee,opened_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (pid, symbol, mode, entry_price, quantity, stop_loss,
                 take_profit, entry_fee, _now()),
            )
            self._enqueue("positions", pid)
            self._conn.commit()
        return pid

    def close_position(self, position_id, exit_price, exit_fee, reason) -> float:
        """Clôture et retourne le PnL net (frais d'entrée et de sortie inclus)."""
        with self._lock:
            p = self._conn.execute(
                "SELECT * FROM positions WHERE id=? AND status='open'", (position_id,)
            ).fetchone()
            if p is None:
                raise ValueError("Position introuvable ou déjà clôturée")
            pnl = ((exit_price - p["entry_price"]) * p["quantity"]
                   - p["entry_fee"] - exit_fee)
            self._conn.execute(
                "UPDATE positions SET status='closed',exit_price=?,exit_fee=?,"
                "closed_at=?,close_reason=?,pnl_net=? WHERE id=?",
                (exit_price, exit_fee, _now(), reason, pnl, position_id),
            )
            self._enqueue("positions", position_id)
            self._conn.commit()
        return pnl

    def get_open_positions(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM positions WHERE status='open' ORDER BY opened_at"
            ).fetchall()
        return [dict(r) for r in rows]

    def last_closed_at(self, symbol: str) -> str | None:
        """Sert à l'anti-rachat (cooldown) dans risk_manager."""
        with self._lock:
            r = self._conn.execute(
                "SELECT MAX(closed_at) AS t FROM positions WHERE symbol=? AND status='closed'",
                (symbol,),
            ).fetchone()
        return r["t"]

    # ---------- trades ----------
    def add_trade(self, symbol, side, price, quantity, fee, mode,
                  position_id=None, order_id=None) -> str | None:
        """Retourne None si order_id déjà enregistré (anti-doublon)."""
        tid = str(uuid.uuid4())
        with self._lock:
            try:
                self._conn.execute(
                    "INSERT INTO trades (id,position_id,order_id,symbol,side,price,"
                    "quantity,fee,mode,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (tid, position_id, order_id, symbol, side, price, quantity,
                     fee, mode, _now()),
                )
            except sqlite3.IntegrityError:
                self._conn.rollback()
                return None
            self._enqueue("trades", tid)
            self._conn.commit()
        return tid

    def get_trades(self, limit: int = 10) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM trades ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    # ---------- signaux ----------
    def add_signal(self, symbol, signal, score=None, details=None) -> str:
        sid = str(uuid.uuid4())
        with self._lock:
            self._conn.execute(
                "INSERT INTO signals (id,symbol,signal,score,details,created_at) "
                "VALUES (?,?,?,?,?,?)",
                (sid, symbol, signal, score,
                 json.dumps(details) if details is not None else None, _now()),
            )
            self._enqueue("signals", sid)
            self._conn.commit()
        return sid

    def get_signals(self, limit: int = 10) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM signals ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    # ---------- performances (réalisé uniquement ; frais inclus) ----------
    def performance(self) -> dict:
        with self._lock:
            r = self._conn.execute(
                "SELECT COUNT(*) AS n, COALESCE(SUM(pnl_net),0) AS pnl, "
                "COALESCE(SUM(CASE WHEN pnl_net>0 THEN 1 ELSE 0 END),0) AS wins, "
                "COALESCE(SUM(entry_fee+COALESCE(exit_fee,0)),0) AS fees "
                "FROM positions WHERE status='closed'"
            ).fetchone()
        n = r["n"]
        return {
            "closed_trades": n,
            "realized_pnl": r["pnl"],
            "win_rate_pct": (r["wins"] / n * 100) if n else 0.0,
            "total_fees": r["fees"],
        }

    def realized_pnl_since(self, iso_ts: str) -> float:
        """PnL net réalisé depuis un instant UTC (coupe-circuit de perte journalière)."""
        with self._lock:
            r = self._conn.execute(
                "SELECT COALESCE(SUM(pnl_net),0) AS pnl FROM positions "
                "WHERE status='closed' AND closed_at>=?", (iso_ts,)
            ).fetchone()
        return r["pnl"]

    # ---------- file de synchronisation ----------
    def pending_sync(self, limit: int = 200) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM sync_queue ORDER BY id LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    def clear_sync(self, queue_ids: list[int]) -> None:
        if not queue_ids:
            return
        with self._lock:
            self._conn.executemany("DELETE FROM sync_queue WHERE id=?",
                                   [(i,) for i in queue_ids])
            self._conn.commit()

    def bump_sync_attempts(self, queue_ids: list[int]) -> None:
        with self._lock:
            self._conn.executemany(
                "UPDATE sync_queue SET attempts=attempts+1 WHERE id=?",
                [(i,) for i in queue_ids])
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()
