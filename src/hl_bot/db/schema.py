"""SQLite schema — ground truth for fills, equity, decisions, positions.

Design principles:
- Hyperliquid `userFills` is the source of truth for executed trades.
- We never compute PnL from our own internal records; we reconcile against the
  exchange. Internal records are for *attribution* (which agent decided what).
- Agents are logical: HL has one account per wallet. We attribute fills to
  agents via the `cloid` (client order id) they set when placing the order.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS fills (
    -- Mirror of Hyperliquid userFills. Primary key is (hash, tid) since a
    -- single tx hash can contain multiple trade ids.
    hash            TEXT NOT NULL,
    tid             INTEGER NOT NULL,
    time_ms         INTEGER NOT NULL,
    coin            TEXT NOT NULL,
    side            TEXT NOT NULL,            -- 'B' buy / 'A' sell
    px              REAL NOT NULL,
    sz              REAL NOT NULL,
    start_position  REAL,
    dir             TEXT,                     -- 'Open Long', 'Close Short', etc.
    closed_pnl      REAL NOT NULL DEFAULT 0,
    fee             REAL NOT NULL DEFAULT 0,
    fee_token       TEXT,
    builder_fee     REAL DEFAULT 0,
    cloid           TEXT,                     -- our client order id -> agent attribution
    agent           TEXT,                     -- resolved at ingest time from cloid prefix
    raw_json        TEXT NOT NULL,
    PRIMARY KEY (hash, tid)
);
CREATE INDEX IF NOT EXISTS idx_fills_time  ON fills(time_ms);
CREATE INDEX IF NOT EXISTS idx_fills_agent ON fills(agent, time_ms);
CREATE INDEX IF NOT EXISTS idx_fills_coin  ON fills(coin, time_ms);

CREATE TABLE IF NOT EXISTS equity_snapshots (
    -- Periodic snapshot of clearinghouseState -> account value, margin used,
    -- position summary. Used for equity curve, drawdown, Sharpe.
    ts_ms           INTEGER PRIMARY KEY,
    account_value   REAL NOT NULL,
    total_margin    REAL NOT NULL,
    total_ntl_pos   REAL NOT NULL,            -- absolute notional position
    total_raw_usd   REAL NOT NULL,
    withdrawable    REAL NOT NULL,
    cross_leverage  REAL,
    raw_json        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agent_decisions (
    -- Every action an agent considered, whether or not it placed an order.
    -- The market snapshot is captured at decision time so we can later replay
    -- and audit edge.
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_ms           INTEGER NOT NULL,
    agent           TEXT NOT NULL,
    action          TEXT NOT NULL,            -- 'place', 'cancel', 'hold', 'flatten'
    coin            TEXT,
    side            TEXT,
    sz              REAL,
    px              REAL,
    cloid           TEXT,                     -- ties to fills.cloid
    reasoning       TEXT,                     -- free-form / LLM trace
    market_snapshot TEXT,                     -- JSON: mid, funding, oi, book imbalance, ...
    is_paper        INTEGER NOT NULL DEFAULT 1,
    error           TEXT
);
CREATE INDEX IF NOT EXISTS idx_decisions_agent ON agent_decisions(agent, ts_ms);
CREATE INDEX IF NOT EXISTS idx_decisions_cloid ON agent_decisions(cloid);

CREATE TABLE IF NOT EXISTS positions (
    -- Logical per-agent position attribution. Updated from fills on ingest.
    -- The exchange truth is in clearinghouseState; this is *who owns what*
    -- among our agents.
    agent           TEXT NOT NULL,
    coin            TEXT NOT NULL,
    net_sz          REAL NOT NULL DEFAULT 0,  -- + long, - short
    avg_entry_px    REAL NOT NULL DEFAULT 0,
    realized_pnl    REAL NOT NULL DEFAULT 0,
    fees_paid       REAL NOT NULL DEFAULT 0,
    last_update_ms  INTEGER NOT NULL,
    PRIMARY KEY (agent, coin)
);

CREATE TABLE IF NOT EXISTS funding_payments (
    -- userFunding payments. Crucial for perp PnL accounting & funding-arb.
    time_ms         INTEGER NOT NULL,
    coin            TEXT NOT NULL,
    usdc            REAL NOT NULL,            -- + received, - paid
    szi             REAL,                     -- position when funding was paid
    funding_rate    REAL,
    raw_json        TEXT NOT NULL,
    PRIMARY KEY (time_ms, coin)
);

CREATE TABLE IF NOT EXISTS agent_state (
    -- Persistent per-agent state: enabled, mode (paper/live), goals breached, etc.
    agent           TEXT PRIMARY KEY,
    mode            TEXT NOT NULL DEFAULT 'paper',   -- paper / live_small / live
    enabled         INTEGER NOT NULL DEFAULT 1,
    paused_reason   TEXT,
    paused_at_ms    INTEGER,
    last_promoted_ms INTEGER,
    notes           TEXT
);

CREATE TABLE IF NOT EXISTS goal_evaluations (
    -- Audit trail of every supervisor goal evaluation.
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_ms           INTEGER NOT NULL,
    agent           TEXT NOT NULL,
    goal_name       TEXT NOT NULL,
    metric_value    REAL,
    threshold       REAL,
    status          TEXT NOT NULL,            -- pass / fail / na
    action_taken    TEXT,                     -- promote / demote / pause / none
    detail          TEXT
);
CREATE INDEX IF NOT EXISTS idx_goal_eval_agent ON goal_evaluations(agent, ts_ms);
"""

# Versioned migrations, applied in order under PRAGMA user_version. Append-only:
# never edit or reorder an entry that has shipped — existing DBs track how many
# they have applied by index. Each entry must be idempotent-safe on a fresh DB
# (fresh DBs run the base SCHEMA and then every migration).
MIGRATIONS: list[str] = []


def _apply_migrations(conn: sqlite3.Connection) -> None:
    current = int(conn.execute("PRAGMA user_version").fetchone()[0])
    for version, script in enumerate(MIGRATIONS[current:], start=current + 1):
        conn.executescript(script)
        conn.execute(f"PRAGMA user_version = {version}")


def connect(db_path: str | Path) -> sqlite3.Connection:
    """Open a SQLite connection with sane defaults."""
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), isolation_level=None)  # autocommit
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    return conn


def init_db(db_path: str | Path) -> sqlite3.Connection:
    """Initialize the schema and apply any pending migrations. Idempotent."""
    conn = connect(db_path)
    conn.executescript(SCHEMA)
    _apply_migrations(conn)
    return conn
