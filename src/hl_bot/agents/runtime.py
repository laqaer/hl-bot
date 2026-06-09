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
from dataclasses import dataclass

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


def positions_from_clearinghouse(st: dict) -> list[dict]:
    """Normalize HL ``clearinghouseState`` into the bot's position-dict shape.

    Pure parse of ``st["assetPositions"][].position`` into the list of dicts the
    rest of the live path consumes (reconcile, allocator, view enrichment).
    Previously inlined and untested in ``femr_tick``; extracted as the first pure
    slice of the shared live/paper tick harness (REVIEW M3 / B12). Malformed
    entries are skipped rather than aborting the tick.
    """
    out: list[dict] = []
    for ap in st.get("assetPositions", []) or []:
        pos = (ap.get("position") or {}) if isinstance(ap, dict) else {}
        with contextlib.suppress(TypeError, ValueError):
            out.append({
                "coin": pos.get("coin"),
                "szi": float(pos.get("szi", 0) or 0),
                "entry_px": float(pos.get("entryPx", 0) or 0),
                "position_value": float(pos.get("positionValue", 0) or 0),
                "unrealized_pnl": float(pos.get("unrealizedPnl", 0) or 0),
                "liquidation_px": float(pos.get("liquidationPx", 0) or 0),
                "leverage": (pos.get("leverage") or {}).get("value"),
                "margin_used": float(pos.get("marginUsed", 0) or 0),
            })
    return out


def reconcile_agents(
    conn: sqlite3.Connection,
    all_positions: list[dict],
    agent_names: list[str],
) -> dict[str, list[str]]:
    """Clear stale DB ownership for each agent independently against HL truth.

    Runs ``reconcile_positions`` per agent (each agent owns coins by name match,
    so reconciling them together would cross-contaminate) and returns only the
    agents that had something reconciled. Extracted from the ``femr_tick``
    preamble as part of the shared tick harness (REVIEW M3 / B12).
    """
    from ..exec.orders import reconcile_positions

    reconciled: dict[str, list[str]] = {}
    for name in agent_names:
        r = reconcile_positions(conn, all_positions, agent=name)
        if r:
            reconciled[name] = r
    return reconciled


def _agent_mode(conn: sqlite3.Connection, agent: str) -> tuple[str, bool]:
    row = conn.execute(
        "SELECT mode, enabled FROM agent_state WHERE agent=?", (agent,)
    ).fetchone()
    if not row:
        return "paper", True
    return row["mode"], bool(row["enabled"])


def gather_decisions(
    conn: sqlite3.Connection,
    agents: list[Agent],
    view: MarketView,
    *,
    is_paper: bool,
    defer_exec_logging: bool = False,
    log_holds: bool = True,
    honor_enabled: bool = True,
) -> list[Decision]:
    """Ask each agent to ``decide()``, isolating failures, and log per policy.

    The single decision-gathering path shared by the paper ``tick`` command
    (:func:`run_tick`) and the live ``femr_tick`` loop, so one tested function
    owns what gets logged and when (REVIEW M3 — the two paths had diverged and
    only the paper one isolated agent crashes).

    Every returned decision has ``is_paper`` set to ``is_paper``. A ``decide()``
    that raises is caught, recorded as an ``error`` row, and skipped — one broken
    agent can no longer abort the whole tick, so risk-reducing flattens from
    healthy agents still run on the live path (this isolation was previously
    missing from ``femr_tick``).

    Logging policy:
    - ``honor_enabled``: skip agents marked ``enabled=0`` in ``agent_state``.
    - ``log_holds``: when False, ``hold`` rows are returned but not logged (noise).
    - ``defer_exec_logging``: when True (the live path), ``place``/``flatten`` are
      returned but NOT logged here — they're logged only after the exchange
      confirms, with the real fill px/sz (see :func:`execute_decisions`), so the
      cooldown check never sees our own intent rows.
    """
    out: list[Decision] = []
    for agent in agents:
        if honor_enabled and not _agent_mode(conn, agent.name)[1]:
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
            d.is_paper = is_paper
            defer = d.action == "hold" and not log_holds
            defer = defer or (defer_exec_logging and d.action in ("place", "flatten"))
            if not defer:
                log_decision(conn, d)
            out.append(d)
    return out


def run_tick(
    conn: sqlite3.Connection,
    agents: list[Agent],
    base_url: str,
    coins: list[str],
    *,
    force_paper: bool = True,
) -> list[Decision]:
    """One scheduling tick: fetch view, ask each agent, log decisions.

    Places no orders — this is the paper ``tick`` path. Every decision is recorded
    with ``is_paper=force_paper`` (default True), so the logged book stays paper.
    """
    view = fetch_market_view(base_url, coins)
    return gather_decisions(conn, agents, view, is_paper=force_paper)


@dataclass
class ExecEvent:
    """One outcome of routing a decision to the exchange (place/flatten).

    ``kind`` is the machine-readable outcome (asserted in tests); ``message`` is
    the human/console string (kept here so the CLI stays a thin printer and the
    live execution path is unit-testable with a fake exchange).
    """

    kind: str  # skip|resting|filled_maker|filled|reject|closed|close_failed
    agent: str
    coin: str
    message: str


def execute_decisions(
    conn: sqlite3.Connection,
    exchange,
    view: MarketView,
    decisions: list[Decision],
    *,
    agent_names: set[str],
    guardrails_ok: bool,
    execution: str = "taker",
) -> list[ExecEvent]:
    """Route place/flatten decisions to the exchange. Pure of presentation.

    This is the single live order-placement loop (previously inlined in
    ``cli.femr_tick``). Behavior is preserved exactly:

    - ``place`` is blocked when guardrails fail or the coin is in cooldown.
    - In ``maker`` mode, a coin with a resting quote is left alone; otherwise a
      post-only limit is placed at the near touch (book-aware, never crossing).
    - In ``taker`` mode, a market order is placed.
    - ``place``/``flatten`` are logged ONLY after exchange acceptance, with the
      REAL fill px/sz, so cooldown checks don't see our own intent rows and
      downstream stops/TPs key off truth.

    Returns an ordered list of :class:`ExecEvent` for the caller to display.
    """
    from ..exec.orders import (
        close_position,
        coin_in_cooldown,
        maker_limit_price,
        place_limit_order,
        place_market_order,
    )

    maker = execution == "maker"
    if maker:
        from ..exec.maker import log_rest, working_orders

    events: list[ExecEvent] = []
    for d in decisions:
        if d.agent not in agent_names or d.coin is None:
            continue

        if d.action == "place" and d.sz and d.side:
            if not guardrails_ok:
                events.append(ExecEvent(
                    "skip", d.agent, d.coin,
                    f"[dim]SKIP {d.agent} {d.coin}: guardrail blocks new entries[/dim]"))
                continue
            if coin_in_cooldown(conn, d.coin, agent=d.agent):
                events.append(ExecEvent(
                    "skip", d.agent, d.coin,
                    f"[dim]SKIP {d.agent} {d.coin}: in cooldown[/dim]"))
                continue
            is_buy = (d.side == "B")
            if maker:
                # Already have a working quote on this coin? leave it.
                if d.coin in working_orders(conn, d.agent):
                    events.append(ExecEvent(
                        "skip", d.agent, d.coin,
                        f"[dim]SKIP {d.agent} {d.coin}: maker quote already resting[/dim]"))
                    continue
                bt = (view.book_top or {}).get(d.coin)
                # Passive fallback when no fresh L2 book: step ~5bps inside from the
                # (possibly stale) mid so a post-only order rests instead of crossing.
                passive = (d.px or 0.0) * (0.9995 if is_buy else 1.0005)
                limit_px = maker_limit_price(
                    bt[0] if bt else None, bt[1] if bt else None, is_buy, passive)
                res = place_limit_order(exchange, d.coin, is_buy, d.sz, limit_px,
                                        post_only=True, cloid=d.cloid)
                if res.status == "resting":
                    events.append(ExecEvent(
                        "resting", d.agent, d.coin,
                        f"[cyan]RESTING[/cyan] {d.coin} {'BUY' if is_buy else 'SELL'} "
                        f"{d.sz} @ ${limit_px} oid={res.oid}"))
                    log_rest(conn, d.agent, d.coin, d.side, d.sz, limit_px, d.cloid, res.oid)
                elif res.ok:  # filled immediately (rare for post-only)
                    if res.avg_px:
                        d.px = res.avg_px
                    log_decision(conn, d)
                    events.append(ExecEvent(
                        "filled_maker", d.agent, d.coin,
                        f"[bold green]FILLED(maker)[/bold green] {d.coin} @ ${res.avg_px}"))
                else:
                    events.append(ExecEvent(
                        "reject", d.agent, d.coin,
                        f"[red]MAKER REJECT[/red] {d.coin}: {res.status} — {res.error}"))
                conn.commit()
                continue
            res = place_market_order(exchange, d.coin, is_buy, d.sz,
                                     slippage_pct=0.01, cloid=d.cloid)
            if res.ok:
                # Log place ONLY after fill confirmed, with the REAL fill px/sz
                # (not the pre-trade mid) so downstream stops/TPs key off truth.
                if res.avg_px:
                    d.px = res.avg_px
                if res.filled_sz:
                    d.sz = res.filled_sz
                log_decision(conn, d)
                events.append(ExecEvent(
                    "filled", d.agent, d.coin,
                    f"[bold green]FILLED[/bold green] {d.coin} {'BUY' if is_buy else 'SELL'} "
                    f"{res.filled_sz} @ ${res.avg_px}"))
            else:
                log_decision(conn, Decision(
                    agent=d.agent, action="rejected", coin=d.coin,
                    reasoning=f"HL rejected: {res.error}", is_paper=False,
                ))
                conn.commit()
                events.append(ExecEvent(
                    "reject", d.agent, d.coin,
                    f"[red]REJECT[/red] {d.coin}: {res.status} — {res.error}"))

        elif d.action == "flatten":
            res = close_position(exchange, d.coin, cloid=d.cloid)
            if res.ok:
                # Log the flatten immediately so ownership clears this tick rather
                # than waiting for next-tick reconciliation. Record the real exit px.
                if res.avg_px:
                    d.px = res.avg_px
                log_decision(conn, d)
                conn.commit()
                events.append(ExecEvent(
                    "closed", d.agent, d.coin,
                    f"[bold]CLOSED[/bold] {d.coin} @ ${res.avg_px}"))
            else:
                events.append(ExecEvent(
                    "close_failed", d.agent, d.coin,
                    f"[red]CLOSE FAILED[/red] {d.coin}: {res.error}"))

    return events
