"""Build and (optionally) send a daily report to Telegram."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from pathlib import Path

import httpx

from ..config import CONFIG_DIR
from ..scoring.metrics import Scorecard, score_all
from ..supervisor.goals import GateProgress, load_goals, promotion_progress


def _fmt(v: float | None, fmt: str = "{:+.2f}") -> str:
    return "—" if v is None else fmt.format(v)


def render_markdown(cards: Iterable[Scorecard]) -> str:
    """Group by agent, show 24h / 7d / 30d / all in a compact form."""
    by_agent: dict[str, dict[str, Scorecard]] = {}
    for c in cards:
        by_agent.setdefault(c.agent, {})[c.window] = c

    lines: list[str] = ["## HL bot daily report", ""]
    for agent, windows in sorted(by_agent.items()):
        lines.append(f"### {agent}")
        for w in ("24h", "7d", "30d", "all"):
            c = windows.get(w)
            if not c:
                continue
            edge = _fmt(c.edge_bps, "{:+.1f} bps") if c.edge_bps is not None else "—"
            sharpe = _fmt(c.sharpe, "{:+.2f}")
            dd = _fmt(c.max_drawdown * 100 if c.max_drawdown is not None else None, "{:+.1f}%")
            lines.append(
                f"- **{w}**: net `{c.net_pnl:+.2f}` · trades `{c.n_trades}` · "
                f"win `{c.win_rate*100:.0f}%` · edge `{edge}` · "
                f"sharpe `{sharpe}` · max DD `{dd}`"
            )
        lines.append("")
    return "\n".join(lines)


_STATUS_MARK = {"pass": "✓", "fail": "✗", "na": "N/A"}


def render_gate_progress(reports: Iterable[GateProgress]) -> str:
    """Render the distance-to-gate section (e.g. the trend_breakout G1 clock).

    One block per agent with a promotion gate, listing each condition's current
    value vs threshold and whether it's met — so the daily digest shows how close
    paper agents are to their next (human-gated) promotion. Empty string when no
    agent has a promotion gate, so the caller can skip the section entirely.
    """
    reports = list(reports)
    if not reports:
        return ""
    lines: list[str] = ["## Gate progress", ""]
    for gp in reports:
        head = "READY" if gp.ready else f"{gp.n_met}/{gp.n_total} met"
        basis = " — paper-sim forward-test" if gp.simulated else ""
        lines.append(
            f"### {gp.agent} — {gp.from_mode} → {gp.to_mode} ({head}){basis}"
        )
        for c in gp.conditions:
            mark = _STATUS_MARK[c.status]
            val = _fmt(c.value)
            lines.append(
                f"- {mark} `{c.metric}({c.window}) {c.op} {c.threshold:g}` "
                f"→ `{val}`"
            )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def gate_progress_reports(
    conn: sqlite3.Connection, configs: Path | None = None
) -> list[GateProgress]:
    """Load every agent config and compute its promotion-gate progress.

    Agents without a promotion block are skipped (``promotion_progress`` → None).
    Read-only; reuses the same scoring the supervisor promotes on.
    """
    goals = []
    for p in sorted(Path(configs or CONFIG_DIR).glob("*.yaml")):
        goals.extend(load_goals(p))
    return [gp for g in goals if (gp := promotion_progress(conn, g))]


def build(conn: sqlite3.Connection, configs: Path | None = None) -> str:
    cards = score_all(conn, windows=["24h", "7d", "30d", "all"])
    md = render_markdown(cards)
    gates = render_gate_progress(gate_progress_reports(conn, configs))
    if gates:
        md = f"{md}\n{gates}"
    return md


def send_telegram(text: str, bot_token: str, chat_id: str) -> None:
    httpx.post(
        f"https://api.telegram.org/bot{bot_token}/sendMessage",
        json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"},
        timeout=15,
    ).raise_for_status()
