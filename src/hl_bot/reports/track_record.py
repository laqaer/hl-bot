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

from ..scoring.metrics import list_agents, score_agent


def _account_equity_curve(conn: sqlite3.Connection) -> list[tuple[int, float]]:
    rows = conn.execute(
        "SELECT ts_ms, account_value FROM equity_snapshots "
        "WHERE account_value > 0 ORDER BY ts_ms ASC"
    ).fetchall()
    return [(int(r[0]), float(r[1])) for r in rows]


def _agent_daily_pnl(conn: sqlite3.Connection, agent: str) -> list[float]:
    rows = conn.execute(
        "SELECT time_ms, COALESCE(closed_pnl,0) - COALESCE(fee,0) AS net "
        "FROM fills WHERE agent = ? ORDER BY time_ms ASC",
        (agent,),
    ).fetchall()
    buckets: dict[int, float] = {}
    for r in rows:
        day = int(r[0] // 86_400_000)
        buckets[day] = buckets.get(day, 0.0) + float(r[1])
    if not buckets:
        return []
    lo, hi = min(buckets), max(buckets)
    return [buckets.get(d, 0.0) for d in range(lo, hi + 1)]


def _daily_sharpe(daily: list[float]) -> float | None:
    if len(daily) < 3:
        return None
    mean = sum(daily) / len(daily)
    var = sum((x - mean) ** 2 for x in daily) / len(daily)
    std = math.sqrt(var)
    return (mean / std * math.sqrt(365)) if std > 0 else None


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
            # Single source of truth: the same dollar drawdown the supervisor gates on.
            "max_drawdown_usd": all_sc.max_drawdown_usd,
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
    if a.get("n_snapshots", 0):
        lines.append("")
        lines.append("![equity curve](track_record.svg)")
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


def equity_curve_svg(
    curve: list[tuple[int, float]],
    *,
    width: int = 640,
    height: int = 240,
    pad: int = 36,
) -> str:
    """Render the account equity curve as a self-contained SVG (no deps).

    A vault depositor reads the Markdown record but *sees* the equity curve; a
    pure-Python SVG keeps the chart export dependency-free (no matplotlib) and
    unit-testable. Returns a complete ``<svg>…</svg>`` string. Handles the empty
    and single-point cases (placeholder / flat line) so it never raises.
    """
    w, h = int(width), int(height)
    x0, y0 = pad, pad
    x1, y1 = w - pad, h - pad
    header = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" '
        f'viewBox="0 0 {w} {h}" font-family="monospace" font-size="11">'
    )
    bg = f'<rect width="{w}" height="{h}" fill="#ffffff"/>'
    frame = (
        f'<rect x="{x0}" y="{y0}" width="{x1 - x0}" height="{y1 - y0}" '
        f'fill="none" stroke="#cccccc"/>'
    )
    title = f'<text x="{x0}" y="{y0 - 12}" fill="#333333">hl-bot account equity</text>'

    if not curve:
        empty = (
            f'<text x="{w // 2}" y="{h // 2}" fill="#999999" '
            f'text-anchor="middle">no equity snapshots</text>'
        )
        return header + bg + frame + title + empty + "</svg>"

    ts = [p[0] for p in curve]
    vals = [p[1] for p in curve]
    tmin, tmax = min(ts), max(ts)
    vmin, vmax = min(vals), max(vals)
    tspan = (tmax - tmin) or 1
    vspan = (vmax - vmin) or 1.0

    def sx(t: int) -> float:
        return x0 + (t - tmin) / tspan * (x1 - x0)

    def sy(v: float) -> float:
        # invert: larger value → higher on screen (smaller y)
        return y1 - (v - vmin) / vspan * (y1 - y0)

    pts = " ".join(f"{sx(t):.1f},{sy(v):.1f}" for t, v in curve)
    line = (
        f'<polyline points="{pts}" fill="none" stroke="#1a7f37" '
        f'stroke-width="2"/>'
    )
    # mark the last point so a flat single-point curve is still visible
    last = f'<circle cx="{sx(ts[-1]):.1f}" cy="{sy(vals[-1]):.1f}" r="3" fill="#1a7f37"/>'
    labels = (
        f'<text x="{x0 + 2}" y="{y0 + 12}" fill="#666666">${vmax:,.2f}</text>'
        f'<text x="{x0 + 2}" y="{y1 - 4}" fill="#666666">${vmin:,.2f}</text>'
        f'<text x="{x1}" y="{y1 + 16}" fill="#666666" text-anchor="end">'
        f"{_date(tmax)}</text>"
        f'<text x="{x0}" y="{y1 + 16}" fill="#666666">{_date(tmin)}</text>'
    )
    return header + bg + frame + title + line + last + labels + "</svg>"


def _date(ms: int) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(ms / 1000))


def _num(v: float | None) -> str:
    return "—" if v is None else f"{v:+.2f}"


def _pct(v: float | None) -> str:
    return "—" if v is None else f"{v*100:+.1f}%"


def _bps(v: float | None) -> str:
    return "—" if v is None else f"{v:+.0f}bps"


def _money(v: float | None) -> str:
    return "—" if v is None else f"${v:+.2f}"


def export(conn: sqlite3.Connection, out_dir: str | Path) -> tuple[Path, Path, Path]:
    """Write track_record.{json,md,svg}; return their paths.

    The SVG is the equity-curve chart (dependency-free) that the Markdown record
    references — what an allocator looks at first.
    """
    track = build_track_record(conn)
    d = Path(out_dir)
    d.mkdir(parents=True, exist_ok=True)
    jp = d / "track_record.json"
    mp = d / "track_record.md"
    sp = d / "track_record.svg"
    jp.write_text(json.dumps(track, indent=2))
    mp.write_text(to_markdown(track))
    sp.write_text(equity_curve_svg(track["equity_curve"]))
    return jp, mp, sp
