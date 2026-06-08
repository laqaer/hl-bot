"""Pull fills, funding, and equity from Hyperliquid into the local DB.

Hyperliquid info endpoint reference (no auth required for public reads):
  POST https://api.hyperliquid.xyz/info
  {"type": "userFills",            "user": "0x..."}
  {"type": "userFunding",          "user": "0x...", "startTime": ..., "endTime": ...}
  {"type": "clearinghouseState",   "user": "0x..."}

We attribute fills to agents via the cloid prefix convention:
  cloid = "0x" + <16-hex-bytes>; we encode `agent_id` in the first 4 bytes
  of the cloid (see agents/cloid.py). Fills with unknown cloid prefix are
  attributed to "manual".
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from typing import Any

import httpx

from ..agents.cloid import agent_from_cloid
from ..risk.scaling import unified_portfolio_value

log = logging.getLogger(__name__)
INFO_PATH = "/info"
KNOWN_AGENTS = [
    "femr_v1",
    "funding_arb_v1",
    "twap_mr_v1",
    "liq_cascade_v1",
    "basis_v1",
    "veto_v1",
]



def _post(client: httpx.Client, base_url: str, payload: dict[str, Any]) -> Any:
    r = client.post(base_url + INFO_PATH, json=payload, timeout=15)
    r.raise_for_status()
    return r.json()


def ingest_fills(conn: sqlite3.Connection, address: str, base_url: str) -> int:
    """Pull recent userFills and upsert. Returns rows inserted."""
    with httpx.Client() as client:
        fills = _post(client, base_url, {"type": "userFills", "user": address}) or []
    n = 0
    cur = conn.cursor()
    for f in fills:
        cloid = f.get("cloid")
        agent = agent_from_cloid(cloid, known_agents=KNOWN_AGENTS) if cloid else "manual"
        try:
            cur.execute(
                """
                INSERT OR IGNORE INTO fills(
                    hash, tid, time_ms, coin, side, px, sz,
                    start_position, dir, closed_pnl, fee, fee_token,
                    builder_fee, cloid, agent, raw_json
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    f["hash"],
                    int(f["tid"]),
                    int(f["time"]),
                    f["coin"],
                    f["side"],
                    float(f["px"]),
                    float(f["sz"]),
                    float(f.get("startPosition", 0) or 0),
                    f.get("dir"),
                    float(f.get("closedPnl", 0) or 0),
                    float(f.get("fee", 0) or 0),
                    f.get("feeToken"),
                    float(f.get("builderFee", 0) or 0),
                    cloid,
                    agent,
                    json.dumps(f, separators=(",", ":")),
                ),
            )
            n += cur.rowcount
        except sqlite3.IntegrityError as e:
            log.warning("fill insert failed hash=%s tid=%s: %s", f.get("hash"), f.get("tid"), e)
    log.info("ingested %d new fills (of %d returned)", n, len(fills))
    return n


def snapshot_equity(conn: sqlite3.Connection, address: str, base_url: str) -> None:
    """Take one unified portfolio-value snapshot.

    Hyperliquid currently exposes usable collateral across perp account value
    plus spot USDC. Store that unified value in account_value so future trailing
    averages track the portfolio sizing rule instead of stale perp-only equity.
    """
    with httpx.Client() as client:
        st = _post(client, base_url, {"type": "clearinghouseState", "user": address})
        try:
            spot_st = _post(client, base_url, {"type": "spotClearinghouseState", "user": address})
        except (httpx.HTTPError, ValueError, TypeError) as e:
            log.warning("spotClearinghouseState snapshot failed; using perp-only value: %s", e)
            spot_st = {}
    if not st:
        log.warning("empty clearinghouseState")
        return
    margin = st.get("marginSummary", {}) or {}
    portfolio_value = unified_portfolio_value(st, spot_st)
    cross_lev = None
    try:
        ntl = float(margin.get("totalNtlPos", 0) or 0)
        cross_lev = ntl / portfolio_value if portfolio_value else None
    except (TypeError, ValueError):
        pass
    raw = {"clearinghouseState": st, "spotClearinghouseState": spot_st, "portfolioValue": portfolio_value}
    conn.execute(
        """
        INSERT OR REPLACE INTO equity_snapshots(
            ts_ms, account_value, total_margin, total_ntl_pos,
            total_raw_usd, withdrawable, cross_leverage, raw_json
        ) VALUES(?,?,?,?,?,?,?,?)
        """,
        (
            int(time.time() * 1000),
            portfolio_value,
            float(margin.get("totalMarginUsed", 0) or 0),
            float(margin.get("totalNtlPos", 0) or 0),
            float(margin.get("totalRawUsd", 0) or 0),
            float(st.get("withdrawable", 0) or 0),
            cross_lev,
            json.dumps(raw, separators=(",", ":")),
        ),
    )


def ingest_funding(
    conn: sqlite3.Connection,
    address: str,
    base_url: str,
    lookback_days: int = 7,
) -> int:
    """Pull funding payments. Returns rows inserted."""
    end = int(time.time() * 1000)
    start = end - lookback_days * 86_400_000
    with httpx.Client() as client:
        rows = _post(
            client,
            base_url,
            {"type": "userFunding", "user": address, "startTime": start, "endTime": end},
        ) or []
    n = 0
    cur = conn.cursor()
    for r in rows:
        delta = r.get("delta", {}) or {}
        try:
            cur.execute(
                """
                INSERT OR IGNORE INTO funding_payments(
                    time_ms, coin, usdc, szi, funding_rate, raw_json
                ) VALUES(?,?,?,?,?,?)
                """,
                (
                    int(r["time"]),
                    delta.get("coin", ""),
                    float(delta.get("usdc", 0) or 0),
                    float(delta.get("szi", 0) or 0) if delta.get("szi") is not None else None,
                    float(delta.get("fundingRate", 0) or 0) if delta.get("fundingRate") is not None else None,
                    json.dumps(r, separators=(",", ":")),
                ),
            )
            n += cur.rowcount
        except sqlite3.IntegrityError:
            pass
    log.info("ingested %d new funding rows", n)
    return n
