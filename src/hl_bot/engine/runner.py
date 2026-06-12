"""One execution cycle for every agent, paper and live together (B12).

This replaces the split between ``runtime.run_tick`` (paper-only) and the
CLI's ``femr_tick`` (live loop) with a single, testable path:

    build_roster  — config-driven: every configs/*.yaml with roster != retired
    run_cycle     — reconcile → allocate caps → decide → paper-simulate the
                    paper-mode agents → execute the live-mode agents (maker
                    lifecycle by default, taker on demand)

Paper agents keep deciding and simulating during live operation, which is what
feeds the auto-promotion gates evidence. The kill switch is checked at cycle
start AND immediately before every order placement.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from ..agents.base import Agent, MarketView
from ..agents.basis import BasisAgent
from ..agents.decisions import Decision, log_decision
from ..agents.femr import FemrAgent
from ..agents.funding_arb import FundingArbAgent
from ..agents.funding_carry import FundingCarryAgent
from ..agents.liq_cascade import LiqCascadeAgent
from ..agents.meta_allocator import MetaAllocator, MetaAllocatorConfig
from ..agents.twap_mr import TwapMrAgent
from ..agents.twap_mr_regime import TwapMrRegimeAgent
from ..agents.xfund_carry import XFundCarryAgent
from ..config import Settings
from ..exec.lifecycle import (
    MakerConfig,
    apply_actions,
    fills_by_cloid,
    open_orders,
    plan_actions,
    submit_entry,
)
from ..exec.orders import (
    GuardrailConfig,
    check_guardrails,
    close_position,
    coin_in_cooldown,
    dynamic_daily_loss_limit,
    order_rate_ok,
    place_market_order,
    reconcile_positions,
)
from ..ops.kill import kill_active, trip_kill
from ..risk.allocation import apply_mode_sizing, resolve_agent_caps
from ..risk.scaling import compute_notional_cap, unified_portfolio_value
from ..sim.paper import PaperCycleResult, simulate_cycle
from ..supervisor.goals import AgentGoals, load_goals

log = logging.getLogger(__name__)

# Default per-agent configs live with the factory so the roster has one source
# of truth (the CLI's backtest/confirm factories should migrate here too).
AGENT_FACTORIES: dict[str, Callable[[sqlite3.Connection | None, dict], Agent]] = {
    "femr_v1": lambda conn, cfg: FemrAgent(config={
        "max_notional_per_trade": 20.0, "max_total_notional": 40.0,
        "funding_enter_per_hr": 0.00015, "funding_exit_per_hr": 0.00005,
        **cfg}, conn=conn),
    "twap_mr_v1": lambda conn, cfg: TwapMrAgent(config=cfg, conn=conn),
    "twap_mr_regime_v1": lambda conn, cfg: TwapMrRegimeAgent(config=cfg, conn=conn),
    "funding_carry_v1": lambda conn, cfg: FundingCarryAgent(config=cfg, conn=conn),
    "xfund_carry_v1": lambda conn, cfg: XFundCarryAgent(config=cfg, conn=conn),
    "liq_cascade_v1": lambda conn, cfg: LiqCascadeAgent(config=cfg, conn=conn),
    "basis_v1": lambda conn, cfg: BasisAgent(config=cfg, conn=conn),
    "funding_arb_v1": lambda conn, cfg: FundingArbAgent(config=cfg),
}


@dataclass
class RosterEntry:
    agent: Agent
    goals: AgentGoals
    mode: str = "paper"        # agent_state truth, set by split_roster


@dataclass
class CycleResult:
    decisions: list[Decision] = field(default_factory=list)
    events: list[str] = field(default_factory=list)
    paper: PaperCycleResult | None = None
    live_agents: list[str] = field(default_factory=list)
    paper_agents: list[str] = field(default_factory=list)
    halted: str | None = None          # guardrail/kill reason if entries blocked

    def summary(self) -> str:
        parts = [f"{len(self.decisions)} decisions",
                 f"live={','.join(self.live_agents) or '∅'}",
                 f"paper={','.join(self.paper_agents) or '∅'}"]
        if self.paper:
            parts.append(self.paper.summary())
        if self.halted:
            parts.append(f"HALTED: {self.halted}")
        return " · ".join(parts)


def load_agent_goals(configs_dir: str | Path) -> dict[str, AgentGoals]:
    out: dict[str, AgentGoals] = {}
    for p in sorted(Path(configs_dir).glob("*.yaml")):
        for g in load_goals(p):
            out[g.agent] = g
    return out


def build_roster(
    conn: sqlite3.Connection,
    configs_dir: str | Path,
    overrides: dict[str, dict] | None = None,
) -> list[RosterEntry]:
    """Instantiate every configured, non-retired agent. Config-driven: an agent
    trades only if it has a YAML contract; retired agents are gone entirely."""
    overrides = overrides or {}
    roster: list[RosterEntry] = []
    for name, g in load_agent_goals(configs_dir).items():
        if g.roster == "retired":
            continue
        factory = AGENT_FACTORIES.get(name)
        if factory is None:
            log.warning("config %s has no agent factory; skipping", name)
            continue
        roster.append(RosterEntry(agent=factory(conn, dict(overrides.get(name) or {})),
                                  goals=g))
    return roster


def split_roster(
    conn: sqlite3.Connection, roster: list[RosterEntry], *, live: bool
) -> tuple[list[RosterEntry], list[RosterEntry]]:
    """(live_entries, paper_entries) by agent_state mode/enabled + YAML roster.

    Paper/default state is safe: an agent must be explicitly enabled and in
    live_small/live mode AND yaml roster 'live' to place real orders. Paused
    (enabled=0) agents run nowhere.
    """
    rows = conn.execute("SELECT agent, mode, enabled FROM agent_state").fetchall()
    state = {r["agent"]: (r["mode"], int(r["enabled"])) for r in rows}
    live_entries: list[RosterEntry] = []
    paper_entries: list[RosterEntry] = []
    for e in roster:
        mode, enabled = state.get(e.agent.name, ("paper", 1))
        e.mode = mode
        if not enabled:
            continue
        if live and e.goals.roster == "live" and mode in ("live_small", "live"):
            live_entries.append(e)
        else:
            paper_entries.append(e)
    return live_entries, paper_entries


def run_cycle(
    conn: sqlite3.Connection,
    s: Settings,
    view: MarketView,
    *,
    live: bool = False,
    execution: str = "maker",
    roster: list[RosterEntry] | None = None,
    exchange: Any = None,
    info: Any = None,
    account_state: dict | None = None,
    spot_state: dict | None = None,
    configs_dir: str | Path | None = None,
    maker_cfg: MakerConfig | None = None,
    now_ms: int | None = None,
) -> CycleResult:
    """One full decision+execution cycle. Pure-ish: pass view/states/exchange
    explicitly and it touches no network besides order placement."""
    from ..config import CONFIG_DIR

    now_ms = now_ms or int(time.time() * 1000)
    configs_dir = configs_dir or CONFIG_DIR
    data_dir = s.db_path.parent
    maker_cfg = maker_cfg or MakerConfig()
    res = CycleResult()

    if roster is None:
        roster = build_roster(conn, configs_dir, _load_overrides(configs_dir))
    live_entries, paper_entries = split_roster(conn, roster, live=live)
    res.live_agents = [e.agent.name for e in live_entries]
    res.paper_agents = [e.agent.name for e in paper_entries]

    # --- account truth (live only) ---
    all_positions: list[dict] = []
    portfolio_value: float | None = None
    if live:
        if account_state is None:
            account_state, spot_state = _fetch_account_state(s)
        portfolio_value = unified_portfolio_value(account_state or {}, spot_state or {})
        all_positions = _positions_from_state(account_state or {})

    # --- risk caps + allocator (over live agents; paper agents keep config caps) ---
    if live_entries:
        risk_cap = compute_notional_cap(conn, live_portfolio_value=portfolio_value)
        allocator = MetaAllocator(
            [e.agent.name for e in live_entries],
            MetaAllocatorConfig(total_capital=risk_cap.max_total_notional,
                                max_alloc=risk_cap.max_per_position_notional),
        )
        allocs = allocator.allocate(conn)
        configured = {
            e.agent.name: {
                "max_total_notional": float(getattr(e.agent.cfg, "max_total_notional", float("inf"))),
                "max_notional_per_trade": float(getattr(e.agent.cfg, "max_notional_per_trade", float("inf"))),
            }
            for e in live_entries if hasattr(e.agent, "cfg")
        }
        resolved = resolve_agent_caps(allocs, risk_cap, configured)
        for e in live_entries:
            cap = resolved.get(e.agent.name)
            if cap is None or not hasattr(e.agent, "cfg"):
                continue
            # live_small runs deliberately tiny regardless of allocator grant.
            cap = apply_mode_sizing(cap, e.mode, e.goals.sizing)
            if hasattr(e.agent.cfg, "max_total_notional"):
                e.agent.cfg.max_total_notional = cap.max_total_notional
            if hasattr(e.agent.cfg, "max_notional_per_trade"):
                e.agent.cfg.max_notional_per_trade = cap.max_notional_per_trade

        # reconcile live ownership against exchange truth
        for e in live_entries:
            stale = reconcile_positions(conn, all_positions, agent=e.agent.name)
            if stale:
                res.events.append(f"RECONCILED {e.agent.name}: {stale}")

        from ..exec.orders import bot_owned_coins
        owned_femr = bot_owned_coins(conn, agent="femr_v1")
        view.extra["live_positions"] = [p for p in all_positions if p["coin"] in owned_femr]

    # --- decide ---
    live_names = {e.agent.name for e in live_entries}
    for e in [*live_entries, *paper_entries]:
        try:
            decisions = e.agent.decide(view)
        except Exception as exc:  # noqa: BLE001
            log_decision(conn, Decision(agent=e.agent.name, action="error",
                                        reasoning="decide() raised", error=str(exc)))
            log.exception("agent %s decide() failed", e.agent.name)
            continue
        for d in decisions:
            d.is_paper = d.agent not in live_names
            if d.action not in ("hold", "place", "flatten"):
                log_decision(conn, d)
            res.decisions.append(d)

    # --- paper simulation ---
    paper_names = {e.agent.name for e in paper_entries}
    res.paper = simulate_cycle(
        conn, view,
        [d for d in res.decisions if d.agent in paper_names and d.action in ("place", "flatten")],
        maker_entries=(execution == "maker"),
        now_ms=now_ms,
    )

    if not live or not live_entries:
        return res

    # --- live execution ---
    kill = kill_active(data_dir)
    if kill:
        res.halted = f"KILL: {kill}"

    ok = True
    if info is not None:
        ok, why = check_guardrails(
            conn, info,
            GuardrailConfig(
                min_bot_capital=40.0,
                max_daily_loss=dynamic_daily_loss_limit(portfolio_value),
                max_total_notional=compute_notional_cap(
                    conn, live_portfolio_value=portfolio_value).max_total_notional,
                max_concurrent_positions=4,
            ),
            agents=list(live_names),
        )
        if not ok:
            res.halted = res.halted or f"guardrail: {why}"
            res.events.append(f"HALT new entries: {why}")
            if why.startswith("DAILY_LOSS:") and not kill_active(data_dir):
                # Account-level daily loss is STICKY: a human must look before
                # any new entries — `hlbot resume` clears it.
                trip_kill(data_dir, why)
                res.events.append("KILL tripped (account daily loss)")

    cooldowns = {e.agent.name: e.goals.cooldown_s for e in live_entries}

    # Maker lifecycle first: detect fills, reprice, expire, escalate exits.
    if execution == "maker" and exchange is not None:
        orders = open_orders(conn)
        if orders:
            fills = fills_by_cloid(conn, [o["cloid"] for o in orders])
            actions = plan_actions(orders, fills, view, now_ms, maker_cfg)
            res.events.extend(apply_actions(conn, exchange, actions, now_ms=now_ms))

    open_by_agent_coin = {
        (o["agent"], o["coin"]) for o in open_orders(conn)
    }

    for d in res.decisions:
        if d.agent not in live_names or d.coin is None:
            continue
        if d.action == "place" and d.sz and d.side:
            # Kill is re-checked before EVERY placement, not just at cycle start.
            if kill_active(data_dir):
                res.events.append(f"SKIP {d.agent} {d.coin}: KILL active")
                continue
            if not ok:
                res.events.append(f"SKIP {d.agent} {d.coin}: guardrail")
                continue
            if coin_in_cooldown(conn, d.coin, agent=d.agent,
                                cooldown_s=cooldowns.get(d.agent, 3600)):
                res.events.append(f"SKIP {d.agent} {d.coin}: cooldown")
                continue
            rate_ok, rate_why = order_rate_ok(conn, d.agent, now_ms=now_ms)
            if not rate_ok:
                res.events.append(f"SKIP {d.agent} {d.coin}: {rate_why}")
                continue
            if exchange is None:
                res.events.append(f"SKIP {d.agent} {d.coin}: no exchange")
                continue
            if execution == "maker" and d.urgency == "normal":
                if (d.agent, d.coin) in open_by_agent_coin:
                    res.events.append(f"SKIP {d.agent} {d.coin}: quote already resting")
                    continue
                res.events.append(submit_entry(conn, exchange, view, d, maker_cfg,
                                               now_ms=now_ms))
            else:
                r = place_market_order(exchange, d.coin, d.side == "B", d.sz,
                                       slippage_pct=0.01, cloid=d.cloid)
                if r.ok:
                    if r.avg_px:
                        d.px = r.avg_px
                    if r.filled_sz:
                        d.sz = r.filled_sz
                    log_decision(conn, d)
                    res.events.append(f"FILLED {d.agent} {d.coin} @ {r.avg_px}")
                else:
                    log_decision(conn, Decision(
                        agent=d.agent, action="rejected", coin=d.coin,
                        reasoning=f"HL rejected: {r.error}", is_paper=False))
                    res.events.append(f"REJECT {d.agent} {d.coin}: {r.error}")
        elif d.action == "flatten":
            # Risk reduction is never blocked by kill/guardrails. Stops cross
            # immediately; normal exits could rest reduce-only, but full-close
            # certainty wins until exec-quality telemetry says otherwise.
            if exchange is None:
                res.events.append(f"SKIP flatten {d.agent} {d.coin}: no exchange")
                continue
            r = close_position(exchange, d.coin, cloid=d.cloid)
            if r.ok:
                if r.avg_px:
                    d.px = r.avg_px
                log_decision(conn, d)
                res.events.append(f"CLOSED {d.agent} {d.coin} @ {r.avg_px}")
            else:
                res.events.append(f"CLOSE FAILED {d.agent} {d.coin}: {r.error}")
    conn.commit()
    return res


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _load_overrides(configs_dir: str | Path) -> dict[str, dict]:
    import json
    p = Path(configs_dir) / "agent_overrides.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text()) or {}
    except (ValueError, OSError):
        return {}


def _fetch_account_state(s: Settings) -> tuple[dict, dict]:
    from ..exec.orders import HL_TRADER_ADDRESS
    with httpx.Client(timeout=10) as cli:
        st = cli.post(s.hl_api_url + "/info",
                      json={"type": "clearinghouseState", "user": HL_TRADER_ADDRESS}).json() or {}
        try:
            spot = cli.post(s.hl_api_url + "/info",
                            json={"type": "spotClearinghouseState", "user": HL_TRADER_ADDRESS}).json() or {}
        except httpx.HTTPError:
            spot = {}
    return st, spot


def _positions_from_state(st: dict) -> list[dict]:
    import contextlib
    out = []
    for ap in st.get("assetPositions", []) or []:
        pos = ap.get("position", {}) or {}
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
