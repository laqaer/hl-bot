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
from ..agents.femr import FemrAgent
from ..agents.funding_arb import FundingArbAgent
from ..agents.funding_carry import FundingCarryAgent
from ..agents.liq_cascade import LiqCascadeAgent
from ..agents.meta_allocator import MetaAllocator, MetaAllocatorConfig
from ..agents.pairs_reversion import PairsReversionAgent
from ..agents.runtime import run_tick
from ..agents.ts_momentum import TsMomentumAgent
from ..agents.twap_mr import TwapMrAgent
from ..agents.twap_mr_regime import TwapMrRegimeAgent
from ..agents.veto import VetoAgent
from ..agents.xfund_carry import XFundCarryAgent
from ..agents.xsect_momentum import XSectMomentumAgent
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


# Agents retired from the LIVE execution roster: they keep evaluating in paper
# for ongoing measurement, but are hard-blocked from placing live orders
# regardless of their agent_state, until a universe+variant earns a G0 PASS.
# Reason is surfaced in the skip log so the retirement is auditable.
RETIRED_LIVE_AGENTS: dict[str, str] = {
    # femr's 130%-APR funding entry never trips on liquid coins (B1), and funding
    # carry shows no net-of-cost edge even on high-funding alts (B1-alt). Dormant
    # and edgeless — retired from live until a demonstrated G0 PASS (B-femr-regime).
    "femr_v1": "retired: dormant on majors, no G0 PASS (B1/B1-alt)",
}


def _filter_live_agents_by_state(conn, agents):
    """Return agents allowed to place live orders plus skipped reasons.

    Paper/default state is safe: an agent must be explicitly enabled and in
    live_small/live mode before it enters the live execution roster. Agents in
    ``RETIRED_LIVE_AGENTS`` are hard-blocked from live regardless of state.
    """
    rows = conn.execute("SELECT agent, mode, enabled FROM agent_state").fetchall()
    state = {r["agent"]: (r["mode"], int(r["enabled"])) for r in rows}
    live_agents = []
    skipped: dict[str, str] = {}
    for agent in agents:
        if agent.name in RETIRED_LIVE_AGENTS:
            skipped[agent.name] = RETIRED_LIVE_AGENTS[agent.name]
            continue
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
    snapshot_equity(conn, s.hl_address, s.hl_api_url)
    console.print(f"[green]✓[/green] fills:{n_fills} funding:{n_fund} +1 equity snapshot")


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
    conn, _ = _conn()
    actions = supervise(conn, configs)
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
def tick(coins: str = "BTC,ETH,SOL,HYPE,ZEC"):
    """Run one tick of all wired agents (paper mode)."""
    conn, s = _conn()
    coin_list = [c.strip() for c in coins.split(",") if c.strip()]
    agents = [
        VetoAgent(config={"lookback_days": 30, "min_trades": 20, "veto_threshold_bps": -5.0}, conn=conn),
        FundingArbAgent(config={"coins": coin_list}),
    ]
    decisions = run_tick(conn, agents, s.hl_api_url, coin_list, force_paper=True)
    console.print(f"[green]✓[/green] {len(decisions)} decisions logged")
    for d in decisions:
        verdict = (d.market_snapshot or {}).get("verdict", "")
        tag = f"[{verdict}]" if verdict else ""
        console.print(f"  {d.agent} {d.action} {d.coin or ''} {tag} :: {d.reasoning}")


def _enrich_view(view, api_url: str, vol: dict[str, float]) -> None:
    """Augment a MarketView with 1h candles (top-vol coins) and spot mids.

    Liquidations are NOT sourced here: Hyperliquid exposes no public
    ``liquidations`` info endpoint (the old REST call to one always returned
    nothing — REVIEW C6). The real feed is the WS ``trades`` stream's
    liquidation flag, overlaid from the WS snapshot by the caller. Without that
    snapshot, ``view.extra['liquidations']`` stays empty and liq_cascade holds.
    """
    import httpx as _httpx

    # ---- top-20-by-volume universe ----
    top = sorted(vol.items(), key=lambda kv: kv[1], reverse=True)[:20]
    top_coins = [c for c, _ in top]

    candles_1h: dict[str, dict] = {}
    closes_by_coin: dict[str, list[float]] = {}
    spot_mids: dict[str, float] = {}

    with _httpx.Client(timeout=15) as cli:
        # 60 × 1m candles -> vwap & sigma per top coin
        end_ms = int(time.time() * 1000)
        start_ms = end_ms - 60 * 60_000
        for coin in top_coins:
            try:
                cs = cli.post(api_url + "/info", json={
                    "type": "candleSnapshot",
                    "req": {"coin": coin, "interval": "1m",
                            "startTime": start_ms, "endTime": end_ms},
                }).json() or []
                if not isinstance(cs, list) or len(cs) < 10:
                    continue
                pxs, vols = [], []
                for k in cs:
                    try:
                        c_px = float(k.get("c", 0))
                        c_vol = float(k.get("v", 0))
                        if c_px > 0:
                            pxs.append(c_px)
                            vols.append(c_vol)
                    except (TypeError, ValueError):
                        continue
                if len(pxs) < 10:
                    continue
                tot_vol = sum(vols)
                vwap = sum(p * v for p, v in zip(pxs, vols, strict=False)) / tot_vol if tot_vol > 0 else sum(pxs) / len(pxs)
                mean = sum(pxs) / len(pxs)
                var = sum((p - mean) ** 2 for p in pxs) / len(pxs)
                sigma = var ** 0.5
                candles_1h[coin] = {"vwap": vwap, "sigma": sigma, "n": len(pxs)}
                closes_by_coin[coin] = pxs
            except Exception:  # noqa: BLE001
                continue

        # Spot mids for BTC/ETH/SOL. HL spot pairs use wrapped tokens
        # (UBTC/USDC=@142, UETH/USDC=@151, USOL/USDC=@156) and the midPx is
        # quoted in scaled native units. We use allMids @N indices and scale
        # against the perp mid to detect basis: skip pair if it would produce
        # a clearly nonsensical (>5%) basis (means we don't have a clean spot).
        try:
            spot = cli.post(api_url + "/info", json={"type": "spotMetaAndAssetCtxs"}).json()
            if isinstance(spot, list) and len(spot) == 2:
                meta = spot[0] or {}
                ctxs = spot[1] or []
                universe = meta.get("universe", []) or []
                tokens = meta.get("tokens", []) or []
                name_by_token = {t.get("index"): t.get("name") for t in tokens}
                # token szDecimals required to normalize price
                wei_by_token = {t.get("index"): int(t.get("weiDecimals", 0) or 0) for t in tokens}
                for u, c in zip(universe, ctxs, strict=False):
                    pair_tokens = u.get("tokens", [])
                    if len(pair_tokens) < 2:
                        continue
                    base_idx = pair_tokens[0]
                    base_name = name_by_token.get(base_idx)
                    quote_name = name_by_token.get(pair_tokens[1])
                    if quote_name != "USDC":
                        continue
                    norm = None
                    if base_name in ("UBTC", "UETH", "USOL"):
                        norm = base_name[1:]   # strip leading 'U'
                    elif base_name in ("BTC", "ETH", "SOL"):
                        norm = base_name
                    if norm not in ("BTC", "ETH", "SOL"):
                        continue
                    try:
                        raw_mid = float(c.get("midPx") or 0)
                    except (TypeError, ValueError):
                        raw_mid = 0
                    if raw_mid <= 0:
                        continue
                    # USDC weiDecimals=8 (standard). base wei from token meta.
                    base_wei = wei_by_token.get(base_idx, 8)
                    quote_wei = 8  # USDC
                    scaled_mid = raw_mid * (10 ** (base_wei - quote_wei))
                    # only adopt if scaled_mid is within 5% of perp mid (sanity)
                    perp_mid = view.mids.get(norm)
                    if (
                        perp_mid and scaled_mid > 0
                        and 0.5 < scaled_mid / perp_mid < 1.5
                        and ((base_name or "").startswith("U") or norm not in spot_mids)
                    ):
                        # Prefer wrapped (U-prefixed) over plain if both present.
                        spot_mids[norm] = scaled_mid
        except Exception:  # noqa: BLE001
            pass

    view.extra["candles_1h"] = candles_1h
    view.extra["closes"] = closes_by_coin
    view.extra["spot_mids"] = spot_mids
    # Liquidations come only from the WS feed (overlaid by the caller). Default
    # empty so liq_cascade safely holds when no WS snapshot is present.
    view.extra["liquidations"] = []


@app.command()
def femr_tick(live: bool = False, execution: str = "taker"):
    """Run FEMR (Funding Extremes Mean Reversion) one tick.

    paper (default): log decisions only, no orders placed.
    live: place real orders on MAIN account, gated by guardrails.
          Bot only touches positions it itself opened (cloid-tagged).
    """
    from ..agents.decisions import Decision, log_decision
    from ..agents.runtime import collect_decisions, fetch_market_view
    from ..exec.orders import (
        HL_TRADER_ADDRESS,
        GuardrailConfig,
        bot_owned_coins,
        build_exchange,
        check_guardrails,
        close_position,
        coin_in_cooldown,
        dynamic_daily_loss_limit,
        place_market_order,
        reconcile_positions,
        telegram_alert,
    )

    conn, s = _conn()

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
    agents = [
        FemrAgent(config=_cfg("femr_v1", {
            "max_notional_per_trade": 20.0,
            "max_total_notional": 40.0,
            "funding_enter_per_hr": 0.00015,
            "funding_exit_per_hr": 0.00005,
        }), conn=conn),
        TwapMrAgent(config=_cfg("twap_mr_v1", {}), conn=conn),
        TwapMrRegimeAgent(config=_cfg("twap_mr_regime_v1", {}), conn=conn),
        LiqCascadeAgent(config=_cfg("liq_cascade_v1", {}), conn=conn),
        BasisAgent(config=_cfg("basis_v1", {}), conn=conn),
    ]
    if live:
        agents, skipped_live = _filter_live_agents_by_state(conn, agents)
        if skipped_live:
            console.print(
                "[yellow]live roster skipped[/yellow]: "
                + ", ".join(f"{name}({why})" for name, why in skipped_live.items())
            )
        if not agents:
            console.print("[yellow]LIVE MODE but no agent_state rows are enabled in live_small/live; no orders possible[/yellow]")
            return

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

    view = fetch_market_view(s.hl_api_url, [])
    _enrich_view(view, s.hl_api_url, view.extra.get("day_ntl_vlm", {}))

    # Overlay a fresh WS snapshot if available (sub-second mids, L2 book, and a
    # REAL liquidations feed for liq_cascade). Purely additive; REST is the
    # fallback when no fresh snapshot exists. Opt-in via HLBOT_WS_SNAPSHOT.
    import os as _os
    ws_path = _os.environ.get("HLBOT_WS_SNAPSHOT")
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

    # liq_cascade can only be fed by the WS liquidations stream. If it is in the
    # roster but no WS snapshot is configured, it will never see an event and is
    # effectively disabled — say so rather than silently holding forever.
    if not ws_path and any(a.name == "liq_cascade_v1" for a in agents):
        console.print(
            "[yellow]liq_cascade_v1 active but HLBOT_WS_SNAPSHOT is unset[/yellow]: "
            "no liquidation feed → agent will hold every tick. Run `hlbot ws` and "
            "set HLBOT_WS_SNAPSHOT to feed it."
        )

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

    # Gather decisions through the shared safe core (REVIEW M3): one agent
    # raising in decide() logs an error and the tick continues instead of
    # crashing the whole live loop. `place`/`flatten` are deferred (logged ONLY
    # after exchange acceptance in the execution loop below — otherwise the
    # cooldown check would see our own intent rows and block forever); `hold`
    # is collected for display but never logged.
    all_decisions = collect_decisions(
        conn, agents, view,
        is_paper=not live,
        defer_actions=frozenset({"hold", "place", "flatten"}),
    )

    console.print(f"[green]✓[/green] {len(all_decisions)} decisions (live={live})")
    for d in all_decisions:
        tag = "" if d.action != "hold" else "[dim]"
        end = "" if d.action != "hold" else "[/dim]"
        console.print(f"  {tag}{d.agent} {d.action} {d.coin or ''} :: {d.reasoning}{end}")

    if not live:
        console.print("[yellow]PAPER MODE[/yellow]")
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
            log_rest,
            reconcile_maker_fills,
            stale_working,
            working_orders,
        )
        from ..exec.orders import cancel_order, maker_limit_price, place_limit_order
        from ..ingest.hyperliquid import ingest_fills
        ingest_fills(conn, s.hl_address, s.hl_api_url)  # so cloid fills are visible
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

    # Execute
    agent_names = {a.name for a in agents}
    for d in all_decisions:
        if d.agent not in agent_names or d.coin is None:
            continue

        if d.action == "place" and d.sz and d.side:
            if not ok:
                console.print(f"[dim]SKIP {d.agent} {d.coin}: guardrail blocks new entries[/dim]")
                continue
            if coin_in_cooldown(conn, d.coin, agent=d.agent):
                console.print(f"[dim]SKIP {d.agent} {d.coin}: in cooldown[/dim]")
                continue
            is_buy = (d.side == "B")
            if execution == "maker":
                # Already have a working quote on this coin? leave it.
                if d.coin in working_orders(conn, d.agent):
                    console.print(f"[dim]SKIP {d.agent} {d.coin}: maker quote already resting[/dim]")
                    continue
                bt = (view.book_top or {}).get(d.coin)
                limit_px = maker_limit_price(
                    bt[0] if bt else None, bt[1] if bt else None, is_buy, d.px or 0.0)
                res = place_limit_order(exchange, d.coin, is_buy, d.sz, limit_px,
                                        post_only=True, cloid=d.cloid)
                if res.status == "resting":
                    console.print(f"[cyan]RESTING[/cyan] {d.coin} {'BUY' if is_buy else 'SELL'} {d.sz} @ ${limit_px} oid={res.oid}")
                    log_rest(conn, d.agent, d.coin, d.side, d.sz, limit_px, d.cloid, res.oid)
                elif res.ok:  # filled immediately (rare for post-only)
                    console.print(f"[bold green]FILLED(maker)[/bold green] {d.coin} @ ${res.avg_px}")
                    if res.avg_px:
                        d.px = res.avg_px
                    log_decision(conn, d)
                else:
                    console.print(f"[red]MAKER REJECT[/red] {d.coin}: {res.status} — {res.error}")
                conn.commit()
                continue
            res = place_market_order(exchange, d.coin, is_buy, d.sz,
                                     slippage_pct=0.01, cloid=d.cloid)
            if res.ok:
                console.print(f"[bold green]FILLED[/bold green] {d.coin} {'BUY' if is_buy else 'SELL'} {res.filled_sz} @ ${res.avg_px}")
                # Log place ONLY after fill confirmed, with the REAL fill px/sz
                # (not the pre-trade mid) so downstream stops/TPs key off truth.
                if res.avg_px:
                    d.px = res.avg_px
                if res.filled_sz:
                    d.sz = res.filled_sz
                log_decision(conn, d)
            else:
                console.print(f"[red]REJECT[/red] {d.coin}: {res.status} — {res.error}")
                log_decision(conn, Decision(
                    agent=d.agent, action="rejected", coin=d.coin,
                    reasoning=f"HL rejected: {res.error}", is_paper=False,
                ))
                conn.commit()

        elif d.action == "flatten":
            res = close_position(exchange, d.coin, cloid=d.cloid)
            if res.ok:
                console.print(f"[bold]CLOSED[/bold] {d.coin} @ ${res.avg_px}")
                # Log the flatten immediately so ownership clears this tick rather
                # than waiting for next-tick reconciliation. Record the real exit px.
                if res.avg_px:
                    d.px = res.avg_px
                log_decision(conn, d)
                conn.commit()
            else:
                console.print(f"[red]CLOSE FAILED[/red] {d.coin}: {res.error}")


@app.command()
def backtest_fetch(
    coins: str = "BTC,ETH,SOL",
    interval: str = "1h",
    days: int = 30,
    refresh: bool = False,
    end_offset_days: int = 0,
):
    """Fetch + cache HL candle/funding history for offline, reproducible backtests.

    Writes a gzipped frame dataset under data/backtest_cache/ (gitignored).
    Run this once where HL is reachable; then `hlbot backtest` runs without network.

    ``--end-offset-days N`` ends the window N days in the past instead of now, so
    you can pull a *disjoint, older* window for out-of-time validation (e.g.
    ``--days 120 --end-offset-days 120`` = the 120d before the trailing 120d).
    """
    import time as _time

    from ..backtest.baskets import resolve_basket
    from ..backtest.data import cached_or_fetch, default_cache_path

    _, s = _conn()
    coin_list = resolve_basket(coins)
    end_ms = None if end_offset_days <= 0 else int(_time.time() * 1000) - end_offset_days * 86_400_000
    path = default_cache_path(coin_list, interval, days, end_ms)
    window = f"{days}d{'' if end_ms is None else f' ending {end_offset_days}d ago'}"
    console.print(f"[dim]fetching {window} {interval} for {coin_list}…[/dim]")
    try:
        frames = cached_or_fetch(coin_list, interval=interval, days=days,
                                 base_url=s.hl_api_url, refresh=refresh, end_ms=end_ms)
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
    params: str = "",
):
    """Replay an agent over real Hyperliquid history with an explicit cost model.

    Fetches candle + funding history (network), drives the agent's real
    ``decide()``, simulates fills (taker by default), and scores the run with the
    same code used live. With ``--compare`` (default) it runs taker AND maker so
    you can see how much of the edge the spread is eating — the central question
    for this book. Places no orders; purely offline analysis.

    ``--params 'lookback_bars=7'`` overrides the agent's config for a sweep.
    """
    from ..backtest.baskets import resolve_basket
    from ..backtest.data import cached_or_fetch, load_frames
    from ..backtest.engine import Backtester, CostModel

    _, s = _conn()
    coin_list = resolve_basket(coins)
    try:
        cfg = _parse_agent_params(params)
    except ValueError as e:
        console.print(f"[red]bad --params: {e}[/red]")
        raise typer.Exit(1) from e

    factories = {
        "twap_mr_v1": lambda conn: TwapMrAgent(config=cfg, conn=conn),
        "twap_mr_regime_v1": lambda conn: TwapMrRegimeAgent(config=cfg, conn=conn),
        "femr_v1": lambda conn: FemrAgent(config=cfg, conn=conn),
        "funding_carry_v1": lambda conn: FundingCarryAgent(config=cfg, conn=conn),
        "xfund_carry_v1": lambda conn: XFundCarryAgent(config=cfg, conn=conn),
        "xsect_momentum_v1": lambda conn: XSectMomentumAgent(config=cfg, conn=conn),
        "ts_momentum_v1": lambda conn: TsMomentumAgent(config=cfg, conn=conn),
        "pairs_reversion_v1": lambda conn: PairsReversionAgent(config=cfg, conn=conn),
        "liq_cascade_v1": lambda conn: LiqCascadeAgent(config=cfg, conn=conn),
        "basis_v1": lambda conn: BasisAgent(config=cfg, conn=conn),
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
        from ..backtest.engine import _curve_stats
        sh, dd, _ = _curve_stats(res.equity_curve, periods_per_year=per_year)
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


def _parse_agent_params(params: str) -> dict[str, object]:
    """Parse a ``key=value,key=value`` CLI string into an agent-config override dict.

    The ``confirm``/``backtest`` factories otherwise hardcode ``config={}``, so a
    parameter sweep (e.g. the 1d ``lookback_bars`` sweep the majors-momentum lead
    needs, B-horizon) meant editing code. This makes it a flag. Values are typed by
    best-effort inference: ``int`` → ``float`` → ``bool`` (true/false) → ``str``, so
    ``lookback_bars=7`` is an int, ``enter_return=0.05`` a float, ``reversion=true`` a
    bool. Pure so the parsing is unit-tested without the network/agents.
    """
    out: dict[str, object] = {}
    for pair in params.split(","):
        pair = pair.strip()
        if not pair:
            continue
        if "=" not in pair:
            raise ValueError(f"bad param {pair!r}; expected key=value")
        key, _, raw = pair.partition("=")
        key, raw = key.strip(), raw.strip()
        if not key:
            raise ValueError(f"bad param {pair!r}; empty key")
        out[key] = _coerce_param(raw)
    return out


def _coerce_param(raw: str) -> object:
    """Infer int → float → bool → str for a single CLI param value."""
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        pass
    low = raw.lower()
    if low in ("true", "false"):
        return low == "true"
    return raw


def _window_specs(windows: int, days: int, now_ms: int) -> list[tuple[str, int | None]]:
    """Disjoint, back-to-back ``days``-long window specs for out-of-time durability.

    Returns ``[(label, end_ms), ...]`` newest-first: window 0 is the trailing
    window (``end_ms=None`` → now), window i ends ``i*days`` days before now so the
    windows abut without overlapping. Pure so the CLI's window math is unit-tested.
    """
    specs: list[tuple[str, int | None]] = []
    for i in range(windows):
        end_ms = None if i == 0 else now_ms - i * days * 86_400_000
        label = f"trailing {days}d" if i == 0 else f"{days}d ending {i * days}d ago"
        specs.append((label, end_ms))
    return specs


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
    windows: int = 1,
    params: str = "",
):
    """Confirm a strategy through the G0 gate: walk-forward + cost stress.

    Prints an explicit PASS/FAIL. A strategy must clear this on real history
    before it is eligible for paper→live promotion (see docs/GO_LIVE.md).

    ``--windows N`` (N>=2) raises the bar to the **out-of-time durability** test:
    it runs the confirmation on N disjoint, back-to-back ``days``-long windows
    (trailing + N-1 older ones) and emits a single DURABLE / NOT DURABLE verdict.
    A trailing-only PASS that reverses sign on an earlier window is a
    window-specific artifact, not an edge (see Iteration 20/21) — this catches it.

    ``--params 'lookback_bars=7,enter_return=0.05'`` overrides the candidate's
    config so a parameter sweep (e.g. a horizon-appropriate lookback) needs no code
    edit.
    """
    import time as _time

    from ..backtest.baskets import resolve_basket
    from ..backtest.confirm import confirm_across_windows, confirm_strategy
    from ..backtest.data import cached_or_fetch, load_frames

    _, s = _conn()
    coin_list = resolve_basket(coins)
    try:
        cfg = _parse_agent_params(params)
    except ValueError as e:
        console.print(f"[red]bad --params: {e}[/red]")
        raise typer.Exit(1) from e
    factories = {
        "twap_mr_v1": lambda conn: TwapMrAgent(config=cfg, conn=conn),
        "twap_mr_regime_v1": lambda conn: TwapMrRegimeAgent(config=cfg, conn=conn),
        "femr_v1": lambda conn: FemrAgent(config=cfg, conn=conn),
        "funding_carry_v1": lambda conn: FundingCarryAgent(config=cfg, conn=conn),
        "xfund_carry_v1": lambda conn: XFundCarryAgent(config=cfg, conn=conn),
        "xsect_momentum_v1": lambda conn: XSectMomentumAgent(config=cfg, conn=conn),
        "ts_momentum_v1": lambda conn: TsMomentumAgent(config=cfg, conn=conn),
        "pairs_reversion_v1": lambda conn: PairsReversionAgent(config=cfg, conn=conn),
        "liq_cascade_v1": lambda conn: LiqCascadeAgent(config=cfg, conn=conn),
        "basis_v1": lambda conn: BasisAgent(config=cfg, conn=conn),
    }
    if agent not in factories:
        console.print(f"[red]unknown agent {agent}; choose from {list(factories)}[/red]")
        raise typer.Exit(1)
    per_year = {"1m": 525_600, "5m": 105_120, "15m": 35_040,
                "1h": 8_760, "4h": 2_190, "1d": 365}.get(interval, 8_760)

    def _load(end_ms: int | None) -> list:
        if cache:
            return cached_or_fetch(coin_list, interval=interval, days=days,
                                   base_url=s.hl_api_url, end_ms=end_ms)
        return load_frames(coin_list, interval=interval, days=days,
                           base_url=s.hl_api_url, end_ms=end_ms)

    if windows >= 2:
        specs = _window_specs(windows, days, int(_time.time() * 1000))
        win_frames: list[tuple[str, list]] = []
        try:
            for label, end_ms in specs:
                win_frames.append((label, _load(end_ms)))
        except Exception as e:  # noqa: BLE001
            console.print(f"[red]failed to load history: {e}[/red]")
            raise typer.Exit(2) from e
        mw = confirm_across_windows(
            factories[agent], win_frames, prefer=prefer,
            min_edge_bps=min_edge_bps, min_sharpe=min_sharpe, periods_per_year=per_year,
        )
        console.print(mw.summary())
        if not mw.durable:
            raise typer.Exit(1)
        return

    try:
        frames = _load(None)
    except Exception as e:  # noqa: BLE001
        console.print(f"[red]failed to load history: {e}[/red]")
        raise typer.Exit(2) from e
    res = confirm_strategy(
        factories[agent], frames, prefer=prefer,
        min_edge_bps=min_edge_bps, min_sharpe=min_sharpe, periods_per_year=per_year,
    )
    console.print(res.summary())
    if not res.confirmed:
        raise typer.Exit(1)


@app.command()
def ws(
    coins: str = "BTC,ETH,SOL,HYPE",
    snapshot: Path = Path("data/ws_snapshot.json"),
    seconds: float = 0.0,
):
    """Run the WebSocket market-data service: maintain live state, write a snapshot.

    Long-running (supervise via systemd). The tick reads the snapshot when fresh
    (set HLBOT_WS_SNAPSHOT) for sub-second mids, L2 depth, and live liquidations.
    seconds=0 runs forever.
    """
    from ..ingest.ws import run_ws

    _, s = _conn()
    coin_list = [c.strip() for c in coins.split(",") if c.strip()]
    console.print(f"[green]ws[/green] subscribing {coin_list} → {snapshot} (every 1s)")
    run_ws(coin_list, snapshot, base_url=s.hl_api_url, duration_s=(seconds or None))


@app.command()
def health(max_tick_age_s: int = 900, heartbeat: bool = True):
    """Assess bot health (tick/ingest freshness, equity, paused agents, 24h PnL).

    Pings HEALTHCHECK_URL when healthy (dead-man switch) and Telegram-alerts when
    not. Designed to run on a timer alongside the tick.
    """
    import os

    from ..ops.health import assess_health, ping_heartbeat

    conn, s = _conn()
    rep = assess_health(conn, max_tick_age_s=max_tick_age_s)
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
    jp, mp = export(conn, out)
    console.print(mp.read_text())
    console.print(f"[green]✓[/green] wrote {jp} and {mp}")


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
