# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Guardianity — Aria (Track 2)
"""Aria's own durable memory — a tiny engram store so the guide remembers a guest
across visits. Standalone (no dependency on the researcher).

Two backends, one interface: a Postgres DSN (Aria's OWN Neon, never shared) via
``DATABASE_URL``/``ARIA_DB``, else a local SQLite file. Only what Aria needs:
persist and reload engrams by tenant.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from pathlib import Path
from typing import Iterator

from engine.engram import Engram

log = logging.getLogger("aria.persistence")


def _is_pg(dsn: str) -> bool:
    return dsn.startswith("postgres://") or dsn.startswith("postgresql://")


class EngramStore:
    """Durable engrams keyed by tenant. ``dsn`` is a Postgres URL or a SQLite path."""

    def __init__(self, dsn: str | Path) -> None:
        self.dsn = str(dsn)
        self.pg = _is_pg(self.dsn)
        self.ph = "%s" if self.pg else "?"
        self._lock = threading.Lock()
        blob = "BYTEA" if self.pg else "BLOB"
        ts = "DOUBLE PRECISION" if self.pg else "REAL"
        if self.pg:
            import psycopg2
            self._conn = psycopg2.connect(self.dsn); self._conn.autocommit = False
            self.backend = "postgres"
        else:
            if self.dsn != ":memory:":
                Path(self.dsn).parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(self.dsn, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self.backend = "sqlite"
        with self._lock:
            cur = self._cursor()
            cur.execute(
                f"""CREATE TABLE IF NOT EXISTS engrams (
                    tenant TEXT NOT NULL, engram_id TEXT NOT NULL, ts {ts}, data {blob} NOT NULL,
                    PRIMARY KEY (tenant, engram_id))"""
            )
            cur.execute("CREATE INDEX IF NOT EXISTS ix_aria_engrams_tenant ON engrams(tenant)")
            self._conn.commit(); cur.close()
        log.info("Aria engram store ready: %s", self.backend)

    def _ensure(self) -> None:
        if not self.pg:
            return
        import psycopg2
        try:
            self._conn.cursor().execute("SELECT 1")
        except (psycopg2.InterfaceError, psycopg2.OperationalError):
            self._conn = psycopg2.connect(self.dsn); self._conn.autocommit = False

    def _cursor(self):
        if self.pg:
            from psycopg2.extras import RealDictCursor
            self._ensure()
            return self._conn.cursor(cursor_factory=RealDictCursor)
        return self._conn.cursor()

    def _binary(self, data: bytes):
        if self.pg:
            import psycopg2
            return psycopg2.Binary(data)
        return data

    def save_engram(self, tenant: str, engram: Engram) -> None:
        sql = ("INSERT INTO engrams (tenant, engram_id, ts, data) VALUES (?,?,?,?) "
               "ON CONFLICT (tenant, engram_id) DO UPDATE SET ts=EXCLUDED.ts, data=EXCLUDED.data")
        if self.pg:
            sql = sql.replace("?", "%s")
        with self._lock:
            cur = self._cursor()
            try:
                cur.execute(sql, (tenant, engram.engram_id, engram.ts, self._binary(engram.to_bytes())))
                self._conn.commit()
            except Exception:
                self._conn.rollback(); raise
            finally:
                cur.close()

    def load_engrams(self, tenant: str) -> Iterator[Engram]:
        sql = "SELECT data FROM engrams WHERE tenant=? ORDER BY ts ASC"
        if self.pg:
            sql = sql.replace("?", "%s")
        with self._lock:
            cur = self._cursor()
            try:
                cur.execute(sql, (tenant,))
                rows = cur.fetchall()
            finally:
                cur.close()
        for r in rows:
            try:
                yield Engram.from_bytes(bytes(r["data"]))
            except Exception:  # noqa: BLE001 — one bad row must not block recall
                log.warning("skipping unreadable engram row for %s", tenant)

    def close(self) -> None:
        with self._lock:
            self._conn.close()
