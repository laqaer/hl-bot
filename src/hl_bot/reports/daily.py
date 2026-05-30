"""Build and (optionally) send a daily report to Telegram."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable

import httpx

from ..scoring.metrics import Scorecard, score_all


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


def build(conn: sqlite3.Connection) -> str:
    cards = score_all(conn, windows=["24h", "7d", "30d", "all"])
    return render_markdown(cards)


def send_telegram(text: str, bot_token: str, chat_id: str) -> None:
    httpx.post(
        f"https://api.telegram.org/bot{bot_token}/sendMessage",
        json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"},
        timeout=15,
    ).raise_for_status()
