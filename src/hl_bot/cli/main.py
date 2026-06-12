"""CLI entrypoint: `hlbot ...`"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import subprocess
import time
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from ..agents.basis import BasisAgent
from ..agents.breakout import BreakoutAgent
from ..agents.femr import FemrAgent
from ..agents.funding_carry import FundingCarryAgent
from ..agents.liq_cascade import LiqCascadeAgent
from ..agents.twap_mr import TwapMrAgent
from ..agents.twap_mr_regime import TwapMrRegimeAgent
from ..agents.veto import VetoAgent
from ..agents.xfund_carry import XFundCarryAgent
from ..agents.xmom import XMomAgent
from ..config import CONFIG_DIR, Settings
from ..db.schema import init_db
from ..ingest.hyperliquid import ingest_fills, ingest_funding, snapshot_equity
from ..reports.daily import build as build_report
from ..reports.daily import send_telegram
from ..research.strategy_health import (
    agent_health,
    build_proposal_document,
    propose_overrides,
)
from ..risk.scaling import compute_notional_cap
from ..scoring.metrics import score_all
from ..scoring.paper import (
    list_paper_agents,
    mark_paper_positions,
    paper_open_positions,
    score_paper_all,
)
from ..scoring.positions import rebuild_positions
from ..supervisor.loop import supervise

app = typer.Typer(no_args_is_help=True, add_completion=False)
console = Console()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def _conn():
    s = Settings.from_env()
    return init_db(s.db_path), s


def _fetch_paper_funding(conn, s) -> dict[str, list[dict[str, Any]]]:
    """Funding-rate history covering every paper hold, for modeled accrual.

    One paginated API call per paper coin; a per-coin failure warns and
    degrades that coin to funding=0 — never crashes the readout. Empty dict
    when there is no paper book (zero network calls).
    """
    from ..backtest.data import fetch_funding_history
    from ..scoring.paper import paper_funding_spans

    funding_by_coin: dict[str, list[dict[str, Any]]] = {}
    for coin, (t0, t1) in sorted(paper_funding_spans(conn).items()):
        try:
            funding_by_coin[coin] = fetch_funding_history(
                coin, t0, t1, base_url=s.hl_api_url)
        except Exception as e:  # noqa: BLE001
            console.print(
                f"[yellow]warn:[/yellow] funding history fetch failed for "
                f"{coin} ({e}); its paper funding_pnl counts as 0"
            )
    return funding_by_coin


def _fetch_mids(s) -> dict[str, float]:
    """Current mids (one allMids call) for marking open paper positions.

    Any failure warns and returns {} — the readout degrades to unmarked
    positions, never crashes.
    """
    import httpx

    try:
        raw = httpx.post(
            s.hl_api_url + "/info", json={"type": "allMids"}, timeout=15
        ).json() or {}
    except Exception as e:  # noqa: BLE001
        console.print(
            f"[yellow]warn:[/yellow] allMids fetch failed ({e}); "
            "open paper positions not marked"
        )
        return {}
    mids: dict[str, float] = {}
    for k, v in raw.items():
        with contextlib.suppress(TypeError, ValueError):
            mids[k] = float(v)
    return mids


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
    snapshot_equity(conn, s.hl_address, s.hl_api_url)
    n_pos = rebuild_positions(conn)
    console.print(
        f"[green]✓[/green] fills:{n_fills} funding:{n_fund} positions:{n_pos} "
        "+1 equity snapshot"
    )


@app.command()
def score(
    paper: bool = typer.Option(
        False, "--paper",
        help="Score the paper decision book (modeled taker costs + modeled "
             "funding accrual) instead of exchange fills.",
    ),
    funding: bool = typer.Option(
        True, "--funding/--no-funding",
        help="--paper only: model funding accrual over paper holds from HL "
             "funding-rate history (network; a per-coin fetch failure degrades "
             "that coin to funding=0 with a warning).",
    ),
    mark: bool = typer.Option(
        True, "--mark/--no-mark",
        help="--paper only: mark open paper positions at the current mid "
             "(one allMids call; a fetch failure degrades to unmarked). "
             "Marks are shown beside the cards, never folded into them.",
    ),
):
    """Print per-agent scorecards (--paper: replayed paper book, B-PAPER3)."""
    conn, s = _conn()
    if paper:
        funding_by_coin = _fetch_paper_funding(conn, s) if funding else {}
        cards = score_paper_all(conn, funding_by_coin=funding_by_coin or None)
        fund_note = "modeled funding" if funding_by_coin else "funding=0"
        title = f"Paper scorecards (decision-book replay · modeled taker costs · {fund_note})"
    else:
        cards = score_all(conn)
        title = "Scorecards"
    table = Table(title=title)
    for col in ("agent", "window", "n_trades", "net_pnl", "funding", "win_rate", "sharpe", "max_dd", "edge_bps"):
        table.add_column(col)
    for c in cards:
        table.add_row(
            c.agent, c.window, str(c.n_trades),
            f"{c.net_pnl:+.2f}",
            f"{c.funding_pnl:+.2f}",
            f"{c.win_rate*100:.0f}%",
            "—" if c.sharpe is None else f"{c.sharpe:+.2f}",
            "—" if c.max_drawdown is None else f"{c.max_drawdown*100:+.1f}%",
            "—" if c.edge_bps is None else f"{c.edge_bps:+.1f}",
        )
    console.print(table)
    if paper:
        now_ms = int(time.time() * 1000)
        open_rows = [
            (a, p) for a in list_paper_agents(conn)
            for p in paper_open_positions(conn, a)
        ]
        if open_rows:
            mids = _fetch_mids(s) if mark else {}
            marked = [
                (a, mp) for a, p in open_rows
                for mp in mark_paper_positions([p], mids)
            ]
            title = (
                "Open paper positions (marked at current mid, modeled exit costs)"
                if mids else "Open paper positions (not marked to market)"
            )
            pos_table = Table(title=title)
            for col in ("agent", "coin", "side", "sz", "entry_px", "mark_px",
                        "upnl", "age_h"):
                pos_table.add_column(col)
            upnl_by_agent: dict[str, float] = {}
            for a, p in marked:
                pos_table.add_row(
                    a, p.coin, "long" if p.side == "B" else "short",
                    f"{p.sz:.5f}", f"{p.entry_px:.4f}",
                    "—" if p.mark_px is None else f"{p.mark_px:.4f}",
                    "—" if p.upnl is None else f"{p.upnl:+.2f}",
                    f"{(now_ms - p.entry_ts_ms) / 3_600_000:.1f}",
                )
                if p.upnl is not None:
                    upnl_by_agent[a] = upnl_by_agent.get(a, 0.0) + p.upnl
            console.print(pos_table)
            if upnl_by_agent:
                console.print(
                    "Open paper uPnL (if flattened now; NOT in the realized "
                    "cards above): "
                    + ", ".join(f"{a} {v:+.2f}"
                                for a, v in sorted(upnl_by_agent.items()))
                )


@app.command()
def positions(rebuild: bool = True):
    """Show per-agent position attribution (replayed from fills)."""
    conn, _ = _conn()
    if rebuild:
        rebuild_positions(conn)
    rows = conn.execute(
        """SELECT agent, coin, net_sz, avg_entry_px, realized_pnl, fees_paid
           FROM positions ORDER BY agent, coin"""
    ).fetchall()
    table = Table(title="Positions (per-agent, from fills)")
    for col in ("agent", "coin", "net_sz", "avg_entry", "realized_pnl", "fees"):
        table.add_column(col)
    for r in rows:
        table.add_row(
            r["agent"], r["coin"], f"{r['net_sz']:+.4f}",
            f"{r['avg_entry_px']:.4f}" if r["net_sz"] else "—",
            f"{r['realized_pnl']:+.2f}", f"{r['fees_paid']:.2f}",
        )
    console.print(table)


@app.command()
def supervisor(
    configs: Path = CONFIG_DIR,
    paper_funding: bool = typer.Option(
        True, "--paper-funding/--no-paper-funding",
        help="Model funding accrual over paper holds from HL funding-rate "
             "history so paper-mode agents' goal evaluation sees their funding "
             "revenue (network; per-coin failure degrades to funding=0; zero "
             "calls when there is no paper book).",
    ),
):
    """Evaluate goals/guardrails for every agent config in ./configs.

    Paper-mode agents with a paper book are scored from the paper-book replay
    (modeled costs + funding). Pause/demote guardrails fire on paper evidence;
    promotion from paper cards is informational only ("promotion-ready",
    human-gated) — the supervisor never flips an agent live on modeled fills.
    """
    conn, s = _conn()
    funding_by_coin = _fetch_paper_funding(conn, s) if paper_funding else {}
    actions = supervise(conn, configs, paper_funding_by_coin=funding_by_coin or None)
    console.print(json.dumps(actions, indent=2) if actions else "[dim]no actions taken[/dim]")


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
def veto(
    lookback_days: int = 30,
    min_trades: int = 20,
    threshold_bps: float = -5.0,
):
    """Per-coin account-edge report: which coins the veto advisory would block.

    Read-only replacement for the retired `hlbot tick` (B12j): replays the
    advisory VetoAgent against the fills table and prints its verdicts WITHOUT
    logging decisions. The old `tick` wrapper logged funding_arb_v1 paper
    `place` rows, which since B-PAPER would contaminate the real paper book
    (`hlbot score --paper`, track-record paper section). Paper ticks of the
    full roster are `hlbot femr_tick` (the default, no orders placed).
    """
    from ..agents.base import MarketView

    conn, _s = _conn()
    agent = VetoAgent(config={
        "lookback_days": lookback_days,
        "min_trades": min_trades,
        "veto_threshold_bps": threshold_bps,
    }, conn=conn)
    decisions = agent.decide(MarketView(ts_ms=int(time.time() * 1000), mids={}))
    if not decisions:
        console.print(f"no fills in the last {lookback_days}d — nothing to judge")
        return
    table = Table(title=f"veto advisory — {lookback_days}d account edge per coin")
    table.add_column("coin")
    table.add_column("verdict")
    table.add_column("edge bps", justify="right")
    table.add_column("trades", justify="right")
    table.add_column("net $", justify="right")
    rows = sorted(decisions, key=lambda d: (d.market_snapshot or {}).get("edge_bps", 0.0))
    for d in rows:
        m = d.market_snapshot or {}
        verdict = str(m.get("verdict", ""))
        style = {"veto": "red", "allow": "green"}.get(verdict, "dim")
        table.add_row(
            d.coin or "",
            f"[{style}]{verdict}[/{style}]",
            f"{float(m.get('edge_bps', 0.0)):+.1f}",
            str(m.get("n_trades", 0)),
            f"{float(m.get('net_pnl', 0.0)):+.2f}",
        )
    console.print(table)


@app.command("femr_tick")
def femr_tick(live: bool = False, execution: str = "taker", vwap_window: int = 0):
    """Run FEMR (Funding Extremes Mean Reversion) one tick.

    paper (default): log decisions only, no orders placed.
    live: place real orders on MAIN account, gated by guardrails.
          Bot only touches positions it itself opened (cloid-tagged).
    vwap_window: rolling VWAP/σ window in 1m bars for the mean-reversion
          agents (0 = HLBOT_VWAP_WINDOW env, else 60 — the historical config).
    """
    from ..agents.runtime import (
        apply_allocator_caps,
        build_roster,
        build_tick_view,
        classify_position_ownership,
        exit_only_live_agents,
        fetch_account_state,
        filter_live_agents,
        gather_decisions,
        load_agent_overrides,
        positions_from_clearinghouse,
        reconcile_agents,
        record_tick_heartbeat,
        synthesize_paper_positions,
    )
    from ..exec.orders import (
        HL_TRADER_ADDRESS,
        GuardrailConfig,
        bot_owned_coins,
        build_exchange,
        check_guardrails,
        dynamic_daily_loss_limit,
        telegram_alert,
    )

    conn, s = _conn()

    account = fetch_account_state(s.hl_api_url, HL_TRADER_ADDRESS)
    acct_val = account.account_value
    portfolio_value = account.portfolio_value
    withdrawable = account.withdrawable
    risk_cap = compute_notional_cap(conn, live_portfolio_value=portfolio_value)
    pv_label = "—" if risk_cap.portfolio_value is None else f"${risk_cap.portfolio_value:.2f}"
    console.print(
        "[bold]risk cap[/bold]: "
        f"bot-open <= ${risk_cap.max_total_notional:.0f}; "
        f"per-position <= ${risk_cap.max_per_position_notional:.0f} "
        f"({risk_cap.multiplier:g}x / {risk_cap.per_position_multiplier:g}x live unified portfolio {pv_label}; "
        f"perp ${acct_val:.2f} + spot USDC ${account.spot_usdc:.2f}; "
        f"ceiling={'none' if risk_cap.ceiling_notional is None else f'${risk_cap.ceiling_notional:.0f}'}; "
        f"source={risk_cap.source})"
    )

    # Instantiate the full agent roster (shared, tested construction — defaults
    # + auto-tuner overrides live in agents.runtime). In paper mode, evaluate
    # everything. In live mode, only agents explicitly enabled and promoted to
    # live_small/live in agent_state are allowed into the execution roster.
    roster_all = build_roster(conn, load_agent_overrides())
    agents = roster_all
    exit_only: set[str] = set()
    if live:
        agents, skipped_live = filter_live_agents(conn, agents)
        if skipped_live:
            console.print(
                "[yellow]live roster skipped[/yellow]: "
                + ", ".join(f"{name}({why})" for name, why in skipped_live.items())
            )
        # Demoted/paused agents whose live book still holds positions or
        # resting quotes stay in the tick EXIT-ONLY: their exits, maker-fill
        # reconciliation, and stale-quote cancels keep running; their entries
        # are dropped in execute_decisions. Without this, a supervisor demote
        # orphaned its own open inventory (no exit ladder, no reconcile, no
        # guardrail pass — the early return below skipped them all).
        exit_managers = exit_only_live_agents(
            conn, roster_all, {a.name for a in agents})
        if exit_managers:
            exit_only = {a.name for a in exit_managers}
            console.print(
                "[yellow]exit-only (demoted, managing open live inventory)[/yellow]: "
                + ", ".join(sorted(exit_only))
            )
            agents = agents + exit_managers
        if not agents:
            console.print("[yellow]LIVE MODE but no agent_state rows are enabled in live_small/live; no orders possible[/yellow]")
            record_tick_heartbeat(conn, mode="live", agents=0, decisions=0)
            return

    # Allocator: rebalance per-agent caps from rolling 7d performance.
    # The approved live risk rule is dynamic but layered:
    #   - aggregate bot-open notional can reach 5x live unified portfolio value
    #   - any SINGLE agent is limited to 1x portfolio value (max_alloc), so one
    #     agent can never consume the whole 5x portfolio cap.
    # resolve_agent_caps applies the final rule: explicit (sub-legacy) configured
    # caps win, legacy broad $1000 ceilings are replaced by the dynamic 1x cap,
    # and configured per-trade sizes are preserved (never raised).
    caps = apply_allocator_caps(conn, agents, risk_cap)
    allocs = caps.allocs
    effective_caps = caps.effective_caps
    effective_order_caps = caps.effective_order_caps
    console.print("[bold]allocator caps[/bold]: " +
                  ", ".join(
                      f"{n}=total ${effective_caps.get(n, v):.0f}/pos ${effective_order_caps.get(n, 0):.0f}"
                      for n, v in allocs.items()
                  ))

    # One tested view pipeline (paper and live ticks decide on it): REST fetch,
    # VWAP/σ + spot + 15m-feed enrichment, and the (opt-in, HLBOT_WS_SNAPSHOT)
    # fresh-WS overlay that carries the real liquidations feed.
    tick_view = build_tick_view(s.hl_api_url, agents, vwap_window=vwap_window)
    view = tick_view.view
    w = tick_view.vwap_window
    bars_15m = tick_view.bars_15m
    bars_1h = tick_view.bars_1h
    if tick_view.ws and tick_view.ws.applied:
        console.print(f"[dim]ws snapshot overlaid: {tick_view.ws.n_mids} mids, "
                      f"{tick_view.ws.n_liqs} liqs[/dim]")

    # Build position list from HL truth (shared, tested parse).
    all_positions = positions_from_clearinghouse(account.clearinghouse)

    # RECONCILE first — clear stale DB ownership for each agent independently.
    # Live only: reconciliation compares the LIVE book to exchange truth; paper
    # positions have no exchange counterpart, and a paper tick shouldn't write
    # live-book rows.
    if live:
        reconciled_all = reconcile_agents(conn, all_positions, [a.name for a in agents])
        if reconciled_all:
            console.print(f"[yellow]reconciled stale ownership: {reconciled_all}[/yellow]")

    # FEMR sees only its own owned coins (adopts handled internally by name match).
    # Paper ticks synthesize that view from the paper-book replay instead — a
    # paper position has no exchange counterpart, so without this femr's exit
    # logic (which only evaluates `live_positions`) could never close one; it
    # held a capacity slot forever (B-PAPER2).
    if live:
        owned_femr = bot_owned_coins(conn, agent="femr_v1", paper=False)
        bot_positions = [p for p in all_positions if p["coin"] in owned_femr]
    else:
        bot_positions = synthesize_paper_positions(conn, "femr_v1", view.mids)
    view.extra["live_positions"] = bot_positions

    # Partition live positions into bot-owned (any roster agent) vs manual via the
    # shared, tested classification (agents.runtime), against this tick's book.
    ownership = classify_position_ownership(
        conn, all_positions, [a.name for a in agents], paper=not live)
    owned_all = ownership.owned_all
    manual_coins = ownership.manual_coins
    feed_15m = (
        f"closes15m: {len(view.extra.get('closes_15m', {}))} coins (≤{bars_15m} bars) · "
        if bars_15m else ""
    )
    feed_1h = (
        f"closes1h: {len(view.extra.get('closes_1h', {}))} coins (≤{bars_1h} bars) · "
        if bars_1h else ""
    )
    console.print(
        f"[dim]market: {len(view.mids)} coins, {len(view.funding)} funding · "
        f"candles: {len(view.extra.get('candles_1h', {}))} (vwap w={w}) · "
        f"{feed_15m}"
        f"{feed_1h}"
        f"spot: {sorted(view.extra.get('spot_mids', {}).keys())} · "
        f"liqs: {len(view.extra.get('liquidations', []))} · "
        f"acct ${acct_val:.2f}, free ${withdrawable:.2f} · "
        f"bot-owned: {sorted(owned_all) or '∅'} · manual: {manual_coins or '∅'}[/dim]"
    )

    # Gather decisions through the shared, tested path (agents.runtime). In live
    # mode, `place`/`flatten` are logged ONLY after exchange acceptance in the
    # execution loop below (defer_exec_logging) — otherwise the cooldown check
    # would see our own intent rows and block subsequent ticks forever. In paper
    # mode there is no execution loop, so exec decisions are logged HERE as
    # is_paper=1 rows — this is what makes the paper book exist at all (before,
    # paper ticks logged nothing and paper agents could never track their own
    # positions). The book-aware replay filters keep those rows invisible to the
    # live path. A crashing agent is isolated so it can't abort risk-reducing
    # flattens from healthy agents.
    all_decisions = gather_decisions(
        conn, agents, view,
        is_paper=not live,
        defer_exec_logging=live,
        log_holds=False,
        honor_enabled=False,
    )

    console.print(f"[green]✓[/green] {len(all_decisions)} decisions (live={live})")
    for d in all_decisions:
        tag = "" if d.action != "hold" else "[dim]"
        end = "" if d.action != "hold" else "[/dim]"
        console.print(f"  {tag}{d.agent} {d.action} {d.coin or ''} :: {d.reasoning}{end}")

    if not live:
        console.print("[yellow]PAPER MODE[/yellow]")
        record_tick_heartbeat(
            conn, mode="paper", agents=len(agents), decisions=len(all_decisions))
        return

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
        # Judge the tick-start snapshot the risk caps were sized from — no
        # second user_state/spot fetch that could diverge mid-tick.
        account=account,
    )
    if not ok:
        console.print(f"[red]HALT new entries[/red]: {why}")
        console.print("[yellow]Flatten/close decisions are still allowed for risk reduction.[/yellow]")
    else:
        console.print(f"[green]guardrails[/green]: {why}")

    # Maker execution prep: refresh fills, promote filled resting orders to owned,
    # cancel stale quotes. Entries below then rest post-only instead of crossing.
    if execution == "maker":
        from ..exec.maker import (
            log_cancel,
            reconcile_maker_fills,
            stale_working,
            working_orders,
        )
        from ..exec.orders import cancel_order
        from ..ingest.hyperliquid import ingest_fills, ingest_ws_user_fills
        ingest_fills(conn, s.hl_address, s.hl_api_url)  # so cloid fills are visible
        # Also fold in any fills the WS snapshot already captured this tick — a
        # maker quote that filled seconds ago is then reconciled NOW, not next
        # REST poll (deduped by (hash,tid)). No-op when the WS feed isn't running.
        n_ws = ingest_ws_user_fills(conn, view.extra.get("user_fills", []))
        if n_ws:
            console.print(f"[dim]ws userFills: +{n_ws} new[/dim]")
        for a in agents:
            working = working_orders(conn, a.name)
            got = reconcile_maker_fills(conn, a.name, working)
            for o in stale_working(working):
                if o["coin"] in got or o.get("oid") is None:
                    continue
                cancel_order(exchange, o["coin"], o["oid"])
                log_cancel(conn, a.name, o)
            if got:
                console.print(f"[green]maker fills[/green] {a.name}: {got}")
        conn.commit()

    # Execute — single tested live order-placement loop (agents.runtime).
    from ..agents.runtime import execute_decisions
    agent_names = {a.name for a in agents}
    for ev in execute_decisions(
        conn, exchange, view, all_decisions,
        agent_names=agent_names, guardrails_ok=ok, execution=execution,
        exit_only=exit_only,
    ):
        console.print(ev.message)

    record_tick_heartbeat(
        conn, mode="live", agents=len(agents), decisions=len(all_decisions))


def parse_agent_config(config: str) -> dict[str, Any]:
    """Parse a ``--config`` JSON override string into a config dict for an agent.

    Empty/whitespace → ``{}`` (use the agent's built-in defaults). The string
    must decode to a JSON *object*; an array, scalar, or malformed JSON is a hard
    error rather than a silent fall-back to defaults, so a typo in a parameter
    sweep can't quietly run the default config and mislabel the result.
    """
    if not config or not config.strip():
        return {}
    try:
        obj = json.loads(config)
    except json.JSONDecodeError as e:
        raise ValueError(f"--config is not valid JSON: {e}") from e
    if not isinstance(obj, dict):
        raise ValueError(f"--config must be a JSON object, got {type(obj).__name__}")
    return obj


def _backtest_factories(cfg: dict[str, Any]):
    """Agent factories for backtest/confirm, with ``cfg`` overrides applied.

    Only one agent is backtested per invocation, so passing the same override
    dict to every factory is harmless — each agent reads only the keys it knows
    and ignores the rest.
    """
    return {
        "twap_mr_v1": lambda conn: TwapMrAgent(config=cfg, conn=conn),
        "twap_mr_regime_v1": lambda conn: TwapMrRegimeAgent(config=cfg, conn=conn),
        "femr_v1": lambda conn: FemrAgent(config=cfg, conn=conn),
        "funding_carry_v1": lambda conn: FundingCarryAgent(config=cfg, conn=conn),
        "xfund_carry_v1": lambda conn: XFundCarryAgent(config=cfg, conn=conn),
        "liq_cascade_v1": lambda conn: LiqCascadeAgent(config=cfg, conn=conn),
        "basis_v1": lambda conn: BasisAgent(config=cfg, conn=conn),
        "breakout_v1": lambda conn: BreakoutAgent(config=cfg, conn=conn),
        "xmom_v1": lambda conn: XMomAgent(config=cfg, conn=conn),
    }


@app.command()
def backtest_fetch(
    coins: str = "BTC,ETH,SOL",
    interval: str = "1h",
    days: int = 30,
    refresh: bool = False,
    vwap_window: int = 60,
):
    """Fetch + cache HL candle/funding history for offline, reproducible backtests.

    Writes a gzipped frame dataset under data/backtest_cache/ (gitignored).
    Run this once where HL is reachable; then `hlbot backtest` runs without network.
    --vwap-window sets the rolling VWAP/sigma window in BARS (live fades a 60×1m
    VWAP, so 15m bars want 4, 5m bars want 12); non-default windows get their own
    cache file because frames bake the window in.
    """
    from ..backtest.data import cached_or_fetch, default_cache_path

    _, s = _conn()
    coin_list = [c.strip() for c in coins.split(",") if c.strip()]
    path = default_cache_path(coin_list, interval, days, vwap_window)
    console.print(f"[dim]fetching {days}d {interval} for {coin_list}…[/dim]")
    try:
        frames = cached_or_fetch(coin_list, interval=interval, days=days,
                                 base_url=s.hl_api_url, refresh=refresh,
                                 vwap_window=vwap_window)
    except Exception as e:  # noqa: BLE001
        console.print(f"[red]fetch failed: {e}[/red]")
        raise typer.Exit(2) from e
    console.print(f"[green]✓[/green] cached {len(frames)} frames → {path}")


@app.command()
def harvest_candles(
    coins: str = "ADA,AVAX,BTC,DOGE,ETH,HYPE,LINK,SOL,TRX,ZEC",
    intervals: str = "1m,5m,15m,1h",
    breadth_coins: str = "CRV,ENA,LIT,NEAR,SUI,TON,WLD,XMR,XPL,XRP",
    breadth_intervals: str = "15m,1h",
    if_stale_minutes: float = 0.0,
    sync_peer: str = "",
):
    """Append the latest fine-interval candles to the rolling local store (B-HIST).

    HL retains only ~5000 candles per interval (≈3.5d of 1m, ≈17d of 5m), so
    live-cadence backtest history must be harvested before it expires. Run
    periodically (deploy/systemd/hlbot-harvest.timer does it hourly): each run
    refetches from the last stored bar and appends, deduped by open time, into
    data/candle_store/{coin}_{interval}.json.gz (gitignored). Exits non-zero if
    any pair failed so the systemd run shows up red; successful pairs are saved
    regardless.

    --breadth-coins is the out-of-universe validation set (B-EDGE2d), harvested
    only at --breadth-intervals (default 15m) so breadth re-tests of the
    momentum family outgrow the API's rolling retention without doubling the
    1m/5m load. --breadth-coins "" disables it.

    --if-stale-minutes N skips the network entirely when every pair's last
    stored bar lags fresh by ≤N minutes (lag beyond one full interval, so the
    threshold works across 1m and 1h pairs). Lets overlapping backstops — the
    ralph loop's per-iteration step, an agent's in-session top-up, the systemd
    timer — all run unconditionally without re-fetching a store another
    mechanism just filled. 0 (default) always harvests.

    --sync-peer DIR union-merges this clone's store with another clone's
    store dir after the harvest (and even when --if-stale-minutes skipped
    it — sync is local and free). Both stores end up holding every bar
    either harvester captured, so one harvester dying no longer gaps the
    irreplaceable 1m sample (both have died once: B-DEPLOY-EXEC, Iter 78).
    Best-effort redundancy: a missing peer clone is skipped, sync failures
    never change the exit code (only harvest failures turn the timer red).
    """
    from ..backtest.store import harvest, harvest_pairs, worst_store_lag

    _, s = _conn()
    coin_list = [c.strip() for c in coins.split(",") if c.strip()]
    interval_list = [i.strip() for i in intervals.split(",") if i.strip()]
    extra = [
        (c.strip(), i.strip())
        for c in breadth_coins.split(",") if c.strip()
        for i in breadth_intervals.split(",") if i.strip()
    ]
    if if_stale_minutes > 0:
        label, lag = worst_store_lag(harvest_pairs(coin_list, interval_list, extra))
        if lag is not None and lag <= if_stale_minutes:
            console.print(
                f"store fresh (worst lag {lag:.1f}m at {label} ≤ "
                f"{if_stale_minutes:g}m) — skipping harvest"
            )
            if sync_peer:
                _sync_peer_store(sync_peer)
            return
    results = harvest(coin_list, interval_list, extra_pairs=extra, base_url=s.hl_api_url)
    table = Table(title="Candle harvest")
    for col in ("coin", "interval", "added", "total", "span", "status"):
        table.add_column(col)
    for r in results:
        table.add_row(
            r.coin, r.interval, str(r.added), str(r.total),
            "—" if r.span_days is None else f"{r.span_days:.1f}d",
            f"[red]{r.error}[/red]" if r.error else "[green]ok[/green]",
        )
    console.print(table)
    if sync_peer:
        _sync_peer_store(sync_peer)
    if any(r.error for r in results):
        raise typer.Exit(2)


def _sync_peer_store(peer: str) -> None:
    """Best-effort union sync with another clone's candle store; never raises.

    Skips quietly when the peer clone isn't present (its data/ dir missing) so
    the flag is safe to bake into units/scripts that may run on other hosts.
    """
    from ..backtest.store import store_dir, sync_stores

    peer_dir = Path(peer)
    if not peer_dir.parent.is_dir():
        console.print(f"[dim]sync peer {peer_dir} absent — skipped[/dim]")
        return
    try:
        results = sync_stores(store_dir(), peer_dir)
    except Exception as e:  # noqa: BLE001 — redundancy must not block the harvest
        console.print(f"[yellow]store sync failed: {e}[/yellow]")
        return
    pulled = sum(r.added_a for r in results)
    pushed = sum(r.added_b for r in results)
    errs = [r for r in results if r.error]
    line = f"store sync ↔ {peer_dir}: +{pulled} bars here, +{pushed} bars peer"
    if errs:
        line += f" [yellow]({len(errs)} file(s) errored, e.g. {errs[0].name}: {errs[0].error})[/yellow]"
    console.print(line)


def _load_backtest_frames(
    coin_list: list[str],
    *,
    source: str,
    interval: str,
    days: float,
    cache: bool,
    vwap_window: int,
    api_url: str,
    with_funding: bool = True,
    store_root: str | None = None,
):
    """Load frames for backtest/confirm per --source; prints status, exits on failure.

    ``api`` fetches/caches via the HL info endpoints (retention-capped at ~5000
    bars per interval); ``store`` reads the harvested rolling store, which is
    how live-cadence runs outgrow that cap (B-HIST2). Store coverage (incl.
    gaps from harvester outages) is printed so a holey sample can't pass as a
    full one.
    """
    if source == "store":
        from ..backtest.store import frames_from_store

        console.print(f"[dim]loading {interval} candles for {coin_list} from store…[/dim]")
        try:
            frames, coverage = frames_from_store(
                coin_list, interval=interval, days=days, with_funding=with_funding,
                base_url=api_url, vwap_window=vwap_window, root=store_root)
        except Exception as e:  # noqa: BLE001
            console.print(f"[red]failed to load from store: {e}[/red]")
            raise typer.Exit(2) from e
        for c in coverage:
            style = "red" if c.missing_pct > 1.0 or not c.bars else "dim"
            span = "—" if c.span_days is None else f"{c.span_days:.1f}d"
            console.print(f"[{style}]store {c.coin}_{c.interval}: {c.bars} bars "
                          f"{span}, {c.missing} missing ({c.missing_pct:.1f}%)[/{style}]")
    elif source == "api":
        from ..backtest.data import cached_or_fetch, load_frames

        console.print(f"[dim]loading {days}d of {interval} candles for {coin_list} "
                      f"({'cache' if cache else 'network'})…[/dim]")
        try:
            frames = (cached_or_fetch(coin_list, interval=interval, days=days,
                                      base_url=api_url, vwap_window=vwap_window)
                      if cache else
                      load_frames(coin_list, interval=interval, days=days,
                                  base_url=api_url, vwap_window=vwap_window))
        except Exception as e:  # noqa: BLE001
            console.print(f"[red]failed to load history: {e}[/red]")
            raise typer.Exit(2) from e
    else:
        console.print(f"[red]unknown source {source!r}; choose api or store[/red]")
        raise typer.Exit(1)
    if not frames:
        console.print("[red]no frames built (insufficient history)[/red]")
        raise typer.Exit(2)
    console.print(f"[dim]{len(frames)} frames[/dim]")
    return frames


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
    config: str = "",
    vwap_window: int = 60,
    source: str = "api",
    funding: bool = True,
    maker_fill: str = "optimistic",
    prop_profile: str = "",
):
    """Replay an agent over real Hyperliquid history with an explicit cost model.

    Fetches candle + funding history (network), drives the agent's real
    ``decide()``, simulates fills (taker by default), and scores the run with the
    same code used live. With ``--compare`` (default) it runs taker AND maker so
    you can see how much of the edge the spread is eating — the central question
    for this book. Places no orders; purely offline analysis.

    --source store replays the harvested rolling candle store instead of the
    retention-capped API (B-HIST2): --days trims to the most recent N days,
    --days 0 uses everything stored, and --no-funding skips the funding fetch
    (price-only economics) when the network is down.

    --maker-fill resting replays the live maker lifecycle honestly (entries
    rest and only fill if price comes to them — judged on the bar's intrabar
    low/high when available, else the close mid; stale quotes cancel, exits
    pay taker) instead of the default optimistic instant-fill-at-mid. The
    truth for a live maker book lies between the two. --maker-fill
    resting-close forces close-only fill detection (the pre-B-FILL2 extra-
    pessimistic bound) to A/B the wick detection itself.

    --prop-profile '{json}' additionally screens each run's per-bar equity
    curve against prop-eval rules (B-PROP2, docs/PROP_EVAL.md) — keys as in
    risk.prop.EvalProfile (max_daily_loss_pct, daily_loss_base,
    max_drawdown_pct, drawdown_mode, profit_target_pct, min_trading_days,
    day_boundary_utc_hour; '{}' = placeholder defaults). The screen's start
    balance is --starting-capital, so size it like the eval account.
    Informational only: a FAIL prints but does not fail the command.
    """
    from ..backtest.engine import Backtester, CostModel
    from ..risk.prop import fill_trading_days, parse_eval_profile, simulate_eval

    _, s = _conn()
    coin_list = [c.strip() for c in coins.split(",") if c.strip()]

    try:
        cfg = parse_agent_config(config)
    except ValueError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1) from e
    factories = _backtest_factories(cfg)
    if agent not in factories:
        console.print(f"[red]unknown agent {agent}; choose from {list(factories)}[/red]")
        raise typer.Exit(1)
    if maker_fill not in ("optimistic", "resting", "resting-close"):
        console.print(f"[red]unknown maker-fill {maker_fill!r}; choose optimistic, resting or resting-close[/red]")
        raise typer.Exit(1)
    prop = None
    if prop_profile.strip():
        try:
            prop = parse_eval_profile(prop_profile, start_balance=starting_capital)
        except ValueError as e:
            console.print(f"[red]{e}[/red]")
            raise typer.Exit(1) from e

    frames = _load_backtest_frames(
        coin_list, source=source, interval=interval, days=days, cache=cache,
        vwap_window=vwap_window, api_url=s.hl_api_url, with_funding=funding)

    per_year = {"1m": 525_600, "5m": 105_120, "15m": 35_040,
                "1h": 8_760, "4h": 2_190, "1d": 365}.get(interval, 8_760)

    modes = [False, True] if compare else [maker]
    dur = (f"{(frames[-1].ts_ms - frames[0].ts_ms) / 86_400_000:.1f}d:store"
           if source == "store" else f"{days}d")
    title = f"Backtest {agent} ({dur} {interval}"
    title += f" w={vwap_window})" if vwap_window != 60 else ")"
    if cfg:
        title += f" cfg={json.dumps(cfg, separators=(',', ':'))}"
    table = Table(title=title)
    for col in ("exec", "net_pnl", "edge_bps", "trades", "win", "sharpe", "maxDD"):
        table.add_column(col)
    fill_notes: list[str] = []
    prop_lines: list[str] = []
    for is_maker in modes:
        from ..db.schema import init_db as _init
        conn = _init(":memory:")
        cost = CostModel(maker=is_maker, maker_fill=maker_fill)
        bt = Backtester(cost, conn=conn, starting_capital=starting_capital)
        res = bt.run(factories[agent](conn), frames)
        # recompute curve stats at the right cadence
        from ..backtest.engine import _curve_stats
        sh, dd, _ = _curve_stats(res.equity_curve, periods_per_year=per_year)
        sc = res.scorecard
        table.add_row(
            cost.exec_label,
            f"{sc.net_pnl:+.2f}",
            "—" if sc.edge_bps is None else f"{sc.edge_bps:+.1f}",
            str(sc.n_trades),
            f"{sc.win_rate*100:.0f}%",
            "—" if sh is None else f"{sh:+.2f}",
            "—" if dd is None else f"{dd*100:+.1f}%",
        )
        st = res.maker_fill_stats
        if st and st.get("rested"):
            fill_notes.append(
                f"{cost.exec_label} quotes: {st['rested']} rested, {st['filled']} filled "
                f"({st['filled']/st['rested']*100:.0f}%), {st['expired']} expired/cancelled"
                + (f", {st['filled_wick']} filled on intrabar wick only"
                   if st.get("filled_wick") else "")
            )
        if prop is not None:
            rep = simulate_eval(
                prop, res.equity_curve,
                trading_days=fill_trading_days(conn, prop.day_boundary_utc_hour))
            prop_lines.append(f"prop[{cost.exec_label}]: {rep.summary()}")
    console.print(table)
    for note in fill_notes:
        console.print(f"[dim]{note}[/dim]")
    if prop is not None:
        rules = (
            f"daily -{prop.max_daily_loss_pct:.1%} of {prop.daily_loss_base}, "
            f"maxDD -{prop.max_drawdown_pct:.1%} {prop.drawdown_mode}")
        if prop.profit_target_pct > 0:
            rules += f", target +{prop.profit_target_pct:.1%}"
        if prop.min_trading_days > 0:
            rules += f", min {prop.min_trading_days} trading days"
        if prop.day_boundary_utc_hour:
            rules += f", reset {prop.day_boundary_utc_hour:02d}:00 UTC"
        console.print(
            f"prop screen (start ${prop.start_balance:.2f}): {rules}")
        for line in prop_lines:
            console.print(line, markup=False)  # exec_label sits in literal []
        console.print(
            "[dim]screen marks at bar closes — intrabar excursions are "
            "invisible (a real eval marks continuously) — and the rules are "
            "placeholders unless you passed the firm's verified terms "
            "(docs/PROP_EVAL.md). Informational; does not gate anything.[/dim]"
        )
    console.print("[dim]taker→maker gap ≈ the spread/fee tax this strategy is paying.[/dim]")


@app.command()
def confirm(
    agent: str = "twap_mr_regime_v1",
    coins: str = "BTC,ETH,SOL,HYPE",
    interval: str = "1h",
    days: int = 120,
    prefer: str = "taker",
    min_edge_bps: float = 3.0,
    min_sharpe: float = 1.0,
    min_trades: int = 20,
    cache: bool = True,
    config: str = "",
    vwap_window: int = 60,
    source: str = "api",
    maker_fill: str = "optimistic",
):
    """Confirm a strategy through the G0 gate: walk-forward + cost stress.

    Prints an explicit PASS/FAIL. A strategy must clear this on real history
    before it is eligible for paper→live promotion (see docs/GO_LIVE.md).
    --source store runs the gate on the harvested rolling candle store
    (--days 0 = everything stored); funding is always fetched — a G0 verdict
    with funding stripped out would be dishonest. --min-trades floors the
    per-split sample size: a verdict from a handful of trades is noise.
    --maker-fill resting makes every maker-priced arm replay the live maker
    lifecycle (rest → fill only on cross, judged on intrabar low/high when
    available → stale cancel; exits taker) instead of the optimistic
    instant-fill upper bound; resting-close forces close-only detection
    (the pre-B-FILL2 bound).
    """
    from ..backtest.confirm import confirm_strategy

    _, s = _conn()
    coin_list = [c.strip() for c in coins.split(",") if c.strip()]
    try:
        cfg = parse_agent_config(config)
    except ValueError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1) from e
    factories = _backtest_factories(cfg)
    if agent not in factories:
        console.print(f"[red]unknown agent {agent}; choose from {list(factories)}[/red]")
        raise typer.Exit(1)
    if maker_fill not in ("optimistic", "resting", "resting-close"):
        console.print(f"[red]unknown maker-fill {maker_fill!r}; choose optimistic, resting or resting-close[/red]")
        raise typer.Exit(1)
    per_year = {"1m": 525_600, "5m": 105_120, "15m": 35_040,
                "1h": 8_760, "4h": 2_190, "1d": 365}.get(interval, 8_760)
    frames = _load_backtest_frames(
        coin_list, source=source, interval=interval, days=days, cache=cache,
        vwap_window=vwap_window, api_url=s.hl_api_url)
    res = confirm_strategy(
        factories[agent], frames, prefer=prefer,
        min_edge_bps=min_edge_bps, min_sharpe=min_sharpe, min_trades=min_trades,
        periods_per_year=per_year, maker_fill=maker_fill,
    )
    console.print(res.summary())
    if not res.confirmed:
        raise typer.Exit(1)


def _git_rev(anchor: Path) -> str | None:
    """Best-effort `git rev-parse HEAD` anchored at a repo-resident path.

    The engine/fill-model revision materially changes verdicts (optimistic →
    resting maker fills flipped signs, Iters 50/51), so a recorded verdict
    without it is reproducible only by guesswork. Never fails the caller."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=anchor, capture_output=True,
            text=True, timeout=10, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    rev = out.stdout.strip()
    return rev if out.returncode == 0 and rev else None


@app.command()
def experiment(
    spec_path: str,
    check_only: bool = False,
    force: bool = False,
    store_root: str = "",
    record: bool = True,
    results_dir: str = "configs/experiments/results",
):
    """Run a pre-registered experiment spec — frozen confirm arms, ripeness-gated.

    Specs live in configs/experiments/*.json and freeze an evidence-bearing
    rerun (agent, universes, every arm's config/execution/fill model, pass
    thresholds, decision rule) BEFORE the deciding sample exists, so the arms
    can't be picked after peeking at early numbers. The command refuses to run
    until every coin's store span reaches the spec's min_span_days AND its
    missing-bar share stays within max_missing_pct — a harvester outage gap
    is a corrupted sample, not a ripe one
    (exit 3 = not ripe; --check-only prints the span readout and stops;
    --force runs anyway — an early run is a peek, record it as one, it is
    NOT the pre-registered verdict). Every run's full verdict (arms, numbers,
    ripeness, spec sha256, code rev) is persisted to --results-dir, committed
    beside the specs; forced peeks land as visibly-named .peek files
    (--no-record opts out). Results are informational: the printed decision
    rule is applied by the operator, never auto-acted on.
    """
    from dataclasses import asdict

    from ..backtest.experiments import (
        arm_comparison,
        check_ripeness,
        experiment_record,
        load_experiment_records,
        load_spec,
        preferred_full_scenario,
        run_experiment,
        write_experiment_record,
    )

    try:
        spec = load_spec(spec_path)
    except (OSError, ValueError) as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1) from e
    if spec.agent not in _backtest_factories({}):
        console.print(f"[red]spec agent {spec.agent!r} unknown; "
                      f"choose from {list(_backtest_factories({}))}[/red]")
        raise typer.Exit(1)

    root = store_root or None
    rep = check_ripeness(spec, root=root)
    console.print(rep.summary(), markup=False)
    if check_only:
        raise typer.Exit(0 if rep.ripe else 3)
    if not rep.ripe:
        if not force:
            console.print("[yellow]refusing to run before the pre-registered sample "
                          "exists (--force to peek anyway)[/yellow]")
            raise typer.Exit(3)
        console.print("[yellow]--force: running on an unripe sample — this is a peek, "
                      "NOT the pre-registered verdict[/yellow]")

    _, s = _conn()

    def _load(coins: list[str], window: int):
        return _load_backtest_frames(
            coins, source=spec.source, interval=spec.interval, days=spec.days,
            cache=True, vwap_window=window, api_url=s.hl_api_url, store_root=root)

    results = run_experiment(
        spec,
        factory_for=lambda cfg: _backtest_factories(cfg)[spec.agent],
        load_frames=_load,
    )
    for ar in results:
        console.print(f"\n[bold]{ar.arm.name}[/bold]")
        console.print(ar.result.summary(), markup=False)
    table = Table(title=f"Experiment {spec.name} — {spec.agent} ({spec.interval}, "
                        f"source={spec.source})")
    for col in ("arm", "exec", "verdict", "edge is/oos", "oos sharpe",
                "trades is/oos", "pocket is/oos/1x"):
        table.add_column(col)
    for ar in results:
        r = ar.result
        fill = f":{ar.arm.maker_fill}" if ar.arm.prefer == "maker" else ""
        full = preferred_full_scenario(
            ar.arm.prefer, [asdict(s) for s in r.cost_ladder]) or {}
        table.add_row(
            ar.arm.name,
            f"{ar.arm.prefer}{fill}",
            "[green]PASS[/green]" if r.confirmed else "[red]FAIL[/red]",
            f"{_fmt_bps(r.in_sample.edge_bps)} / {_fmt_bps(r.out_of_sample.edge_bps)}",
            "—" if r.out_of_sample.sharpe is None else f"{r.out_of_sample.sharpe:+.2f}",
            f"{r.in_sample.n_trades} / {r.out_of_sample.n_trades}",
            _pocket_cell(r.in_sample.pocket_share, r.out_of_sample.pocket_share,
                         full.get("pocket_share")),
        )
    console.print(table)
    # Prior recorded runs (loaded BEFORE this run's record is written): the
    # rerun protocol reads each new verdict against history — a PASS whose
    # pocket numbers don't fall on new data is the pocket renewing its badge.
    prior = load_experiment_records(spec.name, results_dir)
    if prior:
        pt = Table(title=f"Prior recorded runs — {spec.name} "
                         f"(read pocket/edge against these)")
        for col in ("ran_at", "arm", "exec", "verdict", "edge is/oos",
                    "pocket is/oos/1x"):
            pt.add_column(col)
        for rec_ in prior:
            stamp = str(rec_.get("ran_at", "?"))[:10]
            if rec_.get("forced"):
                stamp += " [yellow](peek)[/yellow]"
            for arm_ in rec_["arms"]:
                c = arm_comparison(arm_)
                fill = f":{arm_.get('maker_fill')}" if c["prefer"] == "maker" else ""
                pt.add_row(
                    stamp,
                    str(c["name"]),
                    f"{c['prefer']}{fill}",
                    {True: "[green]PASS[/green]", False: "[red]FAIL[/red]"}.get(
                        c["confirmed"], "—"),
                    f"{_fmt_bps(c['edge_is'])} / {_fmt_bps(c['edge_oos'])}",
                    _pocket_cell(c["pocket_is"], c["pocket_oos"], c["pocket_full"]),
                )
        console.print(pt)
    if spec.decision:
        console.print("decision rule (frozen with the spec):", style="bold")
        console.print(spec.decision, markup=False)
    if record:
        rec = experiment_record(
            spec, rep, results,
            ran_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            spec_sha256=hashlib.sha256(Path(spec_path).read_bytes()).hexdigest(),
            forced=not rep.ripe,
            code_rev=_git_rev(Path(spec_path).resolve().parent),
        )
        out = write_experiment_record(rec, results_dir)
        tag = " (forced peek — NOT the pre-registered verdict)" if not rep.ripe else ""
        console.print(f"verdict recorded: {out}{tag}")


def _fmt_bps(v: float | None) -> str:
    return "—" if v is None else f"{v:+.1f}"


def _fmt_share(v: float | None) -> str:
    return "—" if v is None else f"{v:.2f}"


def _pocket_cell(is_share: float | None, oos_share: float | None,
                 full_share: float | None) -> str:
    """`0.87 / 2.20 / 0.69` — IS / OOS / preferred-execution-1x full sample."""
    if is_share is None and oos_share is None and full_share is None:
        return "—"
    return f"{_fmt_share(is_share)} / {_fmt_share(oos_share)} / {_fmt_share(full_share)}"


@app.command()
def correlate(
    agent_a: str = "twap_mr_v1",
    agent_b: str = "breakout_v1",
    config_a: str = "",
    config_b: str = "",
    vwap_window_a: int = 60,
    vwap_window_b: int = 60,
    coins: str = "BTC,ETH,SOL",
    interval: str = "1h",
    days: int = 30,
    maker: bool = False,
    starting_capital: float = 1000.0,
    cache: bool = True,
    source: str = "api",
    funding: bool = True,
):
    """Daily-PnL Pearson correlation between two agents on the same history.

    A second strategy only diversifies the book if its PnL is low-correlated
    with the first (B-EDGE2c) — this runs both agents over the same candles
    (each arm may use its own --config/--vwap-window, e.g. breakout needs
    window ≥ lookback+1 to carry the closes) and correlates their UTC-day PnL.
    Same cost model for both arms; --maker for the maker arm.
    """
    from ..backtest.correlate import pnl_correlation
    from ..backtest.engine import Backtester, CostModel

    _, s = _conn()
    coin_list = [c.strip() for c in coins.split(",") if c.strip()]
    arms = []
    for agent, config, window in (
        (agent_a, config_a, vwap_window_a), (agent_b, config_b, vwap_window_b),
    ):
        try:
            cfg = parse_agent_config(config)
        except ValueError as e:
            console.print(f"[red]{e}[/red]")
            raise typer.Exit(1) from e
        factories = _backtest_factories(cfg)
        if agent not in factories:
            console.print(f"[red]unknown agent {agent}; choose from {list(factories)}[/red]")
            raise typer.Exit(1)
        arms.append((agent, cfg, window, factories[agent]))

    frames_by_window: dict[int, list] = {}
    results = []
    for _agent, _cfg, window, factory in arms:
        if window not in frames_by_window:
            frames_by_window[window] = _load_backtest_frames(
                coin_list, source=source, interval=interval, days=days,
                cache=cache, vwap_window=window, api_url=s.hl_api_url,
                with_funding=funding)
        conn = init_db(":memory:")
        bt = Backtester(CostModel(maker=maker), conn=conn,
                        starting_capital=starting_capital)
        results.append(bt.run(factory(conn), frames_by_window[window]))

    res_a, res_b = results
    corr = pnl_correlation(
        res_a.equity_curve, res_b.equity_curve,
        starting_a=starting_capital, starting_b=starting_capital)

    table = Table(title=f"Correlation [{'maker' if maker else 'taker'}] "
                        f"({interval}, {len(corr.days)} overlapping days)")
    for col in ("arm", "agent", "w", "cfg", "net_pnl", "edge_bps", "trades"):
        table.add_column(col)
    for label, (agent, cfg, window, _), res in zip("AB", arms, results, strict=True):
        sc = res.scorecard
        table.add_row(
            label, agent, str(window),
            json.dumps(cfg, separators=(",", ":")) if cfg else "—",
            f"{sc.net_pnl:+.2f}",
            "—" if sc.edge_bps is None else f"{sc.edge_bps:+.1f}",
            str(sc.n_trades),
        )
    console.print(table)
    console.print(corr.summary())


@app.command()
def ws(
    coins: str = "BTC,ETH,SOL,HYPE",
    snapshot: Path = Path("data/ws_snapshot.json"),
    seconds: float = 0.0,
):
    """Run the WebSocket market-data service: maintain live state, write a snapshot.

    Long-running (supervise via systemd). The tick reads the snapshot when fresh
    (set HLBOT_WS_SNAPSHOT) for sub-second mids, L2 depth, live liquidations, and
    our own userFills (instant maker-fill detection — the address follows
    HL_VAULT_ADDRESS > HL_TRADER_ADDRESS > HL_ADDRESS, same as the tick).
    seconds=0 runs forever.
    """
    from ..exec.orders import resolve_trader_address
    from ..ingest.ws import run_ws

    _, s = _conn()
    coin_list = [c.strip() for c in coins.split(",") if c.strip()]
    user_address = resolve_trader_address()
    console.print(
        f"[green]ws[/green] subscribing {coin_list} + userFills[{user_address}] "
        f"→ {snapshot} (every 1s)"
    )
    run_ws(coin_list, snapshot, base_url=s.hl_api_url, duration_s=(seconds or None),
           user_address=user_address)


@app.command()
def health(max_tick_age_s: int = 900, max_decision_age_s: int = 259_200, heartbeat: bool = True):
    """Assess bot health (tick/ingest freshness, equity, paused agents, 24h PnL).

    Pings HEALTHCHECK_URL when healthy (dead-man switch) and Telegram-alerts when
    not. Designed to run on a timer alongside the tick.
    """
    import os

    from ..ops.health import (
        assess_health,
        ping_heartbeat,
        read_deploy_signals,
        read_pager_signals,
    )

    conn, s = _conn()
    rep = assess_health(
        conn, max_tick_age_s=max_tick_age_s, max_decision_age_s=max_decision_age_s,
        deploy=read_deploy_signals(s.db_path), pager=read_pager_signals())
    console.print(rep.render())
    down = rep.status == "down"
    # Only DOWN is a real page: warn (fresh box, no ticks yet, a paused agent) is
    # benign and must not spam the heartbeat/Telegram every tick.
    if heartbeat:
        ping_heartbeat(os.environ.get("HEALTHCHECK_URL"), ok=not down)
    if down:
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
def track_record(
    out: Path = Path("data/track_record"),
    paper_funding: bool = typer.Option(
        True, "--paper-funding/--no-paper-funding",
        help="Model funding accrual over paper holds from HL funding-rate "
             "history for the paper section (network; per-coin failures "
             "degrade to funding=0 with a warning). No-op without a paper book.",
    ),
    paper_mark: bool = typer.Option(
        True, "--paper-mark/--no-paper-mark",
        help="Mark open paper positions at the current mid for the paper "
             "section's open-uPnL column (one allMids call; a fetch failure "
             "degrades to '—'). Zero calls without open paper positions. "
             "Marks are shown beside the cards, never folded into them.",
    ),
):
    """Export a public-grade track record (equity curve, Sharpe, DD, per-agent).

    Writes track_record.{json,md} for capital/AUM due diligence (Path C) and the
    go-live gates. Read-only on the DB. Paper agents appear in their own
    clearly-labeled forward-test section, never in the live table.
    """
    from ..reports.track_record import export

    conn, s = _conn()
    funding_by_coin = _fetch_paper_funding(conn, s) if paper_funding else {}
    mids: dict[str, float] = {}
    if paper_mark and any(
            paper_open_positions(conn, a) for a in list_paper_agents(conn)):
        mids = _fetch_mids(s)
    jp, mp, hp = export(
        conn, out,
        paper_funding_by_coin=funding_by_coin or None,
        paper_mids=mids or None,
    )
    console.print(mp.read_text())
    console.print(f"[green]✓[/green] wrote {jp}, {mp}, and {hp} (open the .html to share)")


@app.command()
def gates(
    agent: str = typer.Option(None, "--agent", help="Only this agent."),
    funding: bool = typer.Option(
        True, "--funding/--no-funding",
        help="Model funding accrual for paper-book (G1) cards from HL "
             "funding-rate history (network; per-coin failures degrade to "
             "funding=0 with a warning; zero calls without a paper book).",
    ),
):
    """Read-only roadmap gate readout (ROADMAP_TO_1M.md §4: G1/G2/G3).

    Where each agent stands on the evidence ladder — paper span/edge/sample/
    breaches (G1), live net + drawdown (G2), Sharpe stability (G3) — with the
    blocking checks named. Informational only: never flips a mode (promotion
    stays human-gated). G0 (sim) is `hlbot confirm`, not recorded in the DB.
    """
    from ..supervisor.gates import (
        GATE_TITLES,
        GATE_UNLOCKS,
        effective_mode,
        evaluate_roadmap_gates,
        fills_span_ms,
        paper_span_ms,
    )
    from ..supervisor.goals import AgentGoals, load_goals

    conn, s = _conn()
    cfg_by_agent: dict[str, AgentGoals] = {}
    for p in sorted(Path(CONFIG_DIR).glob("*.yaml")):
        try:
            for g in load_goals(p):
                cfg_by_agent[g.agent] = g
        except Exception as e:  # noqa: BLE001
            console.print(f"[yellow]warn:[/yellow] skipping {p.name}: {e}")
    evidence_agents = {a for a in list_paper_agents(conn)} | {
        r[0] for r in conn.execute(
            "SELECT DISTINCT agent FROM fills WHERE agent IS NOT NULL")
    }
    names = sorted((set(cfg_by_agent) | evidence_agents) - {"_account", "manual"})
    if agent:
        names = [n for n in names if n == agent]
        if not names:
            console.print(f"[red]no config or evidence for agent '{agent}'[/red]")
            raise typer.Exit(1)

    funding_by_coin = _fetch_paper_funding(conn, s) if funding else {}
    table = Table(title="Roadmap gates (read-only; promotion is human-gated)")
    for col in ("agent", "mode", "gate", "verdict", "unlocks", "blockers"):
        table.add_column(col)
    for name in names:
        cfg = cfg_by_agent.get(name)
        mode = effective_mode(conn, name, default=cfg.mode if cfg else "paper")
        results = evaluate_roadmap_gates(
            conn, name, capital=cfg.capital if cfg else None,
            funding_by_coin=funding_by_coin or None)
        if not results:
            no_paper = paper_span_ms(conn, name) is None
            no_fills = fills_span_ms(conn, name) is None
            if no_paper and no_fills:
                table.add_row(name, mode, "—", "[dim]no evidence yet[/dim]", "", "")
            continue
        for r in results:
            verdict = (
                "[green]PASS[/green]" if r.passed
                else f"[red]{len(r.checks) - len(r.blockers)}/{len(r.checks)}[/red]"
            )
            table.add_row(
                name, mode, GATE_TITLES[r.gate], verdict,
                GATE_UNLOCKS[r.gate] if r.passed else "",
                "" if r.passed else "; ".join(c.detail for c in r.blockers),
            )
    console.print(table)
    console.print(
        "[dim]G0 (sim) = hlbot confirm — judge maker claims with "
        "--prefer maker --maker-fill resting. A PASS here is evidence for the "
        "operator, never an automatic promotion.[/dim]"
    )


@app.command("prop-check")
def prop_check(
    start_balance: float = typer.Option(
        0.0, help="Eval account starting balance $ (0 = first equity "
                  "snapshot in the window — fine for 'would my live curve "
                  "have survived?', wrong for sizing a real eval)."),
    daily_loss_pct: float = typer.Option(
        0.05, help="Max daily loss as a fraction (firm rule — VERIFY)."),
    daily_loss_base: str = typer.Option(
        "start", help="What the daily %% is of: 'start' (fixed $) or "
                      "'day_open' (that day's opening equity)."),
    max_dd_pct: float = typer.Option(
        0.10, help="Max drawdown as a fraction (firm rule — VERIFY)."),
    dd_mode: str = typer.Option(
        "trailing", help="'trailing' (from equity high-water mark) or "
                         "'static' (from start balance)."),
    target_pct: float = typer.Option(
        0.0, help="Profit target fraction for a PASS (0 = no target rule)."),
    min_trading_days: int = typer.Option(
        0, help="Minimum distinct trading days for a PASS (0 = no rule)."),
    boundary_hour: int = typer.Option(
        0, min=0, max=23, help="UTC hour at which the daily limit resets."),
    days: float = typer.Option(
        0.0, help="Replay only the last N days of snapshots (0 = all)."),
):
    """Read-only prop-eval rule replay (CAPITAL.md Track B, docs/PROP_EVAL.md).

    Replays the account equity curve (equity_snapshots — includes unrealized
    PnL) against a firm's eval rules: day-boundary daily loss, trailing or
    static max drawdown, profit target + min trading days. Reports every
    would-be breach and the current headroom. The defaults are PLACEHOLDERS
    shaped like common prop rules — pass the verified firm numbers.
    """
    from ..risk.prop import (
        EvalProfile,
        equity_points,
        fill_trading_days,
        simulate_eval,
    )

    if daily_loss_base not in ("start", "day_open"):
        raise typer.BadParameter("daily-loss-base must be 'start' or 'day_open'")
    if dd_mode not in ("trailing", "static"):
        raise typer.BadParameter("dd-mode must be 'trailing' or 'static'")

    conn, _ = _conn()
    since_ms = int((time.time() - days * 86400) * 1000) if days > 0 else 0
    pts = equity_points(conn, since_ms=since_ms)
    if not pts:
        console.print("[red]no equity snapshots in the window[/red] — run "
                      "`hlbot ingest` on the box that trades, or widen --days")
        raise typer.Exit(1)

    profile = EvalProfile(
        name="cli", start_balance=start_balance or pts[0][1],
        max_daily_loss_pct=daily_loss_pct,
        daily_loss_base=daily_loss_base,  # type: ignore[arg-type]
        max_drawdown_pct=max_dd_pct,
        drawdown_mode=dd_mode,  # type: ignore[arg-type]
        profit_target_pct=target_pct, min_trading_days=min_trading_days,
        day_boundary_utc_hour=boundary_hour,
    )
    report = simulate_eval(
        profile, pts,
        trading_days=fill_trading_days(conn, boundary_hour, since_ms))

    span_d = (report.last_ts_ms - report.first_ts_ms) / 86_400_000
    console.print(
        f"rules: daily -{daily_loss_pct:.1%} of {daily_loss_base}, "
        f"maxDD -{max_dd_pct:.1%} {dd_mode}, "
        f"target +{target_pct:.1%}, min {min_trading_days} trading days, "
        f"reset {boundary_hour:02d}:00 UTC  "
        f"[dim](placeholders unless you passed the firm's verified "
        f"numbers)[/dim]"
    )
    console.print(
        f"curve: {report.n_points} snapshots over {span_d:.1f}d, "
        f"start ${profile.start_balance:.2f} → last ${report.last_equity:.2f} "
        f"(HWM ${report.high_water_mark:.2f}), "
        f"{report.trading_days} trading days"
    )
    if report.breaches:
        table = Table(title="Would-be breaches (any ONE fails a real eval)")
        for col in ("when (UTC)", "rule", "equity", "floor", "detail"):
            table.add_column(col)
        for b in report.breaches:
            table.add_row(
                time.strftime("%Y-%m-%d %H:%M", time.gmtime(b.ts_ms / 1000)),
                b.rule, f"${b.equity:.2f}", f"${b.floor:.2f}", b.detail)
        console.print(table)
    else:
        console.print("[green]no breaches in the replayed window[/green]")
    console.print(
        f"headroom now: daily ${report.daily_headroom:.2f} "
        f"(floor ${report.daily_floor:.2f}), "
        f"drawdown ${report.drawdown_headroom:.2f} "
        f"(floor ${report.drawdown_floor:.2f})"
    )
    verdict_color = {"FAIL": "red", "PASS": "green"}.get(report.verdict, "yellow")
    console.print(f"verdict: [{verdict_color}]{report.verdict}[/{verdict_color}]")
    gap = f", largest gap {report.max_gap_hours:.1f}h" if report.max_gap_hours else ""
    density = (
        f"{report.obs_per_day:.1f} obs/day" if report.obs_per_day else "1 snapshot"
    )
    console.print(
        f"[dim]sampled curve: {density}{gap} — a real eval marks "
        f"continuously, so breaches between snapshots are invisible here; "
        f"treat thin headroom as a fail.[/dim]"
    )


@app.command("sleeve-check")
def sleeve_check(
    hard_cap: float = typer.Option(
        ..., help="The tranche $ funded into the sleeve — the most it can "
                  "ever lose. Write it down at funding (docs/MOONSHOT.md); "
                  "there is no sensible default."),
    address: str = typer.Option(
        "", help="Sleeve account address (default: HLBOT_SLEEVE_ADDRESS env)."),
    max_bet_frac: float = typer.Option(
        0.25, help="Per-bet isolated-margin cap as a fraction of the hard cap."),
    max_concurrent: int = typer.Option(2, help="Max simultaneous bets."),
    kill_floor_frac: float = typer.Option(
        0.25, help="Equity fraction of the hard cap at/below which the "
                   "sleeve is DEAD (flatten, stand down)."),
):
    """Read-only moonshot-sleeve ring-fence check (CAPITAL.md Track D,
    docs/MOONSHOT.md).

    Fetches the sleeve account's clearinghouseState and verifies the
    loss-bound invariants: every bet isolated-margin, per-bet margin under
    the cap, bet count under the cap, equity vs kill floor, profits above the
    hard cap flagged for the sweep-to-core ratchet, and the sleeve address
    not being a core (trader/vault) account. Never trades; run it before and
    after every bet and weekly in between.
    """
    import os
    import re

    import httpx

    from ..config import resolve_vault_address
    from ..exec.orders import resolve_trader_address
    from ..risk.sleeve import SleeveConfig, evaluate_sleeve

    addr = (address or os.environ.get("HLBOT_SLEEVE_ADDRESS", "")).strip()
    if not addr:
        console.print("[red]no sleeve address[/red] — pass --address or set "
                      "HLBOT_SLEEVE_ADDRESS")
        raise typer.Exit(1)
    if not re.fullmatch(r"0x[0-9a-fA-F]{40}", addr):
        console.print(f"[red]sleeve address malformed:[/red] {addr!r} "
                      "(want 0x + 40 hex chars)")
        raise typer.Exit(1)

    try:
        cfg = SleeveConfig(
            hard_cap=hard_cap, max_bet_frac=max_bet_frac,
            max_concurrent_bets=max_concurrent,
            kill_floor_frac=kill_floor_frac)
    except ValueError as e:
        raise typer.BadParameter(str(e)) from e

    s = Settings.from_env()
    try:
        with httpx.Client(timeout=10) as cli:
            st = cli.post(
                s.hl_api_url + "/info",
                json={"type": "clearinghouseState", "user": addr},
            ).json() or {}
    except httpx.HTTPError as e:
        console.print(f"[red]clearinghouseState fetch failed:[/red] {e}")
        raise typer.Exit(1) from e

    report = evaluate_sleeve(
        cfg, st, address=addr,
        core_addresses=(resolve_trader_address(), resolve_vault_address() or ""))
    if report.status == "NO_DATA":
        console.print("[red]no clearinghouse data for the sleeve address[/red] "
                      "— never funded, or wrong address?")
        raise typer.Exit(1)

    console.print(
        f"rules: hard cap ${cfg.hard_cap:.2f}, per-bet "
        f"${cfg.max_bet_margin:.2f} ({max_bet_frac:.0%}), "
        f"max {max_concurrent} bets, kill floor ${cfg.kill_floor:.2f} "
        f"({kill_floor_frac:.0%})")
    console.print(
        f"sleeve {addr}: equity ${report.equity:.2f}, "
        f"committed ${report.committed_margin:.2f} across "
        f"{len(report.bets)} bet(s), kill headroom ${report.kill_headroom:.2f}")
    if report.bets:
        table = Table(title="Open bets")
        for col in ("coin", "side", "margin type", "lev", "margin $",
                    "value $", "uPnL $"):
            table.add_column(col)
        for b in report.bets:
            table.add_row(
                b.coin, "LONG" if b.szi > 0 else "SHORT",
                b.leverage_type or "?",
                f"{b.leverage:.0f}x" if b.leverage else "?",
                f"{b.margin_used:.2f}", f"{b.position_value:.2f}",
                f"{b.unrealized_pnl:+.2f}")
        console.print(table)
    for v in report.violations:
        console.print(f"[red]✗ {v}[/red]")
    for n in report.notes:
        console.print(f"[yellow]• {n}[/yellow]")
    color = {"OK": "green", "DEAD": "red"}.get(report.status, "red")
    console.print(f"status: [{color}]{report.status}[/{color}]")
    console.print(
        "[dim]read-only: violations are for the operator to fix by hand — "
        "the bot never trades the sleeve. Top-ups are invisible to this "
        "check; the hard cap only binds while funding stays one written-down "
        "tranche (docs/MOONSHOT.md).[/dim]")


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
