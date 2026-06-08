"""Operational automation: health checks, heartbeat, preflight ('doctor').

These make 24/7 unattended running safe: ``assess_health`` turns the ground-truth
DB into an ok/warn/down verdict (is the bot still ticking? ingesting? bleeding?
paused?), and ``hlbot health`` pings a dead-man-switch + alerts on trouble. The
pure assessment is unit-tested; the network/alert side-effects are thin.
"""

from .health import HealthReport, assess_health, ping_heartbeat

__all__ = ["HealthReport", "assess_health", "ping_heartbeat"]
