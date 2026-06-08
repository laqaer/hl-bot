"""Auto-tuner: read 7d fills, ask Claude Code Opus 4.8 Max for tweaks, apply if safe.

Runs on demand / scheduled by Hermes cron. Reads the configured HLBOT_DB
(synced from EC2). Writes proposed/applied tweaks to:
  ~/projects/hl-bot/data/auto_tuner_log.jsonl
  ~/projects/hl-bot/configs/agent_overrides.json   (live params, picked up by CLI)

Safety rails:
- Param values clamped to per-key min/max
- Reject any proposal that 2x's max_total_notional or zeros stop_loss
- If <50 trades in past 7d for an agent, only allow tiny changes (≤20%)
- All changes deployable via git push; manual rollback by reverting the file
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

DB = Path(os.environ.get("HLBOT_DB", Path.home() / "projects/hl-bot/data/hlbot-aws.sqlite"))
OVERRIDES = Path.home() / "projects/hl-bot/configs/agent_overrides.json"
LOG_PATH = Path.home() / "projects/hl-bot/data/auto_tuner_log.jsonl"

# Per-key bounds (param: (min, max))
BOUNDS: dict[str, tuple[float, float]] = {
    "funding_enter_per_hr": (0.00005, 0.0010),     # 0.005% to 0.1%/hr
    "funding_exit_per_hr": (0.00001, 0.0005),
    "stop_loss_pct": (0.005, 0.04),                 # 0.5% to 4%
    "take_profit_pct": (0.003, 0.025),
    "max_hold_hours": (1.0, 24.0),
    "max_notional_per_trade": (10.0, 200.0),
    "min_daily_volume_usd": (1_000_000.0, 50_000_000.0),
    "sigma_enter": (1.5, 4.0),                       # twap_mr
    "sigma_exit": (0.25, 1.25),                      # twap_mr
    "liq_threshold_usd": (50_000.0, 500_000.0),
    "liq_window_s": (60, 900),
    "basis_enter_bps": (10, 80),
    "basis_exit_bps": (1, 20),
}

# Standing approval: scale TWAP only to $200/trade, keep FEMR small. The tuner
# may reduce risk on any strategy, but may only increase per-trade notional for
# TWAP, and never above this cap.
MAX_PER_TRADE_BY_AGENT = {
    "femr_v1": 20.0,
    "twap_mr_v1": 200.0,
    "liq_cascade_v1": 25.0,
    "basis_v1": 25.0,
}

AGENTS = ["femr_v1", "twap_mr_v1", "liq_cascade_v1", "basis_v1"]
CLAUDE_CODE_BIN = os.environ.get("CLAUDE_CODE_BIN", "claude")
CLAUDE_CODE_MODEL = os.environ.get("CLAUDE_CODE_MODEL", "claude-opus-4-8")
CLAUDE_CODE_EFFORT = os.environ.get("CLAUDE_CODE_EFFORT", "max")
CLAUDE_CODE_TIMEOUT_S = int(os.environ.get("CLAUDE_CODE_TIMEOUT_S", "300"))


def per_agent_summary(conn: sqlite3.Connection, agent: str) -> dict:
    cutoff = int((time.time() - 7 * 86400) * 1000)
    rows = conn.execute(
        "SELECT coin, sz, px, closed_pnl, fee FROM fills "
        "WHERE time_ms >= ? AND agent = ?", (cutoff, agent),
    ).fetchall()
    n = len(rows)
    pnl = sum(float(r[3] or 0) for r in rows)
    fees = sum(float(r[4] or 0) for r in rows)
    notional = sum(abs(float(r[1] or 0) * float(r[2] or 0)) for r in rows)
    edge_bps = (pnl - fees) / notional * 10_000 if notional > 0 else None
    by_coin: dict[str, dict] = {}
    for coin, _sz, _px, p, fee in rows:
        c = by_coin.setdefault(coin, {"n": 0, "net": 0.0})
        c["n"] += 1
        c["net"] += float(p or 0) - float(fee or 0)
    losses = sorted(by_coin.items(), key=lambda kv: kv[1]["net"])[:3]
    wins = sorted(by_coin.items(), key=lambda kv: -kv[1]["net"])[:3]
    return {
        "agent": agent, "n_trades": n, "net_pnl": pnl - fees,
        "edge_bps": edge_bps, "notional_traded": notional,
        "top_losers": [(c, v["n"], v["net"]) for c, v in losses if v["net"] < 0],
        "top_winners": [(c, v["n"], v["net"]) for c, v in wins if v["net"] > 0],
    }


def current_params(agent: str) -> dict:
    """Read current defaults plus overrides."""
    defaults = {
        "femr_v1": {
            "funding_enter_per_hr": 0.00015,
            "funding_exit_per_hr": 0.00005,
            "stop_loss_pct": 0.015,
            "take_profit_pct": 0.008,
            "max_hold_hours": 8.0,
            "max_notional_per_trade": 20.0,
            "max_total_notional": 40.0,
        },
        "twap_mr_v1": {
            "sigma_enter": 2.0,
            "sigma_exit": 0.5,
            "stop_loss_pct": 0.015,
            "max_hold_hours": 4.0,
            "max_notional_per_trade": 200.0,
            "max_total_notional": 1000.0,
            "max_concurrent_positions": 5,
        },
        "liq_cascade_v1": {
            "liq_threshold_usd": 100_000.0,
            "liq_window_s": 300,
            "stop_loss_pct": 0.015,
            "take_profit_pct": 0.005,
            "max_notional_per_trade": 25.0,
            "max_total_notional": 50.0,
        },
        "basis_v1": {
            "basis_enter_bps": 20,
            "basis_exit_bps": 5,
            "stop_loss_pct": 0.01,
            "max_notional_per_trade": 25.0,
            "max_total_notional": 50.0,
        },
    }
    params = dict(defaults.get(agent, {}))
    if OVERRIDES.exists():
        data = json.loads(OVERRIDES.read_text())
        params.update(data.get(agent) or {})
    return params


def ask_claude_code(summaries: list[dict], current: dict[str, dict]) -> dict:
    system = (
        "You are a quant strategist analyzing 7 days of live trading agent results "
        "to propose parameter tweaks. Output STRICT JSON only with this exact shape:\n"
        "{\"<agent_name>\": {\"<param>\": <new_value>, ...}}\n\n"
        "Rules:\n"
        "1. Only suggest changes if you have strong directional evidence.\n"
        "2. Conservative changes only (within 50% of current).\n"
        "3. Skip agents with <50 trades — they don't have enough data.\n"
        "4. If an agent is bleeding (net < 0 & edge < -10bps), TIGHTEN entry (higher threshold).\n"
        "5. If an agent is winning but trading rarely, LOOSEN entry slightly.\n"
        "6. Never zero out a stop loss. Never 2x max_notional_per_trade.\n"
        "7. Output empty {} for any agent you don't want to change.\n"
        "8. NO commentary, just JSON."
    )
    user = json.dumps({
        "summaries": summaries,
        "current_params": current,
        "param_bounds": {k: list(v) for k, v in BOUNDS.items()},
    }, default=str)
    schema = json.dumps({
        "type": "object",
        "additionalProperties": {
            "type": "object",
            "additionalProperties": {"type": "number"},
        },
    })
    cmd = [
        CLAUDE_CODE_BIN,
        "-p",
        "--model", CLAUDE_CODE_MODEL,
        "--effort", CLAUDE_CODE_EFFORT,
        "--tools", "",
        "--output-format", "json",
        "--json-schema", schema,
        "--no-session-persistence",
        "--system-prompt", system,
    ]

    # Force Claude Code to use its stored Claude Max/OAuth login rather than any
    # Anthropic API key env vars that might be present in a cron shell.
    env = os.environ.copy()
    for key in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL"):
        env.pop(key, None)

    try:
        proc = subprocess.run(
            cmd,
            input=user,
            text=True,
            capture_output=True,
            timeout=CLAUDE_CODE_TIMEOUT_S,
            env=env,
            check=False,
        )
    except FileNotFoundError:
        return {"error": f"Claude Code binary not found: {CLAUDE_CODE_BIN}"}
    except subprocess.TimeoutExpired:
        return {"error": f"Claude Code timed out after {CLAUDE_CODE_TIMEOUT_S}s"}

    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip().splitlines()[-3:]
        return {"error": "Claude Code failed: " + " | ".join(detail)}

    try:
        wrapper = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        return {"error": f"Claude Code returned non-JSON wrapper: {exc}"}
    if wrapper.get("is_error"):
        return {"error": f"Claude Code error: {wrapper.get('result') or wrapper}"}
    content = wrapper.get("result")
    if not isinstance(content, str):
        return {"error": "Claude Code wrapper did not include a string result"}
    try:
        proposal = json.loads(content)
    except json.JSONDecodeError as exc:
        return {"error": f"Claude Code returned invalid proposal JSON: {exc}: {content[:300]}"}
    if not isinstance(proposal, dict):
        return {"error": "Claude Code proposal was not a JSON object"}
    return proposal


def validate_proposal(agent: str, current: dict, proposed: dict, summary: dict) -> tuple[dict, list[str]]:
    """Clamp + safety-check proposed params. Returns (approved_changes, rejections)."""
    approved: dict = {}
    rejections: list[str] = []

    n = summary.get("n_trades", 0)
    max_pct = 0.20 if n < 50 else 0.50

    for key, new_val in proposed.items():
        if key not in BOUNDS:
            rejections.append(f"{agent}.{key}: unknown param")
            continue
        try:
            new_val = float(new_val)
        except (TypeError, ValueError):
            rejections.append(f"{agent}.{key}: not numeric")
            continue
        lo, hi = BOUNDS[key]
        if not (lo <= new_val <= hi):
            rejections.append(f"{agent}.{key}: {new_val} out of bounds [{lo}, {hi}]")
            continue
        cur_val = float(current.get(key, new_val))
        if key == "max_notional_per_trade":
            agent_cap = MAX_PER_TRADE_BY_AGENT.get(agent, hi)
            if new_val > agent_cap:
                rejections.append(f"{agent}.{key}: {new_val} > approved cap {agent_cap}")
                continue
            if agent != "twap_mr_v1" and new_val > cur_val:
                rejections.append(f"{agent}.{key}: increase rejected; scale approval is TWAP-only")
                continue
        if cur_val > 0:
            ratio = abs(new_val - cur_val) / cur_val
            if ratio > max_pct:
                rejections.append(
                    f"{agent}.{key}: change {ratio*100:.0f}% > limit {max_pct*100:.0f}%"
                )
                continue
        # Bonus safety: stop loss never zero
        if "stop_loss" in key and new_val < BOUNDS[key][0]:
            rejections.append(f"{agent}.{key}: stop-loss too small")
            continue
        approved[key] = new_val
    return approved, rejections


def apply_overrides(changes_by_agent: dict[str, dict]) -> None:
    OVERRIDES.parent.mkdir(parents=True, exist_ok=True)
    data: dict = json.loads(OVERRIDES.read_text()) if OVERRIDES.exists() else {}
    for agent, changes in changes_by_agent.items():
        if not changes:
            continue
        data.setdefault(agent, {}).update(changes)
    OVERRIDES.write_text(json.dumps(data, indent=2))


def log_run(payload: dict) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a") as f:
        f.write(json.dumps(payload, default=str) + "\n")


def main() -> int:
    if not DB.exists():
        print(f"❌ DB not found at {DB}; run `scp` sync first")
        return 1

    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    summaries = [per_agent_summary(conn, a) for a in AGENTS]
    current = {a: current_params(a) for a in AGENTS}

    lines = ["🔧 *hl-bot auto-tune*", "7d snapshot:", ""]
    for s in summaries:
        edge = f"{s['edge_bps']:+.1f}" if s["edge_bps"] is not None else "—"
        lines.append(
            f"• {s['agent']}: {s['n_trades']} trades, "
            f"${s['net_pnl']:+.2f} net, edge "
            f"{edge} bps"
        )
    lines.append("")

    # Ask Claude Code / Claude Max.
    proposal = ask_claude_code(summaries, current)
    if "error" in proposal:
        lines.append(f"⚠️ model call failed: {proposal['error']}")
        print("\n".join(lines))
        log_run({"ts": time.time(), "error": proposal["error"]})
        return 2

    all_changes = {}
    all_rejections = []
    for s in summaries:
        agent = s["agent"]
        proposed = proposal.get(agent) or {}
        approved, rejections = validate_proposal(agent, current[agent], proposed, s)
        if approved:
            all_changes[agent] = approved
        all_rejections.extend(rejections)

    apply_overrides(all_changes)

    if all_changes:
        lines.append("✅ *applied*:")
        for agent, ch in all_changes.items():
            for k, v in ch.items():
                cur = current[agent].get(k, "?")
                lines.append(f"  • {agent}.{k}: {cur} → {v}")
    else:
        lines.append("✅ no changes (model proposed none or all rejected)")

    if all_rejections:
        lines.append("")
        lines.append("⚠️ rejected:")
        for r in all_rejections[:8]:
            lines.append(f"  • {r}")

    log_run({
        "ts": time.time(),
        "summaries": summaries,
        "proposal": proposal,
        "applied": all_changes,
        "rejected": all_rejections,
    })

    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
