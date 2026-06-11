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


def _equity_svg(curve: list[tuple[int, float]], width: int = 820, height: int = 240,
                pad: int = 30) -> str:
    """Inline SVG line chart of the equity curve — no external deps, shareable."""
    vals = [v for _, v in curve]
    if len(vals) < 2:
        return '<p class="muted">(equity curve needs ≥2 snapshots)</p>'
    lo, hi = min(vals), max(vals)
    rng = (hi - lo) or 1.0
    n = len(vals)

    def x(i: int) -> float:
        return pad + (width - 2 * pad) * i / (n - 1)

    def y(v: float) -> float:
        return pad + (height - 2 * pad) * (1 - (v - lo) / rng)

    pts = " ".join(f"{x(i):.1f},{y(v):.1f}" for i, v in enumerate(vals))
    color = "#0a8a0a" if vals[-1] >= vals[0] else "#c0392b"
    return (
        f'<svg viewBox="0 0 {width} {height}" width="100%" '
        f'style="border:1px solid #eee;background:#fafafa">'
        f'<polyline fill="none" stroke="{color}" stroke-width="2" points="{pts}"/>'
        f'<text x="{pad}" y="{pad-8}" font-size="11" fill="#777">${hi:,.2f}</text>'
        f'<text x="{pad}" y="{height-pad+16}" font-size="11" fill="#777">${lo:,.2f}</text>'
        '</svg>'
    )


def to_html(track: dict[str, Any]) -> str:
    """Self-contained HTML page (inline SVG equity chart + per-agent table).

    The shareable artifact for a vault depositor / allocator — opens in any browser
    with no dependencies. Built from the same numbers as the JSON/Markdown export.
    """
    a = track["account"]
    svg = _equity_svg(track.get("equity_curve") or [])
    gen = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime((track.get("generated_ms") or 0) / 1000))
    eq = ""
    if a.get("start_value") is not None:
        eq = (f"${a['start_value']:,.2f} → ${a['end_value']:,.2f} "
              f"({_pct(a.get('total_return_pct'))})")
    rows = "".join(
        f"<tr><td>{ag['agent']}</td><td>{ag['n_trades']}</td>"
        f"<td>${ag['net_pnl']:+.2f}</td><td>{_bps(ag['edge_bps'])}</td>"
        f"<td>{ag['win_rate']*100:.0f}%</td><td>{_num(ag['sharpe_daily'])}</td>"
        f"<td>{_money(ag['max_drawdown_usd'])}</td></tr>"
        for ag in track["agents"]
    )
    return (
        "<!doctype html><html><head><meta charset=\"utf-8\">"
        "<title>hl-bot track record</title><style>"
        "body{font-family:-apple-system,system-ui,Segoe UI,sans-serif;max-width:880px;"
        "margin:2rem auto;padding:0 1rem;color:#111}table{border-collapse:collapse;width:100%}"
        "td,th{padding:.4rem .6rem;border-bottom:1px solid #eee;text-align:right}"
        "td:first-child,th:first-child{text-align:left}.muted{color:#777;font-size:.9rem}"
        "h1{font-size:1.4rem;margin-bottom:.2rem}h2{font-size:1.05rem;margin-top:1.6rem}"
        "</style></head><body>"
        "<h1>hl-bot track record</h1>"
        f"<p class=\"muted\">generated {gen}</p>"
        "<h2>Account equity</h2>"
        f"<p><b>{eq}</b></p>{svg}"
        f"<p class=\"muted\">sharpe {_num(a.get('sharpe'))} · max DD "
        f"{_pct(a.get('max_drawdown_pct'))} · calmar {_num(a.get('calmar'))} · "
        f"net ${a.get('net_pnl', 0):+.2f} · {a.get('n_snapshots', 0)} snapshots</p>"
        "<h2>Per-agent</h2><table><thead><tr><th>agent</th><th>trades</th><th>net</th>"
        "<th>edge</th><th>win</th><th>sharpe(d)</th><th>maxDD$</th></tr></thead><tbody>"
        f"{rows or '<tr><td colspan=7 class=muted>no agent fills yet</td></tr>'}"
        "</tbody></table></body></html>"
    )


def export(conn: sqlite3.Connection, out_dir: str | Path) -> tuple[Path, Path, Path]:
    """Write track_record.{json,md,html}; return their paths."""
    track = build_track_record(conn)
    d = Path(out_dir)
    d.mkdir(parents=True, exist_ok=True)
    jp = d / "track_record.json"
    mp = d / "track_record.md"
    hp = d / "track_record.html"
    jp.write_text(json.dumps(track, indent=2))
    mp.write_text(to_markdown(track))
    hp.write_text(to_html(track))
    return jp, mp, hp
