"""Hard kill switch — file-based, sticky, human-cleared.

The kill file (``data/KILL``) is the single emergency brake for a fully
autonomous book. While it exists:

  * no NEW orders are placed (entries blocked);
  * flatten/cancel actions are still allowed — risk reduction is never blocked;
  * the supervisor will not promote any agent (pause/demote still run).

It is *sticky*: tripped automatically (account loss limit, equity floor) or
manually (``hlbot kill "reason"``), it stays until a human runs
``hlbot resume``. A file is used instead of a DB row so it works even when the
DB is wedged, and so a human can always ``touch data/KILL`` over SSH.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path

log = logging.getLogger(__name__)

KILL_FILENAME = "KILL"


def kill_path(data_dir: str | Path) -> Path:
    return Path(data_dir) / KILL_FILENAME


def kill_active(data_dir: str | Path) -> str | None:
    """Return the kill reason if the kill switch is tripped, else None."""
    p = kill_path(data_dir)
    try:
        if not p.exists():
            return None
        return p.read_text().strip() or "kill file present (no reason recorded)"
    except OSError:
        # If we cannot read the data dir at all, fail SAFE: treat as killed.
        return "kill state unreadable — failing safe"


def trip_kill(data_dir: str | Path, reason: str, *, alert: bool = True) -> str:
    """Trip the kill switch. Idempotent: an existing reason is preserved and the
    new one appended, so the first cause is never overwritten."""
    p = kill_path(data_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
    line = f"[{stamp}] {reason}"
    existing = kill_active(data_dir)
    if existing and p.exists():
        p.write_text(p.read_text().rstrip() + "\n" + line + "\n")
    else:
        p.write_text(line + "\n")
    log.error("KILL TRIPPED: %s", reason)
    if alert:
        _alert(f"🛑 hl-bot KILL tripped: {reason}\nNew orders halted. Run `hlbot resume` to clear.")
    return line


def clear_kill(data_dir: str | Path, *, alert: bool = True) -> bool:
    """Clear the kill switch. Returns True if it was active."""
    p = kill_path(data_dir)
    if not p.exists():
        return False
    reason = kill_active(data_dir)
    p.unlink(missing_ok=True)
    log.warning("kill cleared (was: %s)", reason)
    if alert:
        _alert("✅ hl-bot kill cleared — trading may resume.")
    return True


def equity_floor_breached(
    conn: sqlite3.Connection,
    *,
    frac: float = 0.75,
    lookback_days: int = 30,
    now_ms: int | None = None,
) -> tuple[bool, str]:
    """True when current equity has fallen below ``frac`` of the rolling
    high-water-mark over ``lookback_days``. The last backstop behind all
    per-agent guardrails: catches a slow account-wide bleed."""
    now_ms = now_ms or int(time.time() * 1000)
    since = now_ms - lookback_days * 86_400_000
    row = conn.execute(
        "SELECT MAX(account_value) FROM equity_snapshots WHERE ts_ms >= ?", (since,)
    ).fetchone()
    hwm = float(row[0]) if row and row[0] is not None else None
    cur_row = conn.execute(
        "SELECT account_value FROM equity_snapshots ORDER BY ts_ms DESC LIMIT 1"
    ).fetchone()
    cur = float(cur_row[0]) if cur_row and cur_row[0] is not None else None
    if hwm is None or cur is None or hwm <= 0:
        return False, "no equity history"
    floor = frac * hwm
    if cur < floor:
        return True, f"equity ${cur:.2f} < {frac:.0%} of {lookback_days}d HWM ${hwm:.2f} (floor ${floor:.2f})"
    return False, f"equity ${cur:.2f} ≥ floor ${floor:.2f} ({lookback_days}d HWM ${hwm:.2f})"


def _alert(message: str) -> None:
    # Lazy import: keep kill.py importable without the exchange SDK stack.
    try:
        from ..exec.orders import telegram_alert
        telegram_alert(message)
    except Exception:  # noqa: BLE001
        log.warning("kill alert not sent: %s", message[:80])
