"""CLI entrypoint: `hlbot ...`"""

from __future__ import annotations

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
from ..agents.liq_cascade import LiqCascadeAgent
from ..agents.meta_allocator import MetaAllocator, MetaAllocatorConfig
from ..agents.runtime import run_tick
from ..agents.twap_mr import TwapMrAgent
from ..agents.veto import VetoAgent
from ..config import CONFIG_DIR, Settings
from ..db.schema import init_db
from ..ingest.hyperliquid import ingest_fills, ingest_funding, snapshot_equity
from ..reports.daily import build as build_report
from ..reports.daily import send_telegram
from ..scoring.metrics import score_all
from ..supervisor.loop import supervise

app = typer.Typer(no_args_is_help=True, add_completion=False)
console = Console()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def _conn():
    s = Settings.from_env()
    return init_db(s.db_path), s


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
    """Augment a MarketView with 1h candles (top-vol coins), spot mids, liquidations."""
    import httpx as _httpx

    # ---- top-20-by-volume universe ----
    top = sorted(vol.items(), key=lambda kv: kv[1], reverse=True)[:20]
    top_coins = [c for c, _ in top]

    candles_1h: dict[str, dict] = {}
    spot_mids: dict[str, float] = {}
    liquidations: list[dict] = []

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
                        c_px = float(k.get("c", 0)); c_vol = float(k.get("v", 0))
                        if c_px > 0:
                            pxs.append(c_px); vols.append(c_vol)
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
                meta = spot[0] or {}; ctxs = spot[1] or []
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
                    if perp_mid and scaled_mid > 0 and 0.5 < scaled_mid / perp_mid < 1.5:
                        # Prefer wrapped (U-prefixed) over plain if both present.
                        if base_name.startswith("U") or norm not in spot_mids:
                            spot_mids[norm] = scaled_mid
        except Exception:  # noqa: BLE001
            pass

        # recent liquidations (best-effort; endpoint may not exist publicly)
        try:
            ev = cli.post(api_url + "/info", json={"type": "liquidations"}).json()
            if isinstance(ev, list):
                for e in ev:
                    try:
                        coin = e.get("coin")
                        sz = float(e.get("sz") or 0); px = float(e.get("px") or 0)
                        if coin and sz > 0 and px > 0:
                            liquidations.append({
                                "coin": coin,
                                "side": e.get("side"),
                                "notional_usd": sz * px,
                                "ts_ms": int(e.get("time") or 0),
                            })
                    except (TypeError, ValueError):
                        continue
        except Exception:  # noqa: BLE001
            pass

    view.extra["candles_1h"] = candles_1h
    view.extra["spot_mids"] = spot_mids
    view.extra["liquidations"] = liquidations


@app.command()
def femr_tick(live: bool = False):
    """Run FEMR (Funding Extremes Mean Reversion) one tick.

    paper (default): log decisions only, no orders placed.
    live: place real orders on MAIN account, gated by guardrails.
          Bot only touches positions it itself opened (cloid-tagged).
    """
    from ..agents.runtime import fetch_market_view
    from ..exec.orders import (
        HL_TRADER_ADDRESS,
        GuardrailConfig,
        bot_owned_coins,
        build_exchange,
        check_guardrails,
        close_position,
        coin_in_cooldown,
        place_market_order,
        reconcile_positions,
        telegram_alert,
    )
    from ..agents.decisions import Decision, log_decision

    conn, s = _conn()

    # Instantiate the full agent roster
    agents = [
        FemrAgent(config={
            "max_notional_per_trade": 20.0,
            "max_total_notional": 40.0,
            "funding_enter_per_hr": 0.00015,
            "funding_exit_per_hr": 0.00005,
        }, conn=conn),
        TwapMrAgent(conn=conn),
        LiqCascadeAgent(conn=conn),
        BasisAgent(conn=conn),
    ]

    # Allocator: rebalance per-agent caps from rolling 7d Sharpe
    allocator = MetaAllocator([a.name for a in agents],
                              MetaAllocatorConfig(total_capital=300.0))
    allocs = allocator.allocate(conn)
    for a in agents:
        cap = allocs.get(a.name, 50.0)
        if hasattr(a, "cfg") and hasattr(a.cfg, "max_total_notional"):
            a.cfg.max_total_notional = cap
    console.print("[bold]allocator caps[/bold]: " +
                  ", ".join(f"{n}=${v:.0f}" for n, v in allocs.items()))

    view = fetch_market_view(s.hl_api_url, [])
    _enrich_view(view, s.hl_api_url, view.extra.get("day_ntl_vlm", {}))

    import httpx as _httpx
    with _httpx.Client(timeout=10) as cli:
        st = cli.post(s.hl_api_url + "/info",
                      json={"type": "clearinghouseState", "user": HL_TRADER_ADDRESS}).json() or {}

    # Build position list from HL truth
    all_positions = []
    for ap in st.get("assetPositions", []) or []:
        pos = ap.get("position", {}) or {}
        try:
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
        except (TypeError, ValueError):
            pass

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

    acct_val = float((st.get("marginSummary") or {}).get("accountValue", 0) or 0)
    withdrawable = float(st.get("withdrawable", 0) or 0)
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

    all_decisions = []
    for agent in agents:
        decisions = agent.decide(view)
        for d in decisions:
            d.is_paper = not live
            # Skip logging "hold" decisions to keep audit log clean
            if d.action != "hold":
                log_decision(conn, d)
            all_decisions.append(d)

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
        raise typer.Exit(2)

    ok, why = check_guardrails(conn, info, GuardrailConfig(min_bot_capital=40.0))
    if not ok:
        console.print(f"[red]HALT[/red]: {why}")
        return
    console.print(f"[green]guardrails[/green]: {why}")

    # Execute
    agent_names = {a.name for a in agents}
    for d in all_decisions:
        if d.agent not in agent_names or d.coin is None:
            continue

        if d.action == "place" and d.sz and d.side:
            if coin_in_cooldown(conn, d.coin, agent=d.agent):
                console.print(f"[dim]SKIP {d.agent} {d.coin}: in cooldown[/dim]")
                continue
            is_buy = (d.side == "B")
            res = place_market_order(exchange, d.coin, is_buy, d.sz,
                                     slippage_pct=0.01, cloid=d.cloid)
            if res.ok:
                console.print(f"[bold green]FILLED[/bold green] {d.coin} {'BUY' if is_buy else 'SELL'} {res.filled_sz} @ ${res.avg_px}")
                # `place` decision was already logged by the agent loop above.
            else:
                # Rewrite as 'rejected' so it doesn't pollute ownership state
                console.print(f"[red]REJECT[/red] {d.coin}: {res.status} — {res.error}")
                # Log a separate audit row for visibility
                log_decision(conn, Decision(
                    agent=d.agent, action="rejected", coin=d.coin,
                    reasoning=f"HL rejected: {res.error}", is_paper=False,
                ))
                # Important: remove the place row that the agent loop wrote
                conn.execute(
                    """DELETE FROM agent_decisions
                       WHERE agent=? AND coin=? AND action='place' AND ts_ms >= ?""",
                    (d.agent, d.coin, int((time.time() - 60) * 1000)),
                )
                conn.commit()

        elif d.action == "flatten":
            res = close_position(exchange, d.coin, cloid=d.cloid)
            if res.ok:
                console.print(f"[bold]CLOSED[/bold] {d.coin} @ ${res.avg_px}")
            else:
                console.print(f"[red]CLOSE FAILED[/red] {d.coin}: {res.error}")


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
