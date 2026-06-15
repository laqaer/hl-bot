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
MIGRATIONS: list[str] = [
    # 1: per-agent funding attribution (B6). Each exchange funding payment is
    # prorated across the agents holding that coin at payment time; any
    # unattributable remainder lands on the '_account' residual row so the
    # per-agent totals always reconcile to the exchange.
    """
    CREATE TABLE IF NOT EXISTS funding_attribution (
        time_ms         INTEGER NOT NULL,
        coin            TEXT NOT NULL,
        agent           TEXT NOT NULL,
        usdc            REAL NOT NULL,
        PRIMARY KEY (time_ms, coin, agent)
    );
    CREATE INDEX IF NOT EXISTS idx_funding_attr_agent
        ON funding_attribution(agent, time_ms);
    """,
    # 2: simulated paper trading (promotion gates need scoreable paper
    # performance — real `fills` only exist for live orders).
    """
    CREATE TABLE IF NOT EXISTS paper_fills (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        time_ms         INTEGER NOT NULL,
        agent           TEXT NOT NULL,
        coin            TEXT NOT NULL,
        side            TEXT NOT NULL,            -- 'B' buy / 'A' sell
        px              REAL NOT NULL,
        sz              REAL NOT NULL,
        closed_pnl      REAL NOT NULL DEFAULT 0,
        fee             REAL NOT NULL DEFAULT 0,
        cloid           TEXT,
        reasoning       TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_paper_fills_agent ON paper_fills(agent, time_ms);

    CREATE TABLE IF NOT EXISTS paper_orders (
        -- Resting simulated maker orders, filled only when price CROSSES the
        -- limit (conservative: touch is not enough).
        cloid           TEXT PRIMARY KEY,
        agent           TEXT NOT NULL,
        coin            TEXT NOT NULL,
        side            TEXT NOT NULL,
        sz              REAL NOT NULL,
        limit_px        REAL NOT NULL,
        created_ms      INTEGER NOT NULL,
        reasoning       TEXT
    );

    CREATE TABLE IF NOT EXISTS paper_funding (
        -- Simulated hourly funding accrual on open paper positions; the whole
        -- edge of the carry strategies, so paper scorecards must include it.
        time_ms         INTEGER NOT NULL,
        agent           TEXT NOT NULL,
        coin            TEXT NOT NULL,
        usdc            REAL NOT NULL,
        PRIMARY KEY (time_ms, agent, coin)
    );
    """,
    # 3: maker order lifecycle state machine (replaces replaying the whole
    # decision audit log to find working orders).
    """
    CREATE TABLE IF NOT EXISTS maker_orders (
        cloid           TEXT PRIMARY KEY,
        agent           TEXT NOT NULL,
        coin            TEXT NOT NULL,
        side            TEXT NOT NULL,            -- 'B' / 'A'
        sz              REAL NOT NULL,
        filled_sz       REAL NOT NULL DEFAULT 0,
        limit_px        REAL NOT NULL,
        oid             INTEGER,
        state           TEXT NOT NULL,            -- quoted/partial/filled/cancelled/expired/taker_fallback
        urgency         TEXT NOT NULL DEFAULT 'normal',  -- normal/exit/stop
        reduce_only     INTEGER NOT NULL DEFAULT 0,
        created_ms      INTEGER NOT NULL,
        updated_ms      INTEGER NOT NULL,
        reprice_count   INTEGER NOT NULL DEFAULT 0,
        parent_cloid    TEXT                      -- set when this quote replaced another
    );
    CREATE INDEX IF NOT EXISTS idx_maker_orders_agent ON maker_orders(agent, state);
    """,
    # 4: G0 confirmation stamps — `hlbot confirm --record` writes one row per
    # run; promotion stages with require_g0 demand a fresh confirmed=1 row.
    """
    CREATE TABLE IF NOT EXISTS confirmations (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        agent           TEXT NOT NULL,
        ts_ms           INTEGER NOT NULL,
        dataset         TEXT,                     -- coins/interval/days fingerprint
        prefer          TEXT,                     -- taker / maker
        confirmed       INTEGER NOT NULL,
        oos_edge_bps    REAL,
        summary         TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_confirmations_agent ON confirmations(agent, ts_ms);
    """,
    # 5: params provenance (V3). Stamp the fingerprint of the EFFECTIVE config a
    # confirmation validated, so require_g0 can refuse a G0 stamp earned for a
    # different param set. Nullable: legacy rows (pre-provenance) stay NULL and
    # simply won't match a real hash — they predate the check. (Shipped on main;
    # kept ALTER-only and verbatim so a DB already at user_version=5 stays valid.)
    """
    ALTER TABLE confirmations ADD COLUMN params_hash TEXT;
    """,
    # 6: forward-evidence accrual (P1). Append-only signal tables for the
    # things HL candle history can NEVER give us — OI + top-of-book imbalance
    # (market_samples), cross-venue funding (xvenue_funding), and per-listing
    # first-seen + reference price (listing_log). Written every engine cycle
    # from the already-fetched MarketView/WS snapshot; idempotent on the PK, no
    # deletes. Also creates the params_hash lookup index HERE (not folded into
    # migration 5, so a DB already at user_version=5 from main still gets it).
    # This is the fuel for confirming the next edges FORWARD.
    """
    CREATE TABLE IF NOT EXISTS market_samples (
        ts_ms          INTEGER NOT NULL,
        coin           TEXT    NOT NULL,
        mid            REAL,
        funding        REAL,          -- HL 1h funding (signed, per-hour)
        open_interest  REAL,          -- metaAndAssetCtxs openInterest (S8 enabler)
        day_ntl_vlm    REAL,
        book_imb       REAL,          -- (bidSz-askSz)/(bidSz+askSz) top-of-book, WS
        PRIMARY KEY (ts_ms, coin)
    );
    CREATE INDEX IF NOT EXISTS idx_market_samples_coin ON market_samples(coin, ts_ms);

    CREATE TABLE IF NOT EXISTS xvenue_funding (
        ts_ms        INTEGER NOT NULL,
        coin         TEXT    NOT NULL,
        venue        TEXT    NOT NULL,   -- 'binance' / 'bybit' / 'hl'
        funding_apr  REAL,              -- annualized %, for cross-venue compare
        PRIMARY KEY (ts_ms, coin, venue)
    );
    CREATE INDEX IF NOT EXISTS idx_xvenue_funding_coin ON xvenue_funding(coin, ts_ms);

    CREATE TABLE IF NOT EXISTS listing_log (
        coin           TEXT PRIMARY KEY,
        first_seen_ms  INTEGER NOT NULL,
        listing_px     REAL,
        source         TEXT            -- 'backfill' (pre-existing) / 'live' (new)
    );

    CREATE INDEX IF NOT EXISTS idx_confirmations_params
        ON confirmations(agent, params_hash, ts_ms);
    """,
    # 7: forward per-bar frame store (P1 linchpin). HL serves only ~5000
    # candles/interval, so a 5m agent's confirm window is retention-capped at
    # ~17.5d no matter how long it soaks. This stores the per-bar signal the
    # engine already computes each cycle (vwap/sigma/mid/funding/vol), floored to
    # the bar boundary, so `confirm` can rebuild frames from `accrued ∪
    # back-fetched` and the OOS window GROWS forward past HL retention. Append-
    # only, idempotent on the bar PK (first observation in a bar wins).
    """
    CREATE TABLE IF NOT EXISTS frame_samples (
        interval       TEXT    NOT NULL,   -- '5m' / '1h' (bar interval)
        coin           TEXT    NOT NULL,
        bar_ts_ms      INTEGER NOT NULL,   -- ts floored to the bar boundary
        mid            REAL,
        funding_hourly REAL,               -- unscaled 1h rate (signal)
        vwap           REAL,               -- rolling vwap (candles_<interval>.vwap)
        sigma          REAL,               -- rolling close-std (candles_<interval>.sigma)
        vol            REAL,               -- rolling 24h notional (day_ntl_vlm)
        PRIMARY KEY (interval, coin, bar_ts_ms)
    );
    CREATE INDEX IF NOT EXISTS idx_frame_samples_load
        ON frame_samples(interval, bar_ts_ms);
    """,
]


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
    conn.execute("PRAGMA busy_timeout=10000;")  # engine + report/sweep share the DB
    return conn


def init_db(db_path: str | Path) -> sqlite3.Connection:
    """Initialize the schema and apply any pending migrations. Idempotent."""
    conn = connect(db_path)
    conn.executescript(SCHEMA)
    _apply_migrations(conn)
    return conn
