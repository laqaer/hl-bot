#!/usr/bin/env -S uv run python
"""Daily scorecard: per-agent PnL, edge, rolling Sharpe → Telegram.

Runs from systemd at 13:00 UTC = 8am Central = 6am PT.
Reads ~/hl-bot/data/hlbot.sqlite. Sends to TG via env-loaded token.
"""
from __future__ import annotations

import json
import math
import os
import sqlite3
import sys
import time
import urllib.request
from pathlib import Path

DB = Path(os.environ.get("HLBOT_DB", Path.home() / "hl-bot" / "data" / "hlbot.sqlite"))
AGENTS = ["femr_v1", "twap_mr_v1", "twap_mr_regime_v1", "liq_cascade_v1", "basis_v1"]


def _windows(days: int) -> int:
    return int((time.time() - days * 86400) * 1000)


def per_agent(conn, agent: str, days: int) -> dict:
    cutoff = _windows(days)
    fills = conn.execute(
        "SELECT time_ms, coin, sz, px, closed_pnl, fee FROM fills "
        "WHERE time_ms >= ? AND agent = ? ORDER BY time_ms",
        (cutoff, agent),
    ).fetchall()
    n = len(fills)
    if n == 0:
        return {"agent": agent, "days": days, "n": 0, "net": 0.0, "edge_bps": None,
                "sharpe": None, "by_coin": {}}
    pnl = sum(float(f[4] or 0) for f in fills)
    fees = sum(float(f[5] or 0) for f in fills)
    notional = sum(abs(float(f[2] or 0) * float(f[3] or 0)) for f in fills)
    net = pnl - fees
    edge_bps = (net / notional * 10_000) if notional > 0 else None

    # Daily returns for Sharpe
    daily: dict[int, float] = {}
    for ts, _, _, _, p, fee in fills:
        day = int(ts // 86_400_000)
        daily[day] = daily.get(day, 0) + float(p or 0) - float(fee or 0)
    if len(daily) >= 3:
        rets = list(daily.values())
        mean = sum(rets) / len(rets)
        var = sum((r - mean) ** 2 for r in rets) / len(rets)
        sd = math.sqrt(var) if var > 0 else 0
        sharpe = (mean / sd * math.sqrt(365)) if sd > 0 else None
    else:
        sharpe = None

    by_coin: dict[str, dict] = {}
    for _, coin, _sz, _px, p, fee in fills:
        c = by_coin.setdefault(coin, {"n": 0, "net": 0.0})
        c["n"] += 1
        c["net"] += float(p or 0) - float(fee or 0)

    return {"agent": agent, "days": days, "n": n, "net": net,
            "edge_bps": edge_bps, "sharpe": sharpe, "by_coin": by_coin}


def acct_value() -> float:
    """Read live spot+perp from HL. Returns 0.0 when no address is configured
    (no hardcoded default — never report a stranger's account as ours)."""
    addr = os.environ.get("HL_TRADER_ADDRESS") or os.environ.get("HL_ADDRESS")
    if not addr:
        return 0.0
    try:
        with urllib.request.urlopen(urllib.request.Request(
            "https://api.hyperliquid.xyz/info",
            data=json.dumps({"type": "clearinghouseState", "user": addr}).encode(),
            headers={"Content-Type": "application/json"},
        ), timeout=10) as r:
            d = json.loads(r.read())
        perp = float(d.get("marginSummary", {}).get("accountValue", 0) or 0)
    except Exception:
        perp = 0.0
    try:
        with urllib.request.urlopen(urllib.request.Request(
            "https://api.hyperliquid.xyz/info",
            data=json.dumps({"type": "spotClearinghouseState", "user": addr}).encode(),
            headers={"Content-Type": "application/json"},
        ), timeout=10) as r:
            sp = json.loads(r.read())
        spot = next(
            (float(b.get("total", 0) or 0) for b in sp.get("balances", [])
             if b.get("coin") == "USDC"), 0.0,
        )
    except Exception:
        spot = 0.0
    return perp + spot


def fmt(v, prec=2) -> str:
    if v is None:
        return "—"
    return f"{v:+.{prec}f}"


def build_report() -> str:
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    capital = acct_value()
    lines = [f"🤖 *hl-bot scorecard* — {time.strftime('%Y-%m-%d')}",
             f"capital: ${capital:.2f}", ""]
    totals = {"net_24h": 0.0, "net_7d": 0.0}
    for ag in AGENTS:
        s24 = per_agent(conn, ag, 1)
        s7 = per_agent(conn, ag, 7)
        totals["net_24h"] += s24["net"]
        totals["net_7d"] += s7["net"]
        lines.append(f"*{ag}*")
        lines.append(f"  24h: {s24['n']} trades, ${fmt(s24['net'])} net, "
                     f"edge {fmt(s24['edge_bps'], 1)} bps")
        lines.append(f"  7d:  {s7['n']} trades, ${fmt(s7['net'])} net, "
                     f"edge {fmt(s7['edge_bps'], 1)} bps, "
                     f"sharpe {fmt(s7['sharpe'], 2)}")
        if s24["by_coin"]:
            top = sorted(s24["by_coin"].items(), key=lambda kv: kv[1]["net"], reverse=True)[:3]
            lines.append("  best 24h: " + ", ".join(
                f"{c} ${v['net']:+.2f}" for c, v in top))
        lines.append("")
    lines.append(f"*total* 24h: ${fmt(totals['net_24h'])} · "
                 f"7d: ${fmt(totals['net_7d'])}")
    return "\n".join(lines)


def send_telegram(text: str) -> None:
    token = os.environ.get("TG_BOT_TOKEN")
    chat = os.environ.get("TG_CHAT_ID", "8588356687")
    if not token:
        print(text)
        print("\n(TG_BOT_TOKEN not set, dry-run only)", file=sys.stderr)
        return
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=json.dumps({"chat_id": chat, "text": text, "parse_mode": "Markdown"}).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        r.read()
    print("sent", file=sys.stderr)


if __name__ == "__main__":
    # If no TG_BOT_TOKEN, print to stdout — useful for Hermes cron (deliver: telegram).
    text = build_report()
    if os.environ.get("TG_BOT_TOKEN"):
        send_telegram(text)
    else:
        print(text)
