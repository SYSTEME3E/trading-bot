"""Synchronisation SQLite -> Supabase (à sens unique, facultative, non bloquante).

flush() ne lève JAMAIS d'exception : en cas d'échec, les lignes restent dans
la file et seront renvoyées au prochain passage.
"""
from __future__ import annotations

import logging

from database.database import Database

log = logging.getLogger(__name__)


def make_client(url: str, service_key: str):
    from supabase import create_client  # import tardif : paquet facultatif
    return create_client(url, service_key)


class SupabaseSync:
    def __init__(self, db: Database, client=None, batch_size: int = 200):
        self.db = db
        self.client = client
        self.batch_size = batch_size

    def flush(self) -> int:
        """Envoie la file vers Supabase. Retourne le nombre de lignes synchronisées."""
        try:
            pending = self.db.pending_sync(self.batch_size)
            if not pending:
                return 0
            # On garde la dernière version de chaque ligne (upsert par id).
            latest: dict[tuple[str, str], dict] = {}
            ids_by_tbl: dict[str, list[int]] = {}
            import json
            for q in pending:
                latest[(q["tbl"], q["row_id"])] = json.loads(q["payload"])
                ids_by_tbl.setdefault(q["tbl"], []).append(q["id"])

            done = 0
            for tbl, qids in ids_by_tbl.items():
                rows = [p for (t, _), p in latest.items() if t == tbl]
                try:
                    self.client.table(tbl).upsert(rows, on_conflict="id").execute()
                except Exception as e:  # réseau, quota, schéma...
                    log.warning("Sync Supabase échouée (%s) : %s", tbl, type(e).__name__)
                    self.db.bump_sync_attempts(qids)
                    continue
                self.db.clear_sync(qids)
                done += len(rows)
            return done
        except Exception as e:
            log.warning("Sync Supabase ignorée : %s", type(e).__name__)
            return 0
