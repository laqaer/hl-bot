"""Runtime harness: pull a MarketView, ask each enabled agent to decide,
route decisions (paper logging only by default; live placement requires
HL_SECRET_KEY and agent mode != 'paper').

Live order placement is intentionally NOT wired yet — the harness logs all
decisions and a separate `place_order` adapter would be plugged in. This keeps
the default behavior risk-free.
"""

from __future__ import annotations

import contextlib
import logging
import sqlite3
import time

import httpx

from ..agents.base import Agent, MarketView
from ..agents.decisions import Decision, log_decision

log = logging.getLogger(__name__)


def fetch_market_view(base_url: str, coins: list[str]) -> MarketView:
    """Fetch mids + 1h funding + 24h volume for all coins via /info.

    NOTE: returns ALL coins from the universe, not just the requested ones,
    because FEMR needs to scan the whole universe for funding extremes.
    The `coins` parameter is kept for backward compatibility but ignored.
    """
    with httpx.Client(timeout=15) as client:
        mids_raw = client.post(base_url + "/info", json={"type": "allMids"}).json() or {}
        meta_ctx = client.post(base_url + "/info", json={"type": "metaAndAssetCtxs"}).json()
    mids: dict[str, float] = {}
    for k, v in mids_raw.items():
        with contextlib.suppress(TypeError, ValueError):
            mids[k] = float(v)
    funding: dict[str, float] = {}
    open_interest: dict[str, float] = {}
    day_ntl_vlm: dict[str, float] = {}
    if isinstance(meta_ctx, list) and len(meta_ctx) == 2:
        universe = meta_ctx[0].get("universe", [])
        ctxs = meta_ctx[1]
        for u, c in zip(universe, ctxs, strict=False):
            name = u.get("name")
            if not name:
                continue
            try:
                funding[name] = float(c.get("funding", 0))
                open_interest[name] = float(c.get("openInterest", 0))
                day_ntl_vlm[name] = float(c.get("dayNtlVlm", 0))
            except (TypeError, ValueError):
                pass
    return MarketView(
        ts_ms=int(time.time() * 1000),
        mids=mids,
        funding=funding,
        open_interest=open_interest,
        extra={"day_ntl_vlm": day_ntl_vlm},
    )


def _agent_mode(conn: sqlite3.Connection, agent: str) -> tuple[str, bool]:
    row = conn.execute(
        "SELECT mode, enabled FROM agent_state WHERE agent=?", (agent,)
    ).fetchone()
    if not row:
        return "paper", True
    return row["mode"], bool(row["enabled"])


def run_tick(
    conn: sqlite3.Connection,
    agents: list[Agent],
    base_url: str,
    coins: list[str],
    *,
    force_paper: bool = True,
) -> list[Decision]:
    """One scheduling tick: fetch view, ask each agent, log decisions.

    If force_paper is True (default), every decision is recorded with
    is_paper=True regardless of agent mode. Flip to False only when you've
    wired and reviewed the live order adapter.
    """
    view = fetch_market_view(base_url, coins)
    all_decisions: list[Decision] = []
    for agent in agents:
        mode, enabled = _agent_mode(conn, agent.name)
        if not enabled:
            log.info("agent %s disabled, skipping", agent.name)
            continue
        try:
            decisions = agent.decide(view)
        except Exception as e:  # noqa: BLE001
            log_decision(conn, Decision(
                agent=agent.name, action="error",
                reasoning="decide() raised", error=str(e),
                is_paper=True,
            ))
            log.exception("agent %s decide() failed", agent.name)
            continue
        for d in decisions:
            d.is_paper = True if force_paper else (mode == "paper")
            log_decision(conn, d)
            all_decisions.append(d)
    return all_decisions
