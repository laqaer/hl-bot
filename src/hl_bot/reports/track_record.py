"""Track-record export — the artifact capital/AUM decisions are made on.

Turns the ground-truth tables into a shareable, auditable record: account equity
curve + per-agent net/edge/Sharpe/drawdown across standard windows, exported as
JSON (machine) and Markdown (human). This is the Path-C deliverable in
``docs/ROADMAP_TO_1M.md`` — a vault depositor or allocator needs exactly this
before committing capital, and it doubles as the evidence the supervisor's
go-live gates reference.

Everything is computed from the same tables and ``score_agent`` used live, so the
track record can never flatter the live numbers.
"""

from __future__ import annotations

import json
import math
import sqlite3
import time
from pathlib import Path
from typing import Any

from ..scoring.attribution import agent_pnl_events, daily_pnl_series
from ..scoring.metrics import list_agents, score_agent


def _account_equity_curve(conn: sqlite3.Connection) -> list[tuple[int, float]]:
    rows = conn.execute(
        "SELECT ts_ms, account_value FROM equity_snapshots "
        "WHERE account_value > 0 ORDER BY ts_ms ASC"
    ).fetchall()
    return [(int(r[0]), float(r[1])) for r in rows]


def _agent_daily_pnl(conn: sqlite3.Connection, agent: str) -> list[float]:
    # Fills net of fees PLUS the agent's attributed funding share — same inputs
    # as the live scorecard, so the public record can't disagree with it.
    return daily_pnl_series(agent_pnl_events(conn, agent))


def _daily_sharpe(daily: list[float]) -> float | None:
    if len(daily) < 3:
        return None
    mean = sum(daily) / len(daily)
    var = sum((x - mean) ** 2 for x in daily) / len(daily)
    std = math.sqrt(var)
    return (mean / std * math.sqrt(365)) if std > 0 else None


def _dollar_max_drawdown(daily: list[float]) -> float | None:
    """Largest peak-to-trough dollar drop of the cumulative PnL curve."""
    if not daily:
        return None
    cum = 0.0
    peak = 0.0
    max_dd = 0.0
    for x in daily:
        cum += x
        peak = max(peak, cum)
        max_dd = min(max_dd, cum - peak)
    return max_dd


def build_track_record(
    conn: sqlite3.Connection,
    *,
    now_ms: int | None = None,
) -> dict[str, Any]:
    now_ms = now_ms or int(time.time() * 1000)
    curve = _account_equity_curve(conn)
    acct = score_agent(conn, "_account", "all")
    account: dict[str, Any] = {
        "sharpe": acct.sharpe,
        "max_drawdown_pct": acct.max_drawdown,
        "calmar": acct.calmar,
        "net_pnl": acct.net_pnl,
        "n_snapshots": len(curve),
    }
    if curve:
        account.update({
            "start_value": curve[0][1],
            "end_value": curve[-1][1],
            "start_ms": curve[0][0],
            "end_ms": curve[-1][0],
            "total_return_pct": (curve[-1][1] / curve[0][1] - 1.0) if curve[0][1] else None,
        })

    agents = [
        a for a in list_agents(conn)
        if a not in ("_account", "manual") and not a.startswith("unknown:")
    ]
    per_agent: list[dict[str, Any]] = []
    for a in agents:
        all_sc = score_agent(conn, a, "all")
        daily = _agent_daily_pnl(conn, a)
        per_agent.append({
            "agent": a,
            "n_trades": all_sc.n_trades,
            "net_pnl": all_sc.net_pnl,
            "edge_bps": all_sc.edge_bps,
            "win_rate": all_sc.win_rate,
            "sharpe_daily": _daily_sharpe(daily),
            "max_drawdown_usd": _dollar_max_drawdown(daily),
            "windows": {
                w: score_agent(conn, a, w).as_dict()  # type: ignore[arg-type]
                for w in ("24h", "7d", "30d")
            },
        })

    return {
        "generated_ms": now_ms,
        "account": account,
        "equity_curve": curve,
        "agents": sorted(per_agent, key=lambda d: d["net_pnl"], reverse=True),
    }


def to_markdown(track: dict[str, Any]) -> str:
    a = track["account"]
    lines = ["# hl-bot track record", ""]
    lines.append("## Account")
    if a.get("start_value") is not None:
        lines.append(
            f"- equity: `${a['start_value']:.2f}` → `${a['end_value']:.2f}` "
            f"({_pct(a.get('total_return_pct'))})"
        )
    lines.append(
        f"- sharpe `{_num(a.get('sharpe'))}` · max DD `{_pct(a.get('max_drawdown_pct'))}` "
        f"· calmar `{_num(a.get('calmar'))}` · net `${a.get('net_pnl', 0):+.2f}` "
        f"· snapshots `{a.get('n_snapshots', 0)}`"
    )
    lines.append("")
    lines.append("## Agents")
    lines.append("| agent | trades | net | edge | win | sharpe(d) | maxDD$ |")
    lines.append("|---|--:|--:|--:|--:|--:|--:|")
    for ag in track["agents"]:
        lines.append(
            f"| {ag['agent']} | {ag['n_trades']} | ${ag['net_pnl']:+.2f} | "
            f"{_bps(ag['edge_bps'])} | {ag['win_rate']*100:.0f}% | "
            f"{_num(ag['sharpe_daily'])} | {_money(ag['max_drawdown_usd'])} |"
        )
    return "\n".join(lines)


def _num(v: float | None) -> str:
    return "—" if v is None else f"{v:+.2f}"


def _pct(v: float | None) -> str:
    return "—" if v is None else f"{v*100:+.1f}%"


def _bps(v: float | None) -> str:
    return "—" if v is None else f"{v:+.0f}bps"


def _money(v: float | None) -> str:
    return "—" if v is None else f"${v:+.2f}"


def equity_curve_svg(
    curve: list[tuple[int, float]],
    *,
    width: int = 720,
    height: int = 240,
    pad: int = 36,
) -> str:
    """Render the equity curve as a dependency-free SVG polyline.

    The chart depositors/allocators actually look at. Returns a placeholder
    SVG when there aren't enough points to draw a line.
    """
    if len(curve) < 2:
        return (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}">'
            f'<text x="{width//2}" y="{height//2}" text-anchor="middle" '
            f'font-family="sans-serif" fill="#888">not enough equity history yet</text></svg>'
        )
    ts = [t for t, _ in curve]
    vs = [v for _, v in curve]
    t0, t1 = min(ts), max(ts)
    lo, hi = min(vs), max(vs)
    spread = (hi - lo) or 1.0
    tspan = (t1 - t0) or 1

    def x(t: int) -> float:
        return pad + (t - t0) / tspan * (width - 2 * pad)

    def y(v: float) -> float:
        return height - pad - (v - lo) / spread * (height - 2 * pad)

    pts = " ".join(f"{x(t):.1f},{y(v):.1f}" for t, v in curve)
    up = vs[-1] >= vs[0]
    color = "#0a7f3f" if up else "#b00020"
    start_d = time.strftime("%Y-%m-%d", time.gmtime(t0 / 1000))
    end_d = time.strftime("%Y-%m-%d", time.gmtime(t1 / 1000))
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'font-family="sans-serif" font-size="11">'
        f'<rect width="{width}" height="{height}" fill="#fff"/>'
        f'<polyline points="{pts}" fill="none" stroke="{color}" stroke-width="1.5"/>'
        f'<text x="{pad}" y="{pad - 10}" fill="#444">account equity (USDC)</text>'
        f'<text x="{pad}" y="{height - 8}" fill="#888">{start_d}</text>'
        f'<text x="{width - pad}" y="{height - 8}" fill="#888" text-anchor="end">{end_d}</text>'
        f'<text x="{width - pad}" y="{pad - 10}" fill="{color}" text-anchor="end">'
        f'${vs[0]:.2f} → ${vs[-1]:.2f}</text>'
        f"</svg>"
    )


def to_html(track: dict[str, Any]) -> str:
    """One-page shareable HTML: equity chart + the markdown tables, no deps."""
    svg = equity_curve_svg(track.get("equity_curve") or [])
    gen = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(track["generated_ms"] / 1000))
    rows = "".join(
        f"<tr><td>{ag['agent']}</td><td>{ag['n_trades']}</td>"
        f"<td>${ag['net_pnl']:+.2f}</td><td>{_bps(ag['edge_bps'])}</td>"
        f"<td>{ag['win_rate']*100:.0f}%</td><td>{_num(ag['sharpe_daily'])}</td>"
        f"<td>{_money(ag['max_drawdown_usd'])}</td></tr>"
        for ag in track["agents"]
    )
    a = track["account"]
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>hl-bot track record</title>
<style>
 body {{ font-family: -apple-system, sans-serif; max-width: 780px; margin: 2em auto; color: #222; }}
 table {{ border-collapse: collapse; width: 100%; }}
 td, th {{ border: 1px solid #ddd; padding: 6px 10px; text-align: right; }}
 td:first-child, th:first-child {{ text-align: left; }}
 .meta {{ color: #777; font-size: 0.85em; }}
</style></head><body>
<h1>hl-bot track record</h1>
<p class="meta">generated {gen} · computed from exchange-reconciled fills, funding
and equity snapshots — the same tables the live supervisor uses.</p>
{svg}
<p>account: sharpe <b>{_num(a.get("sharpe"))}</b> · max DD <b>{_pct(a.get("max_drawdown_pct"))}</b>
 · net <b>${a.get("net_pnl", 0):+.2f}</b></p>
<h2>Per-agent (all-time)</h2>
<table><tr><th>agent</th><th>trades</th><th>net</th><th>edge</th><th>win</th>
<th>sharpe(d)</th><th>maxDD$</th></tr>{rows}</table>
</body></html>
"""


def export(
    conn: sqlite3.Connection, out_dir: str | Path
) -> tuple[Path, Path, Path, Path]:
    """Write track_record.{json,md,svg,html}; return their paths."""
    track = build_track_record(conn)
    d = Path(out_dir)
    d.mkdir(parents=True, exist_ok=True)
    jp = d / "track_record.json"
    mp = d / "track_record.md"
    sp = d / "track_record.svg"
    hp = d / "track_record.html"
    jp.write_text(json.dumps(track, indent=2))
    mp.write_text(to_markdown(track))
    sp.write_text(equity_curve_svg(track.get("equity_curve") or []))
    hp.write_text(to_html(track))
    return jp, mp, sp, hp
