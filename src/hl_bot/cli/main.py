"""CLI entrypoint: `hlbot ...`"""

from __future__ import annotations

import contextlib
import json
import logging
import time
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from ..agents.basis import BasisAgent
from ..agents.dislocation_reversion import DislocationReversionAgent
from ..agents.femr import FemrAgent
from ..agents.funding_arb import FundingArbAgent
from ..agents.funding_carry import FundingCarryAgent
from ..agents.funding_crowding_fade import FundingCrowdingFadeAgent
from ..agents.liq_cascade import LiqCascadeAgent
from ..agents.meta_allocator import MetaAllocator, MetaAllocatorConfig
from ..agents.new_listing_reversion import NewListingReversionAgent
from ..agents.runtime import run_tick
from ..agents.spot_perp_carry import SpotPerpCarryAgent
from ..agents.twap_mr import TwapMrAgent
from ..agents.twap_mr_regime import TwapMrRegimeAgent
from ..agents.veto import VetoAgent
from ..agents.xfund_carry import XFundCarryAgent
from ..config import CONFIG_DIR, Settings
from ..db.schema import init_db
from ..engine.views import enrich_view as _enrich_view
from ..ingest.accrual import accrue_xvenue_funding
from ..ingest.hyperliquid import ingest_fills, ingest_funding, ingest_transfers, snapshot_equity
from ..reports.daily import build as build_report
from ..reports.daily import send_telegram
from ..research.funding_xvenue import fetch_xvenue_funding
from ..research.strategy_health import (
    agent_health,
    build_proposal_document,
    propose_overrides,
)
from ..risk.allocation import resolve_agent_caps
from ..risk.scaling import compute_notional_cap, spot_usdc_from_state, unified_portfolio_value
from ..scoring.metrics import score_all
from ..supervisor.loop import supervise

app = typer.Typer(no_args_is_help=True, add_completion=False)
console = Console()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def _conn():
    s = Settings.from_env()
    return init_db(s.db_path), s


def _filter_live_agents_by_state(conn, agents):
    """Return agents allowed to place live orders plus skipped reasons.

    Paper/default state is safe: an agent must be explicitly enabled and in
    live_small/live mode before it enters the live execution roster.
    """
    rows = conn.execute("SELECT agent, mode, enabled FROM agent_state").fetchall()
    state = {r["agent"]: (r["mode"], int(r["enabled"])) for r in rows}
    live_agents = []
    skipped: dict[str, str] = {}
    for agent in agents:
        mode, enabled = state.get(agent.name, ("paper", 1))
        if enabled == 1 and mode in ("live_small", "live"):
            live_agents.append(agent)
        else:
            skipped[agent.name] = f"mode={mode} enabled={enabled}"
    return live_agents, skipped


@app.command()
def init():
    """Create the database and ensure config dir exists."""
    conn, s = _conn()
    conn.close()
    CONFIG_DIR.mkdir(exist_ok=True)
    console.print(f"[green]✓[/green] DB at {s.db_path}")
    console.print(f"[green]✓[/green] Configs at {CONFIG_DIR}")


@app.command()
def ingest(funding_days: int = 7):
    """Pull fills, funding, and an equity snapshot from Hyperliquid."""
    conn, s = _conn()
    if not s.hl_address:
        console.print("[red]HL_ADDRESS not set in env[/red]")
        raise typer.Exit(1)
    n_fills = ingest_fills(conn, s.hl_address, s.hl_api_url)
    n_fund = ingest_funding(conn, s.hl_address, s.hl_api_url, funding_days)
    n_trans = ingest_transfers(conn, s.hl_address, s.hl_api_url)
    snapshot_equity(conn, s.hl_address, s.hl_api_url)
    from ..scoring.attribution import replay_positions_table
    n_pos = replay_positions_table(conn)
    console.print(
        f"[green]✓[/green] fills:{n_fills} funding:{n_fund} transfers:{n_trans} "
        f"+1 equity snapshot · positions replayed:{n_pos}"
    )


@app.command()
def accrue_xvenue(
    coins: str = typer.Option(
        "",
        help="Comma-separated coin list (default HLBOT_XVENUE_COINS, then sweep/confirm universe)",
    ),
):
    """Fetch Binance/Bybit funding and accrue into xvenue_funding (S5 signal fuel)."""
    import os

    conn, _s = _conn()
    universe = coins or os.environ.get(
        "HLBOT_XVENUE_COINS",
        os.environ.get("HLBOT_CONFIRM_UNIVERSE")
        or os.environ.get("HLBOT_SWEEP_UNIVERSE")
        or "BTC,ETH,SOL,HYPE,DOGE,XRP,WIF,kPEPE",
    )
    coin_list = [c.strip() for c in universe.split(",") if c.strip()]
    xvenue = fetch_xvenue_funding(coin_list)
    n = accrue_xvenue_funding(conn, xvenue)
    console.print(
        f"[green]✓[/green] xvenue funding: {n} rows across {len(xvenue)} coins"
    )


@app.command()
def score():
    """Print per-agent scorecards."""
    conn, _ = _conn()
    cards = score_all(conn)
    table = Table(title="Scorecards")
    for col in ("agent", "window", "n_trades", "net_pnl", "win_rate", "sharpe", "max_dd", "edge_bps"):
        table.add_column(col)
    for c in cards:
        table.add_row(
            c.agent, c.window, str(c.n_trades),
            f"{c.net_pnl:+.2f}",
            f"{c.win_rate*100:.0f}%",
            "—" if c.sharpe is None else f"{c.sharpe:+.2f}",
            "—" if c.max_drawdown is None else f"{c.max_drawdown*100:+.1f}%",
            "—" if c.edge_bps is None else f"{c.edge_bps:+.1f}",
        )
    console.print(table)


@app.command()
def supervisor(configs: Path = CONFIG_DIR):
    """Evaluate goals/guardrails for every agent config in ./configs."""
    conn, s = _conn()
    actions = supervise(conn, configs, data_dir=s.db_path.parent)
    console.print(json.dumps(actions, indent=2) if actions else "[dim]no actions taken[/dim]")


@app.command()
def kill(reason: str = typer.Argument("manual kill")):
    """Trip the kill switch: halt all NEW orders and promotions until `hlbot resume`.

    Flatten/cancel (risk reduction) stays allowed. Sticky across restarts."""
    from ..ops.kill import trip_kill

    _, s = _conn()
    line = trip_kill(s.db_path.parent, reason)
    console.print(f"[red]KILL tripped[/red]: {line}")


@app.command()
def resume():
    """Clear the kill switch and allow trading again."""
    from ..ops.kill import clear_kill

    _, s = _conn()
    if clear_kill(s.db_path.parent):
        console.print("[green]kill cleared — trading may resume[/green]")
    else:
        console.print("[dim]kill switch was not active[/dim]")


@app.command()
def research_strategies(write: bool = True):
    """Evaluate strategy health from fills; emit risk-reducing proposals.

    Read-only on trade data. Writes proposals to
    configs/agent_overrides.proposed.json (NEVER the live overrides), and never
    proposes raising any notional cap. Safe to run anytime; places no orders.
    """
    from ..scoring.metrics import list_agents

    conn, _ = _conn()
    agents = [
        a for a in list_agents(conn)
        if a not in ("_account", "manual") and not a.startswith("unknown:")
    ]
    overrides_path = CONFIG_DIR / "agent_overrides.json"
    current: dict = {}
    if overrides_path.exists():
        try:
            current = json.loads(overrides_path.read_text())
        except (ValueError, OSError):
            current = {}

    healths = [agent_health(conn, a) for a in agents]

    table = Table(title="Strategy health (realized, exchange-grounded)")
    for col in ("agent", "24h", "7d", "30d", "concentration", "losing coins"):
        table.add_column(col)

    def _cell(h, w: str) -> str:
        ws = h.windows.get(w)
        if ws is None or ws.n_trades == 0:
            return "—"
        edge = "—" if ws.edge_bps is None else f"{ws.edge_bps:+.0f}bps"
        return f"${ws.net_pnl:+.1f}/{edge}/{ws.n_trades}t"

    for h in healths:
        conc = "—" if h.concentration is None else f"{h.concentration*100:.0f}%"
        table.add_row(
            h.agent, _cell(h, "24h"), _cell(h, "7d"), _cell(h, "30d"),
            conc, ", ".join(h.losing_coins) or "—",
        )
    console.print(table)

    proposals = propose_overrides(healths, current)
    doc = build_proposal_document(proposals)
    any_proposal = False
    for p in proposals:
        if not (p.changes or p.flags or p.add_coin_vetoes):
            continue
        any_proposal = True
        console.print(f"[bold]{p.agent}[/bold]")
        if p.changes:
            console.print(f"  proposed (risk-reducing): {p.changes}")
        if p.add_coin_vetoes:
            console.print(f"  veto coins: {', '.join(p.add_coin_vetoes)}")
        for f in p.flags:
            console.print(f"  [yellow]⚠ {f}[/yellow]")
        for r in p.rationale:
            console.print(f"  [dim]{r}[/dim]")
    if not any_proposal:
        console.print("[dim]no risk-reducing changes proposed[/dim]")

    if write:
        out_path = CONFIG_DIR / "agent_overrides.proposed.json"
        out_path.write_text(json.dumps(doc, indent=2))
        console.print(f"[green]✓[/green] proposals written to {out_path}")
        console.print("[dim]review and merge into agent_overrides.json manually; "
                      "nothing was applied automatically[/dim]")
    else:
        console.print(json.dumps(doc, indent=2))


@app.command()
def tick(coins: str = "BTC,ETH,SOL,HYPE,ZEC"):
    """Run one tick of all wired agents (paper mode)."""
    conn, s = _conn()
    coin_list = [c.strip() for c in coins.split(",") if c.strip()]
    agents = [
        VetoAgent(config={"lookback_days": 30, "min_trades": 20, "veto_threshold_bps": -5.0}, conn=conn),
        FundingArbAgent(config={"coins": coin_list}),
        FundingCarryAgent(config={}, conn=conn),
        XFundCarryAgent(config={}, conn=conn),
    ]
    decisions = run_tick(conn, agents, s.hl_api_url, coin_list, force_paper=True)
    console.print(f"[green]✓[/green] {len(decisions)} decisions logged")
    for d in decisions:
        verdict = (d.market_snapshot or {}).get("verdict", "")
        tag = f"[{verdict}]" if verdict else ""
        console.print(f"  {d.agent} {d.action} {d.coin or ''} {tag} :: {d.reasoning}")


@app.command()
def run(
    live: bool = False,
    execution: str = "auto",   # per-agent execution_mode() (carry=maker,
                               # reversion/momentum=taker); 'maker'/'taker' force all
    interval: int = 20,
    ingest_every_s: int = 300,
    supervise_every_s: int = 900,
    enrich_every_s: int = 300,
    max_cycles: int = 0,
    profile: str = "",
):
    """Long-running event-paced engine — replaces the 5-min cron tick.

    Every ``interval`` seconds: build a market view (WS snapshot preferred,
    REST fallback) and run one consolidated cycle (paper sim + live execution).
    Every ``ingest_every_s``: ingest fills/funding/equity and refresh
    attribution; trips the kill switch on an equity-floor breach.
    Every ``supervise_every_s``: evaluate goals (auto-promotion lives here).
    ``max_cycles`` > 0 exits after N cycles (for testing/ops checks).
    ``--profile moonshot`` runs the ring-fenced sleeve: own data dir/DB/KILL,
    configs/moonshot/ contracts, and (via env) its own sub-account + wallet.
    """
    import os

    if profile:
        os.environ["HLBOT_PROFILE"] = profile

    from ..agents.runtime import fetch_market_view
    from ..engine.runner import build_roster, run_cycle
    from ..engine.views import enrich_view, overlay_ws_snapshot
    from ..ingest.hyperliquid import ingest_fills as _ingest_fills
    from ..ingest.hyperliquid import ingest_funding as _ingest_funding
    from ..ingest.hyperliquid import ingest_transfers as _ingest_transfers
    from ..ingest.hyperliquid import snapshot_equity as _snapshot_equity
    from ..ops.kill import equity_floor_breached, kill_active, trip_kill
    from ..scoring.positions import refresh_attribution

    conn, s = _conn()
    data_dir = s.db_path.parent
    heartbeat_path = data_dir / "run_heartbeat"

    exchange = info = None
    if live:
        from ..exec.orders import HL_TRADER_ADDRESS, build_exchange, telegram_alert
        if not HL_TRADER_ADDRESS:
            console.print("[red]FATAL: HL_TRADER_ADDRESS / HL_ADDRESS not set[/red]")
            raise typer.Exit(2)
        if not s.hl_address or s.hl_address.lower() != HL_TRADER_ADDRESS.lower():
            # Fill ingest is keyed to HL_ADDRESS; trading to HL_TRADER_ADDRESS.
            # A split brain makes fill detection, the daily-loss guardrail and
            # the equity floor watch the WRONG account. Refuse to start.
            console.print(
                f"[red]FATAL: HL_ADDRESS ({s.hl_address or 'unset'}) must equal "
                f"HL_TRADER_ADDRESS ({HL_TRADER_ADDRESS}) in live mode[/red]")
            raise typer.Exit(2)
        try:
            exchange, info, _ = build_exchange(env_path=s.api_wallet_env)
        except Exception as e:  # noqa: BLE001
            console.print(f"[red]FATAL: build_exchange failed: {e}[/red]")
            telegram_alert(f"🚨 hl-bot run: build_exchange failed: {e}")
            raise typer.Exit(2) from e

    configs_dir = s.configs_dir
    overrides_roster = build_roster(conn, configs_dir)
    console.print(
        f"[bold]hlbot run[/bold] live={live} execution={execution} interval={interval}s · "
        f"profile={s.profile or 'core'} · "
        f"roster: {', '.join(e.agent.name for e in overrides_roster)}"
    )

    last_ingest = 0.0
    last_supervise = 0.0
    ingest_failures = 0
    last_enrich = 0.0
    cached_extra: dict = {}
    enrich_offset = 0
    cycles = 0
    while True:
        t0 = time.time()
        try:
            view = fetch_market_view(s.hl_api_url, [])
            if t0 - last_enrich >= enrich_every_s:
                enrich_view(view, s.hl_api_url, view.extra.get("day_ntl_vlm", {}),
                            universe_size=s.enrich_universe_size,
                            max_workers=s.enrich_max_workers,
                            refresh_limit=s.enrich_refresh_limit,
                            rotate_offset=enrich_offset, carry_extra=cached_extra)
                cached_extra = dict(view.extra)
                last_enrich = t0
                if s.enrich_refresh_limit > 0:
                    enrich_offset = ((enrich_offset + s.enrich_refresh_limit)
                                     % max(1, s.enrich_universe_size))
            else:
                merged = dict(cached_extra)
                merged.update(view.extra)
                view.extra = merged
            overlay_ws_snapshot(view, os.environ.get("HLBOT_WS_SNAPSHOT"))

            res = run_cycle(conn, s, view, live=live, execution=execution,
                            exchange=exchange, info=info, configs_dir=configs_dir)
            console.print(f"[dim]{time.strftime('%H:%M:%S')}[/dim] {res.summary()}")
            for ev in res.events:
                console.print(f"  {ev}")

            if t0 - last_ingest >= ingest_every_s:
                last_ingest = t0
                if s.hl_address:
                    try:
                        _ingest_fills(conn, s.hl_address, s.hl_api_url)
                        _ingest_funding(conn, s.hl_address, s.hl_api_url, 7)
                        _ingest_transfers(conn, s.hl_address, s.hl_api_url)
                        _snapshot_equity(conn, s.hl_address, s.hl_api_url)
                        refresh_attribution(conn)
                        ingest_failures = 0
                    except Exception as ie:  # noqa: BLE001
                        ingest_failures += 1
                        logging.getLogger("hlbot.run").warning(
                            "ingest failed (%d consecutive): %s", ingest_failures, ie)
                        if live and ingest_failures >= 10 and not kill_active(data_dir):
                            # ~50 min blind: guardrails/equity floor/maker fill
                            # detection are all starved — stop the book.
                            trip_kill(data_dir, f"INGEST BLIND x{ingest_failures}: {ie}")
                            console.print("[red]KILL tripped: ingest blind[/red]")
                breached, why = equity_floor_breached(conn)
                if breached and not kill_active(data_dir):
                    trip_kill(data_dir, f"EQUITY FLOOR: {why}")
                    console.print(f"[red]KILL tripped: {why}[/red]")

            if t0 - last_supervise >= supervise_every_s:
                last_supervise = t0
                actions = supervise(conn, configs_dir, data_dir=data_dir)
                if actions:
                    console.print(f"[bold]supervisor[/bold]: {json.dumps(actions)}")

            heartbeat_path.touch()
            _ping_healthcheck()
        except KeyboardInterrupt:
            console.print("[yellow]run loop interrupted[/yellow]")
            return
        except Exception as e:  # noqa: BLE001
            log_ = logging.getLogger("hlbot.run")
            log_.exception("cycle failed")
            console.print(f"[red]cycle error: {e}[/red]")

        cycles += 1
        if max_cycles and cycles >= max_cycles:
            return
        time.sleep(max(0.0, interval - (time.time() - t0)))


def _ping_healthcheck() -> None:
    """Dead-man switch: GET HEALTHCHECK_URL after each successful cycle so a
    hung/crashed engine pages by SILENCE. Best-effort, never raises."""
    import os

    url = os.environ.get("HEALTHCHECK_URL")
    if not url:
        return
    try:
        import httpx

        httpx.get(url, timeout=5)
    except Exception:  # noqa: BLE001
        pass


@app.command()
def femr_tick(live: bool = False, execution: str = "auto"):
    """DEPRECATED — one-shot tick of the full agent roster, kept for manual
    ops; production runs `hlbot run` (consolidated engine loop).

    paper (default): log decisions only, no orders placed.
    live: place real orders on MAIN account, gated by guardrails.
          Bot only touches positions it itself opened (cloid-tagged).
    execution: 'auto' (default) routes each agent's entries per its own
          execution mode — carry/funding agents post maker, momentum agents
          cross taker; 'maker'/'taker' force one mode for every agent.
          Exits always go taker.
    """
    import os as _os

    from ..agents.decisions import log_decision
    from ..agents.runtime import fetch_market_view
    from ..exec.orders import (
        HL_TRADER_ADDRESS,
        GuardrailConfig,
        bot_owned_coins,
        build_exchange,
        check_guardrails,
        dynamic_daily_loss_limit,
        reconcile_positions,
        telegram_alert,
    )
    from ..exec.router import execute_decisions

    if execution not in ("auto", "maker", "taker"):
        console.print(f"[red]--execution must be auto|maker|taker, got {execution}[/red]")
        raise typer.Exit(1)

    conn, s = _conn()
    ws_path = _os.environ.get("HLBOT_WS_SNAPSHOT")

    # Load auto-tuner overrides if present
    overrides_path = Path(__file__).resolve().parents[3] / "configs" / "agent_overrides.json"
    overrides: dict = {}
    if overrides_path.exists():
        try:
            overrides = json.loads(overrides_path.read_text())
        except (ValueError, OSError):
            overrides = {}

    def _cfg(agent_name: str, defaults: dict) -> dict:
        merged = dict(defaults)
        merged.update(overrides.get(agent_name) or {})
        return merged

    import httpx as _httpx
    with _httpx.Client(timeout=10) as cli:
        st = cli.post(
            s.hl_api_url + "/info",
            json={"type": "clearinghouseState", "user": HL_TRADER_ADDRESS},
        ).json() or {}
        try:
            spot_st = cli.post(
                s.hl_api_url + "/info",
                json={"type": "spotClearinghouseState", "user": HL_TRADER_ADDRESS},
            ).json() or {}
        except _httpx.HTTPError:
            spot_st = {}
    acct_val = float((st.get("marginSummary") or {}).get("accountValue", 0) or 0)
    spot_usdc = spot_usdc_from_state(spot_st)
    portfolio_value = unified_portfolio_value(st, spot_st)
    withdrawable = float(st.get("withdrawable", 0) or 0)
    risk_cap = compute_notional_cap(conn, live_portfolio_value=portfolio_value)
    pv_label = "—" if risk_cap.portfolio_value is None else f"${risk_cap.portfolio_value:.2f}"
    console.print(
        "[bold]risk cap[/bold]: "
        f"bot-open <= ${risk_cap.max_total_notional:.0f}; "
        f"per-position <= ${risk_cap.max_per_position_notional:.0f} "
        f"({risk_cap.multiplier:g}x / {risk_cap.per_position_multiplier:g}x live unified portfolio {pv_label}; "
        f"perp ${acct_val:.2f} + spot USDC ${spot_usdc:.2f}; "
        f"ceiling={'none' if risk_cap.ceiling_notional is None else f'${risk_cap.ceiling_notional:.0f}'}; "
        f"source={risk_cap.source})"
    )

    # Instantiate the full agent roster. In paper mode, evaluate everything. In
    # live mode, only agents explicitly enabled and promoted to live_small/live
    # in agent_state are allowed into the execution roster.
    # Roster: confirmed post-cost bleeders (twap_mr_v1 taker, basis_v1) are
    # retired (configs/*.yaml roster: retired) so they stop consuming
    # MetaAllocator weight; the maker-designed carry strategies are in.
    agents = [
        FemrAgent(config=_cfg("femr_v1", {
            "max_notional_per_trade": 20.0,
            "max_total_notional": 40.0,
            "funding_enter_per_hr": 0.00015,
            "funding_exit_per_hr": 0.00005,
        }), conn=conn),
        XFundCarryAgent(config=_cfg("xfund_carry_v1", {}), conn=conn),
        FundingCarryAgent(config=_cfg("funding_carry_v1", {}), conn=conn),
        TwapMrRegimeAgent(config=_cfg("twap_mr_regime_v1", {}), conn=conn),
        BasisAgent(config=_cfg("basis_v1", {}), conn=conn),
        # liq_cascade is entry-dead without a WS snapshot (its only real signal
        # source — REVIEW C6) but MUST stay on the roster: its stop/max-hold
        # exits and position reconciliation manage anything it already holds.
        LiqCascadeAgent(config=_cfg("liq_cascade_v1", {}), conn=conn),
    ]
    paper_sim_agents: list = []
    if live and not ws_path:
        console.print("[dim]liq_cascade_v1: no HLBOT_WS_SNAPSHOT — no liquidation "
                      "signal, entries impossible (exits still managed)[/dim]")
    if live:
        heartbeat = Path(str(s.db_path.parent / "run_heartbeat"))
        if heartbeat.exists() and (time.time() - heartbeat.stat().st_mtime) < 60:
            console.print("[red]REFUSED: hlbot run is active (run_heartbeat fresh) — "
                          "two live executors would duplicate orders[/red]")
            raise typer.Exit(2)
        full_roster = agents
        agents, skipped_live = _filter_live_agents_by_state(conn, agents)
        disabled = {
            r["agent"] for r in
            conn.execute("SELECT agent FROM agent_state WHERE enabled = 0").fetchall()
        }
        live_names = {a.name for a in agents}
        # Paper-mode (but not paused) agents keep trading in the simulator so
        # their scorecards accrue the evidence auto-promotion gates on.
        paper_sim_agents = [
            a for a in full_roster
            if a.name not in live_names and a.name not in disabled
        ]
        if skipped_live:
            console.print(
                "[yellow]live roster skipped[/yellow]: "
                + ", ".join(f"{name}({why})" for name, why in skipped_live.items())
            )
        if not agents:
            console.print("[yellow]LIVE MODE but no agent_state rows are enabled in live_small/live; paper simulation only[/yellow]")

    # Allocator: rebalance per-agent caps from rolling 7d performance.
    # The approved live risk rule is dynamic but layered:
    #   - aggregate bot-open notional can reach 5x live unified portfolio value
    #   - any SINGLE agent is limited to 1x portfolio value (max_alloc), so one
    #     agent can never consume the whole 5x portfolio cap.
    # resolve_agent_caps applies the final rule: explicit (sub-legacy) configured
    # caps win, legacy broad $1000 ceilings are replaced by the dynamic 1x cap,
    # and configured per-trade sizes are preserved (never raised).
    allocator = MetaAllocator(
        [a.name for a in agents],
        MetaAllocatorConfig(
            total_capital=risk_cap.max_total_notional,
            max_alloc=risk_cap.max_per_position_notional,
        ),
    )
    allocs = allocator.allocate(conn)
    configured_caps_in = {
        a.name: {
            "max_total_notional": float(getattr(a.cfg, "max_total_notional", float("inf"))),
            "max_notional_per_trade": float(getattr(a.cfg, "max_notional_per_trade", float("inf"))),
        }
        for a in agents if hasattr(a, "cfg")
    }
    resolved = resolve_agent_caps(allocs, risk_cap, configured_caps_in)
    if live:
        # Same clamp the engine applies: live_small runs deliberately tiny
        # regardless of allocator grant (this path skipped it — a live_small
        # agent could size at the full 1x-portfolio cap).
        from ..engine.runner import load_agent_goals
        from ..risk.allocation import apply_mode_sizing
        modes = {r["agent"]: r["mode"] for r in
                 conn.execute("SELECT agent, mode FROM agent_state").fetchall()}
        goals_by_agent = load_agent_goals(CONFIG_DIR)
        for name, cap in list(resolved.items()):
            g = goals_by_agent.get(name)
            resolved[name] = apply_mode_sizing(
                cap, modes.get(name, "paper"), g.sizing if g else None)
    effective_caps: dict[str, float] = {}
    effective_order_caps: dict[str, float] = {}
    for a in agents:
        cap = resolved.get(a.name)
        if cap is None:
            effective_caps[a.name] = allocs.get(a.name, 0.0)
            continue
        effective_caps[a.name] = cap.max_total_notional
        if hasattr(a, "cfg") and hasattr(a.cfg, "max_total_notional"):
            a.cfg.max_total_notional = cap.max_total_notional
            if hasattr(a.cfg, "max_notional_per_trade"):
                a.cfg.max_notional_per_trade = cap.max_notional_per_trade
                effective_order_caps[a.name] = cap.max_notional_per_trade
    console.print("[bold]allocator caps[/bold]: " +
                  ", ".join(
                      f"{n}=total ${effective_caps.get(n, v):.0f}/pos ${effective_order_caps.get(n, 0):.0f}"
                      for n, v in allocs.items()
                  ))

    # Entry execution per agent: 'auto' asks each agent (config override or
    # class default — carry agents post maker, momentum crosses taker);
    # an explicit --execution maker/taker forces every agent.
    exec_modes = {
        a.name: (a.execution_mode() if execution == "auto" else execution)
        for a in agents
    }
    console.print("[bold]execution[/bold]: " +
                  ", ".join(f"{n}={m}" for n, m in exec_modes.items()))

    view = fetch_market_view(s.hl_api_url, [])
    _enrich_view(view, s.hl_api_url, view.extra.get("day_ntl_vlm", {}),
                 universe_size=s.enrich_universe_size, max_workers=s.enrich_max_workers)

    # Overlay a fresh WS snapshot if available (sub-second mids, L2 book, and a
    # REAL liquidations feed for liq_cascade). Purely additive; REST is the
    # fallback when no fresh snapshot exists. Opt-in via HLBOT_WS_SNAPSHOT.
    if ws_path:
        from ..ingest.ws import load_fresh_snapshot
        snap = load_fresh_snapshot(ws_path, max_age_s=30.0)
        if snap is not None:
            view.mids.update(snap.mids)
            view.funding.update(snap.funding)
            if snap.book_top:
                view.book_top.update(snap.book_top)
            liqs = snap.extra.get("liquidations") or []
            if liqs:
                view.extra["liquidations"] = liqs
            console.print(f"[dim]ws snapshot overlaid: {len(snap.mids)} mids, "
                          f"{len(liqs)} liqs[/dim]")

    # Build position list from HL truth
    all_positions = []
    for ap in st.get("assetPositions", []) or []:
        pos = ap.get("position", {}) or {}
        with contextlib.suppress(TypeError, ValueError):
            all_positions.append({
                "coin": pos.get("coin"),
                "szi": float(pos.get("szi", 0) or 0),
                "entry_px": float(pos.get("entryPx", 0) or 0),
                "position_value": float(pos.get("positionValue", 0) or 0),
                "unrealized_pnl": float(pos.get("unrealizedPnl", 0) or 0),
                "liquidation_px": float(pos.get("liquidationPx", 0) or 0),
                "leverage": (pos.get("leverage") or {}).get("value"),
                "margin_used": float(pos.get("marginUsed", 0) or 0),
            })

    # RECONCILE first — clear stale DB ownership for each agent independently
    reconciled_all: dict[str, list[str]] = {}
    for a in agents:
        r = reconcile_positions(conn, all_positions, agent=a.name)
        if r:
            reconciled_all[a.name] = r
    if reconciled_all:
        console.print(f"[yellow]reconciled stale ownership: {reconciled_all}[/yellow]")

    # FEMR sees only its own owned coins (adopts handled internally by name match).
    owned_femr = bot_owned_coins(conn, agent="femr_v1")
    bot_positions = [p for p in all_positions if p["coin"] in owned_femr]
    view.extra["live_positions"] = bot_positions

    owned_all: set[str] = set()
    for a in agents:
        owned_all |= bot_owned_coins(conn, agent=a.name)
    manual_coins = [p["coin"] for p in all_positions if p["coin"] not in owned_all]
    console.print(
        f"[dim]market: {len(view.mids)} coins, {len(view.funding)} funding · "
        f"candles: {len(view.extra.get('candles_1h', {}))} · "
        f"spot: {sorted(view.extra.get('spot_mids', {}).keys())} · "
        f"liqs: {len(view.extra.get('liquidations', []))} · "
        f"acct ${acct_val:.2f}, free ${withdrawable:.2f} · "
        f"bot-owned: {sorted(owned_all) or '∅'} · manual: {manual_coins or '∅'}[/dim]"
    )

    paper_names = {a.name for a in paper_sim_agents}
    all_decisions = []
    for agent in [*agents, *paper_sim_agents]:
        decisions = agent.decide(view)
        for d in decisions:
            d.is_paper = (not live) or (agent.name in paper_names)
            # Only log non-place/flatten actions immediately (holds skipped, rejected later).
            # `place` and `flatten` are logged ONLY after exchange acceptance in the execution
            # loop below (or after a simulated fill in the paper simulator) — otherwise the
            # cooldown check would see our own intent rows and block subsequent ticks forever.
            if d.action not in ("hold", "place", "flatten"):
                log_decision(conn, d)
            all_decisions.append(d)

    console.print(f"[green]✓[/green] {len(all_decisions)} decisions (live={live})")
    for d in all_decisions:
        tag = "" if d.action != "hold" else "[dim]"
        end = "" if d.action != "hold" else "[/dim]"
        console.print(f"  {tag}{d.agent} {d.action} {d.coin or ''} :: {d.reasoning}{end}")

    from ..sim.paper import simulate_cycle

    if not live:
        # Every decision is paper: simulate fills so paper performance is
        # scoreable (n_trades/edge/sharpe gates can actually fire).
        sim = simulate_cycle(
            conn, view,
            [d for d in all_decisions if d.action in ("place", "flatten")],
            maker_entries=(execution == "maker"),
        )
        console.print(f"[yellow]PAPER MODE[/yellow] — {sim.summary()}")
        return

    paper_decisions = [
        d for d in all_decisions
        if d.agent in paper_names and d.action in ("place", "flatten")
    ]
    if paper_names:
        sim = simulate_cycle(conn, view, paper_decisions,
                             maker_entries=(execution == "maker"))
        console.print(f"[dim]{sim.summary()}[/dim]")
    if not agents:
        return  # nothing enabled for live execution

    from ..ops.kill import kill_active
    kill_reason = kill_active(s.db_path.parent)
    if kill_reason:
        console.print(
            f"[red]KILL ACTIVE[/red]: {kill_reason} — "
            "new entries blocked; flatten/cancel still allowed (`hlbot resume` to clear)"
        )

    try:
        exchange, info, _ = build_exchange()
    except Exception as e:  # noqa: BLE001
        console.print(f"[red]FATAL: build_exchange failed: {e}[/red]")
        telegram_alert(f"🚨 hl-bot: build_exchange failed: {e}")
        raise typer.Exit(2) from e

    ok, why = check_guardrails(
        conn,
        info,
        GuardrailConfig(
            min_bot_capital=40.0,
            max_daily_loss=dynamic_daily_loss_limit(portfolio_value),
            max_total_notional=risk_cap.max_total_notional,
            max_concurrent_positions=4,
        ),
        agents=[a.name for a in agents],
    )
    if not ok:
        console.print(f"[red]HALT new entries[/red]: {why}")
        console.print("[yellow]Flatten/close decisions are still allowed for risk reduction.[/yellow]")
    else:
        console.print(f"[green]guardrails[/green]: {why}")

    # Maker execution prep: refresh fills, promote filled resting orders to owned,
    # cancel stale quotes. Runs when any agent quotes maker this tick OR any
    # agent still has a working quote from a previous tick — flipping an agent
    # (or the whole tick) to taker must never orphan a live resting order.
    from ..exec.maker import working_orders
    working_by_agent = {a.name: working_orders(conn, a.name) for a in agents}
    if any(m == "maker" for m in exec_modes.values()) or any(working_by_agent.values()):
        from ..exec.maker import log_cancel, reconcile_maker_fills, stale_working
        from ..exec.orders import cancel_order
        from ..ingest.hyperliquid import ingest_fills
        ingest_fills(conn, s.hl_address, s.hl_api_url)  # so cloid fills are visible
        for a in agents:
            working = working_by_agent[a.name]
            got = reconcile_maker_fills(conn, a.name, working)
            for o in stale_working(working):
                if o["coin"] in got or o.get("oid") is None:
                    continue
                cancel_order(exchange, o["coin"], o["oid"])
                log_cancel(conn, a.name, o)
            if got:
                console.print(f"[green]maker fills[/green] {a.name}: {got}")
        conn.commit()

    # Execute through the single audited router (exec/router.py): per-agent
    # maker/taker entries (maker quotes priced off the live book), taker exits,
    # guardrail/cooldown gates, fill-confirmed decision logging. The sticky
    # kill switch vetoes new entries; risk-reducing flatten/closes still run.
    outcomes = execute_decisions(
        conn, exchange, all_decisions,
        exec_modes=exec_modes, entries_allowed=ok and not kill_reason,
        book_top=view.book_top,
    )
    for oc in outcomes:
        if oc.status == "filled":
            console.print(f"[bold green]FILLED[/bold green] {oc.agent} {oc.coin} {oc.sz} @ ${oc.px} [{oc.mode}]")
        elif oc.status == "resting":
            console.print(f"[cyan]RESTING[/cyan] {oc.agent} {oc.coin} {oc.sz} @ ${oc.px} {oc.detail}")
        elif oc.status == "closed":
            console.print(f"[bold]CLOSED[/bold] {oc.agent} {oc.coin} @ ${oc.px}")
        elif oc.status == "rejected":
            console.print(f"[red]REJECT[/red] {oc.agent} {oc.coin}: {oc.detail}")
        elif oc.status == "close_failed":
            console.print(f"[red]CLOSE FAILED[/red] {oc.agent} {oc.coin}: {oc.detail}")
        else:
            console.print(f"[dim]SKIP {oc.agent} {oc.coin}: {oc.detail}[/dim]")


@app.command()
def backtest_fetch(
    coins: str = "BTC,ETH,SOL",
    interval: str = "1h",
    days: int = 30,
    refresh: bool = False,
):
    """Fetch + cache HL candle/funding history for offline, reproducible backtests.

    Writes a gzipped frame dataset under data/backtest_cache/ (gitignored).
    Run this once where HL is reachable; then `hlbot backtest` runs without network.
    """
    from ..backtest.data import cached_or_fetch, default_cache_path

    _, s = _conn()
    coin_list = [c.strip() for c in coins.split(",") if c.strip()]
    path = default_cache_path(coin_list, interval, days)
    console.print(f"[dim]fetching {days}d {interval} for {coin_list}…[/dim]")
    try:
        frames = cached_or_fetch(coin_list, interval=interval, days=days,
                                 base_url=s.hl_api_url, refresh=refresh)
    except Exception as e:  # noqa: BLE001
        console.print(f"[red]fetch failed: {e}[/red]")
        raise typer.Exit(2) from e
    console.print(f"[green]✓[/green] cached {len(frames)} frames → {path}")


@app.command()
def backtest(
    agent: str = "twap_mr_v1",
    coins: str = "BTC,ETH,SOL",
    interval: str = "1h",
    days: int = 30,
    maker: bool = False,
    compare: bool = True,
    starting_capital: float = 1000.0,
    cache: bool = True,
):
    """Replay an agent over real Hyperliquid history with an explicit cost model.

    Fetches candle + funding history (network), drives the agent's real
    ``decide()``, simulates fills (taker by default), and scores the run with the
    same code used live. With ``--compare`` (default) it runs taker AND maker so
    you can see how much of the edge the spread is eating — the central question
    for this book. Places no orders; purely offline analysis.
    """
    from ..backtest.data import cached_or_fetch, load_frames
    from ..backtest.engine import Backtester, CostModel

    _, s = _conn()
    coin_list = [c.strip() for c in coins.split(",") if c.strip()]

    factories = {
        "twap_mr_v1": lambda conn: TwapMrAgent(config={}, conn=conn),
        "twap_mr_regime_v1": lambda conn: TwapMrRegimeAgent(config={}, conn=conn),
        "femr_v1": lambda conn: FemrAgent(config={}, conn=conn),
        "funding_carry_v1": lambda conn: FundingCarryAgent(config={}, conn=conn),
        "spot_perp_carry_v1": lambda conn: SpotPerpCarryAgent(config={}, conn=conn),
        "xfund_carry_v1": lambda conn: XFundCarryAgent(config={}, conn=conn),
        "liq_cascade_v1": lambda conn: LiqCascadeAgent(config={}, conn=conn),
        "dislocation_reversion_v1": lambda conn: DislocationReversionAgent(config={}, conn=conn),
        "funding_crowding_fade_v1": lambda conn: FundingCrowdingFadeAgent(config={}, conn=conn),
        "new_listing_reversion_v1": lambda conn: NewListingReversionAgent(config={}, conn=conn),
        "basis_v1": lambda conn: BasisAgent(config={}, conn=conn),
    }
    if agent not in factories:
        console.print(f"[red]unknown agent {agent}; choose from {list(factories)}[/red]")
        raise typer.Exit(1)

    console.print(f"[dim]loading {days}d of {interval} candles for {coin_list} "
                  f"({'cache' if cache else 'network'})…[/dim]")
    try:
        frames = (cached_or_fetch(coin_list, interval=interval, days=days, base_url=s.hl_api_url)
                  if cache else
                  load_frames(coin_list, interval=interval, days=days, base_url=s.hl_api_url))
    except Exception as e:  # noqa: BLE001
        console.print(f"[red]failed to load history: {e}[/red]")
        raise typer.Exit(2) from e
    if not frames:
        console.print("[red]no frames built (insufficient history)[/red]")
        raise typer.Exit(2)
    console.print(f"[dim]{len(frames)} frames[/dim]")

    per_year = {"1m": 525_600, "5m": 105_120, "15m": 35_040,
                "1h": 8_760, "4h": 2_190, "1d": 365}.get(interval, 8_760)

    modes = [False, True] if compare else [maker]
    table = Table(title=f"Backtest {agent} ({days}d {interval})")
    for col in ("exec", "net_pnl", "edge_bps", "trades", "win", "sharpe", "maxDD"):
        table.add_column(col)
    for is_maker in modes:
        from ..db.schema import init_db as _init
        conn = _init(":memory:")
        bt = Backtester(CostModel(maker=is_maker), conn=conn,
                        starting_capital=starting_capital)
        res = bt.run(factories[agent](conn), frames)
        # recompute curve stats at the right cadence
        from ..scoring.curves import curve_stats
        sh, dd, _ = curve_stats(res.equity_curve, periods_per_year=per_year)
        sc = res.scorecard
        table.add_row(
            "maker" if is_maker else "taker",
            f"{sc.net_pnl:+.2f}",
            "—" if sc.edge_bps is None else f"{sc.edge_bps:+.1f}",
            str(sc.n_trades),
            f"{sc.win_rate*100:.0f}%",
            "—" if sh is None else f"{sh:+.2f}",
            "—" if dd is None else f"{dd*100:+.1f}%",
        )
    console.print(table)
    console.print("[dim]taker→maker gap ≈ the spread/fee tax this strategy is paying.[/dim]")


# Per-interval annualization + HL retention-aware default windows for confirm.
_PER_YEAR = {"1m": 525_600, "5m": 105_120, "15m": 35_040,
             "1h": 8_760, "4h": 2_190, "1d": 365}
_SEC_TO_INTERVAL = {60: "1m", 300: "5m", 900: "15m", 3600: "1h",
                    14_400: "4h", 86_400: "1d"}


def _confirm_and_record(
    conn, s, agent: str, *, coins: str, interval: str, days: int, prefer: str,
    min_edge_bps: float = 3.0, min_sharpe: float = 1.0, cache: bool = True,
    refresh: bool = False, use_overrides: bool = True, params: str = "",
    record: bool = False, use_accrued: bool = True,
):
    """Run the G0 gate for one agent built from its DEPLOYED config (V3) and,
    with ``record``, stamp the verdict + params_hash into ``confirmations``.

    Shared by ``confirm`` (one agent, verbose) and ``autoconfirm`` (the nightly
    forward loop). With ``use_accrued`` the confirm frames are
    ``back-fetched ∪ forward frame_samples`` (P1 linchpin), so a retention-capped
    5m agent's OOS window GROWS forward past HL's ~17.5d instead of just rolling.
    Returns ``(res, phash, dataset, cfg, cov)``. Raises on an unknown agent
    (KeyError) or a history-load failure (the caller decides how loud to be)."""
    from ..agents.fingerprint import config_fingerprint
    from ..backtest.confirm import confirm_strategy
    from ..backtest.data import (
        cached_or_fetch,
        frames_coverage_days,
        load_accrued_frames,
        load_frames,
        merge_frames,
    )
    from ..engine.runner import AGENT_FACTORIES, _load_overrides

    factory_fn = AGENT_FACTORIES.get(agent)
    if factory_fn is None:
        raise KeyError(agent)
    coin_list = [c.strip() for c in coins.split(",") if c.strip()]
    # Deployed config = factory defaults + agent_overrides.json (exactly what the
    # runner instantiates), optionally + an ad-hoc --params JSON for what-if runs.
    cfg: dict = {}
    if use_overrides:
        cfg.update(_load_overrides(s.configs_dir).get(agent) or {})
    if params:
        cfg.update(json.loads(params))   # caller catches ValueError
    factory = lambda conn, _cfg=dict(cfg): factory_fn(conn, dict(_cfg))  # noqa: E731
    phash = config_fingerprint(factory(None))
    per_year = _PER_YEAR.get(interval, 8_760)
    frames = (cached_or_fetch(coin_list, interval=interval, days=days,
                              base_url=s.hl_api_url, refresh=refresh)
              if cache else
              load_frames(coin_list, interval=interval, days=days, base_url=s.hl_api_url))
    # Union HL's retention-capped candles with the forward-accrued frame store:
    # HL's official bars win inside its window; accrued bars extend it backward
    # (the window the agent actually soaked), so the OOS sample grows forward.
    # Bound accrued to the SAME requested window (now - days) so `--days N` means
    # "the last N days" deterministically — coverage within it still grows toward
    # N as accrual deepens, but a longer soak can't silently inflate the window
    # past the request and drift the walk-forward split / recorded provenance.
    n_back = len(frames)
    if use_accrued:
        since_ms = int(time.time() * 1000) - days * 86_400_000 if days > 0 else None
        accrued = load_accrued_frames(conn, coin_list, interval, since_ms=since_ms)
        if accrued:
            frames = merge_frames(frames, accrued)
    n_accrued_added = len(frames) - n_back
    res = confirm_strategy(
        factory, frames, prefer=prefer,
        min_edge_bps=min_edge_bps, min_sharpe=min_sharpe, periods_per_year=per_year,
    )
    # Record the ACTUAL coverage, not just the requested days: at fine intervals
    # HL serves only ~5000 candles (5m → ~17d), so "90d" in a record overstates.
    # With accrued frames the real span is the union's, which grows past that.
    cov = frames_coverage_days(frames)
    dataset = f"{coins}/{interval}/{days}d"
    if frames and cov < days * 0.9:
        dataset = f"{coins}/{interval}/{days}d(actual~{cov:.0f}d)"
    if n_accrued_added > 0:
        dataset += f"+{n_accrued_added}fwd"
    if record:
        conn.execute(
            """INSERT INTO confirmations(agent, ts_ms, dataset, prefer, confirmed,
                                         oos_edge_bps, summary, params_hash)
               VALUES(?,?,?,?,?,?,?,?)""",
            (agent, int(time.time() * 1000), dataset, prefer,
             1 if res.confirmed else 0, res.out_of_sample.edge_bps, res.summary(), phash),
        )
        conn.commit()
    return res, phash, dataset, cfg, cov


@app.command()
def confirm(
    agent: str = "twap_mr_regime_v1",
    coins: str = "BTC,ETH,SOL,HYPE",
    interval: str = "1h",
    days: int = 120,
    prefer: str = "taker",
    min_edge_bps: float = 3.0,
    min_sharpe: float = 1.0,
    cache: bool = True,
    record: bool = False,
    use_overrides: bool = True,
    params: str = "",
    accrued: bool = True,
):
    """Confirm a strategy through the G0 gate: walk-forward + cost stress.

    Prints an explicit PASS/FAIL. A strategy must clear this on real history
    before it is eligible for paper→live promotion. With --record the verdict
    is stamped into the confirmations table, which is what promotion stages
    with require_g0 check (auto-promotion runs on this evidence).

    V3 provenance: the agent is built from the SAME config the engine deploys
    (factory defaults + agent_overrides.json, unless --no-use-overrides), with
    an optional ad-hoc --params JSON merged on top, and the deployed config's
    params_hash is stamped into the record. Promotion's require_g0 matches that
    hash, so a tuned override can never inherit a G0 earned for other params.
    """
    from ..engine.runner import AGENT_FACTORIES

    conn, s = _conn()
    if agent not in AGENT_FACTORIES:
        console.print(f"[red]unknown agent {agent}; choose from {list(AGENT_FACTORIES)}[/red]")
        raise typer.Exit(1)
    try:
        res, phash, dataset, cfg, cov = _confirm_and_record(
            conn, s, agent, coins=coins, interval=interval, days=days, prefer=prefer,
            min_edge_bps=min_edge_bps, min_sharpe=min_sharpe, cache=cache,
            use_overrides=use_overrides, params=params, record=record, use_accrued=accrued)
    except ValueError as e:
        console.print(f"[red]--params must be valid JSON: {e}[/red]")
        raise typer.Exit(2) from e
    except Exception as e:  # noqa: BLE001
        console.print(f"[red]failed to load history: {e}[/red]")
        raise typer.Exit(2) from e
    console.print(res.summary())
    console.print(f"[dim]deployed params_hash={phash}"
                  + (f" (config: {json.dumps(cfg)})" if cfg else " (factory defaults)")
                  + "[/dim]")
    if "actual~" in dataset:
        console.print(f"[yellow]note:[/yellow] only ~{cov:.1f}d of {interval} history exists "
                      f"at HL (retention cap); G0 evidence window is ~{cov:.0f}d, not {days}d.")
    if record:
        # The INSERT (with the deployed config's params_hash) already happened in
        # _confirm_and_record(record=True); main's inline default-config stamp is
        # superseded by that override-aware path.
        console.print(f"[dim]confirmation recorded (confirmed={res.confirmed}, "
                      f"params_hash={phash})[/dim]")
    if not res.confirmed:
        raise typer.Exit(1)


def _autoconfirm_targets(roster, modes: dict, explicit: set):
    """Which roster entries the nightly forward auto-confirm should re-run.

    Default: agents whose CURRENT mode is paper AND whose paper→live_small stage
    requires G0 — i.e. a fresh forward G0 is exactly what blocks their
    promotion. ``explicit`` (an --agents allow-list) overrides the rule. Pure so
    the selection is unit-tested without touching the network."""
    out = []
    for e in roster:
        name = e.agent.name
        if explicit:
            if name in explicit:
                out.append(e)
            continue
        if modes.get(name, "paper") != "paper":
            continue   # already promoted past paper — not what the flywheel waits on
        paper_stage = next((p for p in e.goals.ladder() if p.from_mode == "paper"), None)
        if paper_stage and paper_stage.require_g0:
            out.append(e)
    return out


@app.command()
def autoconfirm(
    coins: str = "BTC,ETH,SOL,HYPE,DOGE,XRP,WIF,kPEPE",
    days: int = 0,
    prefer: str = "taker",
    record: bool = True,
    agents: str = "",
    cache: bool = True,
    refresh: bool = True,
):
    """Nightly FORWARD auto-confirm loop (P1c): re-run `hlbot confirm --record`
    over the accrued forward window for every unconfirmed agent, so any that now
    clear a params-matched G0 are auto-promoted by the supervisor — no human
    step. See docs/research/P1_forward_evidence_flywheel.md.

    Targets, by default, agents whose CURRENT mode is paper AND whose
    paper→live_small stage requires G0 (i.e. a fresh forward G0 is exactly what
    blocks their promotion). Pass --agents a,b to override the set. Each agent's
    interval is derived from its cfg bar_seconds; --days 0 picks an HL-retention-
    aware default per interval. --refresh (default on) re-fetches the latest
    candles so the rolling window advances each night instead of re-confirming a
    stale cached dataset (refreshed once per distinct interval/days). Resilient:
    one agent's failure never stops the rest. Runs after the nightly sweep."""
    from ..engine.runner import _load_overrides, build_roster

    conn, s = _conn()
    roster = build_roster(conn, s.configs_dir, _load_overrides(s.configs_dir))
    modes = {r["agent"]: r["mode"]
             for r in conn.execute("SELECT agent, mode FROM agent_state").fetchall()}
    explicit = {a.strip() for a in agents.split(",") if a.strip()}
    targets = _autoconfirm_targets(roster, modes, explicit)

    if not targets:
        console.print("[dim]autoconfirm: no unconfirmed paper agents awaiting G0[/dim]")
        return

    console.print(f"[bold]autoconfirm[/bold] {len(targets)} agent(s): "
                  + ", ".join(e.agent.name for e in targets))
    n_confirmed = 0
    refreshed: set[tuple[str, int]] = set()   # dedupe network refreshes per dataset
    for e in targets:
        name = e.agent.name
        bar_s = int(getattr(getattr(e.agent, "cfg", None), "bar_seconds", 0) or 0)
        interval = _SEC_TO_INTERVAL.get(bar_s, "1h")
        d = days or {"1m": 90, "5m": 90, "15m": 90}.get(interval, 210)
        # Refresh the cache once per (interval, days); agents that share a
        # dataset reuse the freshened frames rather than re-fetching.
        do_refresh = cache and refresh and (interval, d) not in refreshed
        refreshed.add((interval, d))
        try:
            res, phash, dataset, _cfg, cov = _confirm_and_record(
                conn, s, name, coins=coins, interval=interval, days=d, prefer=prefer,
                cache=cache, refresh=do_refresh, record=record)
        except Exception as ex:  # noqa: BLE001 - one agent must not abort the loop
            console.print(f"  [yellow]{name}: confirm failed ({ex})[/yellow]")
            continue
        verdict = "[green]✅ CONFIRMED[/green]" if res.confirmed else "❌ not confirmed"
        n_confirmed += int(res.confirmed)
        console.print(f"  {name}: {verdict} "
                      f"(interval={interval} ~{cov:.0f}d, OOS edge "
                      f"{('—' if res.out_of_sample.edge_bps is None else f'{res.out_of_sample.edge_bps:+.1f}bps')}, "
                      f"params_hash={phash})")
    console.print(f"[green]✓[/green] autoconfirm done: {n_confirmed}/{len(targets)} "
                  f"now clear G0{' (recorded)' if record else ''}")


@app.command()
def s8_oi_backtest(
    coins: str = "BTC,ETH,SOL,DOGE,AVAX,LINK,SUI,WLD",
    days: int = 30,
    prefer: str = "taker",
    lookback_min: int = 30,
    min_edge_bps: float = 3.0,
    min_sharpe: float = 1.0,
    params: str = "",
    sweep: bool = False,
):
    """Measure the S8 OI-crowding edge on real Binance OI history.

    HL never serves OI history (only candles), so oi_crowding_reversal can't be
    back-tested on HL — only confirmed FORWARD over weeks. Binance publishes ~30d
    of 5m OI, a usable cross-venue PROXY for crowding: this loads HL 5m candle
    frames, overlays Binance OI-change, and runs the SAME G0 gate as `confirm`.
    OI comes from Binance's PUBLIC dumps (data.binance.vision), which work even
    where the fapi API is geo-blocked (US hosts / CI get HTTP 451). With --sweep
    it calibrates the OI-change distribution and grids oi_spike_min × z_enter so
    you can pick a threshold instead of guessing."""
    from ..backtest.confirm import confirm_strategy
    from ..backtest.data import cached_or_fetch, frames_coverage_days, overlay_oi_change
    from ..engine.runner import AGENT_FACTORIES
    from ..research.oi_history import fetch_binance_oi_vision, hl_to_binance
    from ..research.sweep import SweepRow, SweepSpec, write_outputs

    s = Settings.from_env()
    coin_list = [c.strip() for c in coins.split(",") if c.strip()]
    cfg: dict = {}
    if params:
        try:
            cfg.update(json.loads(params))
        except ValueError as e:
            console.print(f"[red]--params must be valid JSON: {e}[/red]")
            raise typer.Exit(2) from e

    console.print(f"[dim]loading {len(coin_list)} coins × {days}d 5m candles…[/dim]")
    frames = cached_or_fetch(coin_list, interval="5m", days=days, base_url=s.hl_api_url)
    if not frames:
        console.print("[red]no candle frames built[/red]")
        raise typer.Exit(2)

    oi_by_coin: dict[str, list[tuple[int, float]]] = {}
    skipped: list[str] = []
    for coin in coin_list:
        if hl_to_binance(coin) is None:
            skipped.append(coin)
            continue
        pts = fetch_binance_oi_vision(coin, days=days)
        if pts:
            oi_by_coin[coin] = pts
        else:
            skipped.append(coin)
    n_sig = overlay_oi_change(frames, oi_by_coin, lookback_ms=lookback_min * 60_000)
    if not oi_by_coin:
        console.print("[red]no Binance OI fetched (network blocked?) — "
                      "cannot determine the edge[/red]")
        raise typer.Exit(2)
    cov = frames_coverage_days(frames)
    console.print(f"[dim]OI for {len(oi_by_coin)} coin(s) "
                  f"(skipped {','.join(skipped) or '∅'}); {n_sig} bar-signals overlaid; "
                  f"~{cov:.0f}d window[/dim]")

    def _run(extra: dict):
        fac = lambda conn, _c=dict(extra): AGENT_FACTORIES["oi_crowding_reversal_v1"](conn, dict(_c))  # noqa: E731
        return confirm_strategy(fac, frames, prefer=prefer, min_edge_bps=min_edge_bps,
                                min_sharpe=min_sharpe, periods_per_year=_PER_YEAR["5m"])
    _e = lambda v: "—" if v is None else f"{v:+.1f}"  # noqa: E731

    if sweep:
        vals = sorted(v for f in frames for v in f.oi_change.values() if v > 0)
        if vals:
            q = lambda p: vals[min(len(vals) - 1, int(p / 100 * len(vals)))]  # noqa: E731
            console.print(f"[bold]ΔOI distribution[/bold] ({len(vals)} +moves, {lookback_min}m): "
                          f"p50 {q(50):.2%}  p90 {q(90):.2%}  p95 {q(95):.2%}  "
                          f"p99 {q(99):.2%}  max {vals[-1]:.2%}")
        console.print(f"\n[bold]sweep[/bold] (walk-forward, ~{cov:.0f}d):")
        console.print(f"{'spike':>6} {'z':>4} | {'IS_tr':>5} {'OOS_tr':>6} "
                      f"{'OOS_edge':>9} {'PASS':>5}")
        sweep_rows: list[SweepRow] = []
        for spike in (0.005, 0.01, 0.02, 0.03):
            for z in (1.0, 1.5, 2.0):
                params = {"oi_spike_min": spike, "z_enter": z, "lookback_min": lookback_min}
                r = _run(params)
                mark = "✅" if r.confirmed else "—"
                console.print(f"{spike:>6.3f} {z:>4.1f} | {r.in_sample.n_trades:>5} "
                              f"{r.out_of_sample.n_trades:>6} {_e(r.out_of_sample.edge_bps):>9} "
                              f"{mark:>5}")
                sweep_rows.append(SweepRow.from_result(coin_list, params, r))
        pseudo_spec = SweepSpec(
            agent="oi_crowding_reversal_v1",
            interval="5m",
            days=days,
            prefer=prefer,
            min_edge_bps=min_edge_bps,
            min_sharpe=min_sharpe,
            universes=[coin_list],
            grid={},
        )
        jpath, mpath = write_outputs(
            pseudo_spec, sweep_rows,
            json_dir=Path("data/sweeps"),
            md_dir=Path("research/results"),
            coverage_by_universe={",".join(coin_list): cov},
        )
        console.print(f"[dim]sweep results saved: {jpath} + {mpath}[/dim]")
        console.print("[dim]Pick by a PRIOR (distribution + a real overshoot), NOT the max "
                      "OOS-edge cell — the ~5d OOS is thin and selecting on it overfits. "
                      "The forward soak is the real arbiter.[/dim]")
        return

    res = _run(cfg)
    console.print(f"\n[bold]S8 OI-crowding backtest[/bold] (Binance OI proxy, ~{cov:.0f}d, "
                  f"lookback {lookback_min}m, params {cfg or 'defaults'})")
    console.print(res.summary())
    verdict = "[green]✅ PASS[/green]" if res.confirmed else "[red]❌ FAIL[/red]"
    console.print(f"{verdict}  IS edge {_e(res.in_sample.edge_bps)}bps / "
                  f"OOS edge {_e(res.out_of_sample.edge_bps)}bps / "
                  f"{res.out_of_sample.n_trades} OOS trades")
    console.print("[dim]Cross-venue proxy: Binance OI ≈ HL crowding. A PASS here is "
                  "evidence to keep soaking S8 forward, not a promotion — live still "
                  "requires a params-matched forward G0 on HL data.[/dim]")


@app.command()
def sweep(
    spec: Path,
    refresh: bool = False,
    json_dir: Path = Path("data/sweeps"),
    md_dir: Path = Path("research/results"),
):
    """Run a parameter/universe sweep through the G0 confirmation gate.

    Loads configs/sweeps/<name>.yaml, replays every combo over cached real
    history, and writes a ranked report to research/results/ (committed by the
    nightly host job so research sessions start from fresh evidence).

    With ``use_overrides: true`` (default) the sweep baseline is the deployed
    config (factory defaults + ``configs/agent_overrides.json``); grid params
    are layered on top. With ``use_accrued: true`` (default) the back-fetched
    HL candles are unioned with forward-accrued ``frame_samples`` so the
    evidence window can grow past HL's retention cap."""
    import time

    from ..backtest.data import (
        cached_or_fetch,
        frames_coverage_days,
        load_accrued_frames,
        merge_frames,
    )
    from ..engine.runner import AGENT_FACTORIES, _load_overrides
    from ..research.sweep import SweepSpec, run_sweep, write_outputs

    conn, s = _conn()
    sw = SweepSpec.load(spec)
    factory = AGENT_FACTORIES.get(sw.agent)
    if factory is None:
        console.print(f"[red]unknown agent {sw.agent}[/red]")
        raise typer.Exit(1)

    # Deployed baseline: factory defaults + agent_overrides.json (V3 provenance).
    base_config: dict = {}
    if sw.use_overrides:
        base_config.update(_load_overrides(s.configs_dir).get(sw.agent) or {})
        if base_config:
            console.print(f"[dim]baseline config from overrides: {base_config}[/dim]")

    frames_by_universe = {}
    for universe in sw.universes or [[]]:
        console.print(f"[dim]loading {sw.days}d {sw.interval} for {universe}…[/dim]")
        try:
            frames = cached_or_fetch(
                list(universe), interval=sw.interval, days=sw.days,
                base_url=s.hl_api_url, refresh=refresh)
        except Exception as e:  # noqa: BLE001
            console.print(f"[red]history load failed for {universe}: {e}[/red]")
            frames_by_universe[tuple(universe)] = []
            continue

        if sw.use_accrued:
            since_days = sw.accrued_since_days if sw.accrued_since_days is not None else sw.days
            since_ms = int(time.time() * 1000) - since_days * 86_400_000
            accrued = load_accrued_frames(
                conn, list(universe), sw.interval, since_ms=since_ms)
            if accrued:
                n_before = len(frames)
                frames = merge_frames(frames, accrued)
                console.print(f"[dim]+{len(frames) - n_before} forward-accrued bars[/dim]")
        frames_by_universe[tuple(universe)] = frames

    rows = run_sweep(sw, frames_by_universe, factory, base_config=base_config)
    # Per-universe coverage (empty/failed load → 0.0d). The report and this note
    # key off the LIMITING (shortest) span so a long-history universe can't mask
    # a short/failed one and overstate the evidence window.
    coverage_by_universe = {",".join(u): frames_coverage_days(f)
                            for u, f in frames_by_universe.items()}
    jpath, mpath = write_outputs(sw, rows, json_dir=json_dir, md_dir=md_dir,
                                 coverage_by_universe=coverage_by_universe)
    confirmed = sum(1 for r in rows if r.confirmed)
    limiting = min(coverage_by_universe.values(), default=0.0)
    if coverage_by_universe and limiting < sw.days * 0.9:
        console.print(f"[yellow]note:[/yellow] requested {sw.days}d of {sw.interval} but the "
                      f"limiting universe has only ~{limiting:.1f}d of candles at HL "
                      f"(retention cap) — this sweep's evidence window is ~{limiting:.0f}d.")
    console.print(f"[green]✓[/green] {len(rows)} combos, {confirmed} confirmed → {mpath}")


@app.command()
def ws(
    coins: str = "BTC,ETH,SOL,HYPE",
    snapshot: Path = Path("data/ws_snapshot.json"),
    seconds: float = 0.0,
    user: str = "",
):
    """Run the WebSocket market-data service: maintain live state, write a snapshot.

    Long-running (supervise via systemd). The tick reads the snapshot when fresh
    (set HLBOT_WS_SNAPSHOT) for sub-second mids, L2 depth, and live liquidations.
    With ``--user`` (or the configured HL address) also subscribes to
    ``userFills`` and writes fills to the DB in real time.
    seconds=0 runs forever.
    """
    from ..ingest.ws import run_ws

    _, s = _conn()
    coin_list = [c.strip() for c in coins.split(",") if c.strip()]
    user_address = user or s.hl_address
    if user_address:
        console.print(f"[green]ws[/green] subscribing {coin_list} + userFills({user_address}) → {snapshot} (every 1s)")
    else:
        console.print(f"[green]ws[/green] subscribing {coin_list} → {snapshot} (every 1s)")
    run_ws(coin_list, snapshot, base_url=s.hl_api_url, duration_s=(seconds or None),
           db_path=s.db_path, user_address=user_address or None)


@app.command()
def health(max_tick_age_s: int = 900, heartbeat: bool = True):
    """Assess bot health (tick/ingest freshness, equity, paused agents, 24h PnL).

    Pings HEALTHCHECK_URL when healthy (dead-man switch) and Telegram-alerts when
    not. Designed to run on a timer alongside the tick.
    """
    import os

    from ..ops.health import assess_health, ping_heartbeat

    conn, s = _conn()
    rep = assess_health(conn, max_tick_age_s=max_tick_age_s, data_dir=s.db_path.parent)
    console.print(rep.render())
    if heartbeat:
        ping_heartbeat(os.environ.get("HEALTHCHECK_URL"), ok=rep.ok)
    if not rep.ok:
        with contextlib.suppress(Exception):
            from ..exec.orders import telegram_alert
            telegram_alert(rep.render())
        raise typer.Exit(1)


@app.command()
def doctor(require_live: bool = False):
    """Preflight: env, DB, configs, API-wallet perms, HL reachability.

    Exit non-zero if any critical check fails. Run before enabling live and in CI.
    """
    from pathlib import Path as _Path

    from ..exec.orders import DEFAULT_API_WALLET_ENV
    from ..ops.doctor import render, run_doctor

    _, s = _conn()
    checks = run_doctor(
        hl_address=s.hl_address,
        api_url=s.hl_api_url,
        db_path=s.db_path,
        config_dir=_Path(CONFIG_DIR),
        api_wallet_path=DEFAULT_API_WALLET_ENV,
        require_live=require_live,
    )
    text, ok = render(checks)
    console.print(text)
    if not ok:
        raise typer.Exit(1)


@app.command()
def track_record(out: Path = Path("data/track_record")):
    """Export a public-grade track record (equity curve, Sharpe, DD, per-agent).

    Writes track_record.{json,md} for capital/AUM due diligence (Path C) and the
    go-live gates. Read-only on the DB.
    """
    from ..reports.track_record import export

    conn, _ = _conn()
    jp, mp, sp, hp = export(conn, out)
    console.print(mp.read_text())
    console.print(f"[green]✓[/green] wrote {jp}, {mp}, {sp}, {hp}")


@app.command()
def report(send: bool = False):
    """Build daily report; optionally send to Telegram."""
    conn, s = _conn()
    md = build_report(conn)
    console.print(md)
    if send:
        if not s.tg_bot_token or not s.tg_chat_id:
            console.print("[red]TG_BOT_TOKEN / TG_CHAT_ID not set[/red]")
            raise typer.Exit(1)
        send_telegram(md, s.tg_bot_token, s.tg_chat_id)
        console.print("[green]✓ sent[/green]")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
