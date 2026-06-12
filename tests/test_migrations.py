"""Versioned schema migrations under PRAGMA user_version.

Contract: the base SCHEMA is the v0 shape; every later structural change is an
appended entry in MIGRATIONS. A DB created at any historical version must reach
the current shape via init_db, and a fully-migrated DB must be a no-op.
"""

from __future__ import annotations

import sqlite3

from hl_bot.db import schema
from hl_bot.db.schema import MIGRATIONS, init_db


def _user_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def test_fresh_db_lands_at_latest_version(tmp_path):
    conn = init_db(tmp_path / "fresh.sqlite")
    assert _user_version(conn) == len(MIGRATIONS)


def test_init_is_idempotent(tmp_path):
    p = tmp_path / "again.sqlite"
    init_db(p).close()
    conn = init_db(p)  # second run must not fail or re-apply
    assert _user_version(conn) == len(MIGRATIONS)


def test_old_db_upgrades(tmp_path, monkeypatch):
    p = tmp_path / "old.sqlite"
    # Simulate a DB created before any migrations existed.
    monkeypatch.setattr(schema, "MIGRATIONS", [])
    init_db(p).close()
    monkeypatch.undo()

    test_migration = (
        "CREATE TABLE IF NOT EXISTS _migration_probe (id INTEGER PRIMARY KEY);"
    )
    monkeypatch.setattr(schema, "MIGRATIONS", [*MIGRATIONS, test_migration])
    conn = init_db(p)
    assert _user_version(conn) == len(MIGRATIONS) + 1
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert "_migration_probe" in tables


def test_partial_migration_resumes(tmp_path, monkeypatch):
    """A DB stamped at version N applies only migrations N+1..len."""
    p = tmp_path / "partial.sqlite"
    m1 = "CREATE TABLE IF NOT EXISTS _m1 (id INTEGER PRIMARY KEY);"
    m2 = "CREATE TABLE IF NOT EXISTS _m2 (id INTEGER PRIMARY KEY);"

    monkeypatch.setattr(schema, "MIGRATIONS", [*MIGRATIONS, m1])
    init_db(p).close()

    monkeypatch.setattr(schema, "MIGRATIONS", [*MIGRATIONS, m1, m2])
    conn = init_db(p)
    assert _user_version(conn) == len(MIGRATIONS) + 2
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert {"_m1", "_m2"} <= tables
