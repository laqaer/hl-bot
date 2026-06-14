"""Honest coverage reporting.

HL's candleSnapshot serves at most ~5000 candles per interval, so a fine
interval can't reach a long lookback regardless of the requested window (5m →
~17d). These tests pin the truth-telling: the actual span is measured, a
short-coverage WARNING fires, and the sweep report says the real window rather
than the misleading requested days.
"""

from __future__ import annotations

import logging

from hl_bot.backtest.data import (
    Frame,
    frames_coverage_days,
    warn_if_short_coverage,
)
from hl_bot.research.sweep import SweepRow, SweepSpec, render_markdown

DAY = 86_400_000


def _frames(n: int, *, step_ms: int = 5 * 60_000) -> list[Frame]:
    return [Frame(ts_ms=i * step_ms, mids={"BTC": 100.0}) for i in range(n)]


def test_frames_coverage_days_measures_span():
    # 10 days of 5m bars → span is exactly 10 days (minus one bar).
    bars = 10 * DAY // (5 * 60_000) + 1
    frames = _frames(bars)
    assert abs(frames_coverage_days(frames) - 10.0) < 0.01


def test_frames_coverage_days_degenerate():
    assert frames_coverage_days([]) == 0.0
    assert frames_coverage_days(_frames(1)) == 0.0


def test_warn_fires_when_coverage_short(caplog):
    # ~17d of 5m bars but the request asked for 90d → must warn.
    bars = 17 * DAY // (5 * 60_000)
    frames = _frames(bars)
    with caplog.at_level(logging.WARNING):
        cov = warn_if_short_coverage(frames, interval="5m", days=90)
    assert 16.5 < cov < 17.5
    assert any("coverage short" in r.message for r in caplog.records)


def test_warn_silent_when_coverage_adequate(caplog):
    # ~200d of 1h bars for a 180d request → no warning.
    bars = 200 * DAY // (60 * 60_000)
    frames = _frames(bars, step_ms=60 * 60_000)
    with caplog.at_level(logging.WARNING):
        warn_if_short_coverage(frames, interval="1h", days=180)
    assert not any("coverage short" in r.message for r in caplog.records)


def _spec() -> SweepSpec:
    return SweepSpec(agent="dislocation_reversion_v1", interval="5m", days=90,
                     prefer="taker")


def _row() -> SweepRow:
    return SweepRow(universe=["BTC"], params={}, confirmed=False, is_edge_bps=1.0,
                    oos_edge_bps=1.0, oos_sharpe=1.0, oos_net_pnl=0.0, n_trades=5,
                    reasons=[])


def test_report_states_actual_coverage_when_short():
    md = render_markdown(_spec(), [_row()], date="2026-06-14",
                         coverage_by_universe={"BTC,ETH,SOL,HYPE": 17.4})
    assert "~17.4d" in md
    assert "not 90d" in md


def test_report_omits_note_when_coverage_adequate():
    md = render_markdown(_spec(), [_row()], date="2026-06-14",
                         coverage_by_universe={"BTC,ETH,SOL,HYPE": 88.0})
    assert "ACTUAL coverage" not in md


def test_report_omits_note_when_coverage_unknown():
    # Back-compat: callers that don't pass coverage get the old header.
    md = render_markdown(_spec(), [_row()], date="2026-06-14")
    assert "ACTUAL coverage" not in md


def test_report_uses_limiting_span_not_max():
    # A long-history universe must NOT mask a short/failed one (Codex P2): the
    # note must fire on the shortest span and list every universe.
    md = render_markdown(_spec(), [_row()], date="2026-06-14",
                         coverage_by_universe={"BIG": 90.0, "SHORT": 17.4, "FAILED": 0.0})
    assert "ACTUAL coverage" in md
    assert "SHORT ~17.4d" in md
    assert "FAILED ~0.0d" in md
    assert "SHORTEST span" in md
    # shortest listed first
    assert md.index("FAILED ~0.0d") < md.index("SHORT ~17.4d") < md.index("BIG ~90.0d")
