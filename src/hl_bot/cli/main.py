"""CLI entrypoint: `hlbot ...`"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from ..agents.femr import FemrAgent
from ..agents.funding_arb import FundingArbAgent
from ..agents.runtime import run_tick
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
    agents = [
        FemrAgent(config={
            "max_notional_per_trade": 20.0,
            "max_total_notional": 40.0,
            "funding_enter_per_hr": 0.00015,
            "funding_exit_per_hr": 0.00005,
        }, conn=conn),
    ]

    view = fetch_market_view(s.hl_api_url, [])

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

    # RECONCILE first — clear stale DB ownership for positions HL no longer shows
    reconciled = reconcile_positions(conn, all_positions)
    if reconciled:
        console.print(f"[yellow]reconciled stale ownership: {reconciled}[/yellow]")

    owned = bot_owned_coins(conn)
    bot_positions = [p for p in all_positions if p["coin"] in owned]
    view.extra["live_positions"] = bot_positions

    acct_val = float((st.get("marginSummary") or {}).get("accountValue", 0) or 0)
    withdrawable = float(st.get("withdrawable", 0) or 0)
    manual_coins = [p["coin"] for p in all_positions if p["coin"] not in owned]
    console.print(
        f"[dim]market: {len(view.mids)} coins, {len(view.funding)} funding · "
        f"acct ${acct_val:.2f}, free ${withdrawable:.2f} · "
        f"bot-owned: {sorted(owned) or '∅'} · manual: {manual_coins or '∅'}[/dim]"
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
        if d.action != "hold":
            console.print(f"  {d.agent} {d.action} {d.coin or ''} :: {d.reasoning}")

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
    for d in all_decisions:
        if d.agent != "femr_v1" or d.coin is None:
            continue

        if d.action == "place" and d.sz and d.side:
            if coin_in_cooldown(conn, d.coin):
                console.print(f"[dim]SKIP {d.coin}: in cooldown[/dim]")
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
