"""WS overlay keeps the funding SIGNAL fresh.

enrich_view() copies REST funding into extra["funding_hourly"] before the WS
overlay runs; the overlay then updates view.funding from the socket. A
funding-threshold agent reads extra["funding_hourly"], so the overlay must
re-mirror it or such agents gate on stale REST funding in WS-enabled runs
(Codex #18 P2).
"""

from __future__ import annotations

import hl_bot.ingest.ws as ws
from hl_bot.agents.base import MarketView
from hl_bot.engine import views


class _Snap:
    mids = {"BTC": 101.0}
    funding = {"BTC": 0.00009}     # fresher WS funding
    book_top: dict = {}
    extra: dict = {}


def test_overlay_refreshes_funding_hourly(monkeypatch, tmp_path):
    v = MarketView(ts_ms=0, mids={"BTC": 100.0}, funding={"BTC": 0.00001})
    v.extra["funding_hourly"] = dict(v.funding)   # as enrich_view would set it (stale REST)

    monkeypatch.setattr(ws, "load_fresh_snapshot", lambda path, max_age_s=30.0: _Snap())
    assert views.overlay_ws_snapshot(v, str(tmp_path / "snap.json")) is True

    # both the raw funding and the signal copy reflect the WS value, not the stale REST one
    assert v.funding["BTC"] == 0.00009
    assert v.extra["funding_hourly"]["BTC"] == 0.00009
