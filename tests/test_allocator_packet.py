"""Allocator-packet tests — the bundle composes the chassis + the two reports.

The packet introduces no numbers of its own: it must faithfully carry the
track-record and edge-search records and the audited chassis list.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from hl_bot.db.schema import init_db
from hl_bot.reports.allocator_packet import (
    CHASSIS,
    build_allocator_packet,
    export,
    to_markdown,
)
from hl_bot.reports.edge_search import THESES


@pytest.fixture
def conn(tmp_path):
    return init_db(tmp_path / "ap.sqlite")


def _fill(conn, agent, t_ms, pnl, fee=0.1):
    conn.execute(
        """INSERT INTO fills(hash, tid, time_ms, coin, side, px, sz,
           start_position, dir, closed_pnl, fee, fee_token, builder_fee,
           cloid, agent, raw_json)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (f"h{agent}{t_ms}", t_ms, t_ms, "BTC", "B", 100.0, 1.0, 0, "Close Long",
         pnl, fee, "USDC", 0, None, agent, "{}"),
    )


def test_chassis_sources_are_real_files():
    # Every cited source must exist in the repo — the chassis claim is auditable.
    repo = Path(__file__).resolve().parents[1]
    for c in CHASSIS:
        assert (repo / c.source).is_file(), f"missing chassis source: {c.source}"


def test_packet_carries_both_reports_faithfully(conn):
    _fill(conn, "twap_mr_regime_v1", int(time.time() * 1000), pnl=5.0)
    packet = build_allocator_packet(conn)

    # edge-search record carried verbatim (same thesis count, all pruned).
    assert len(packet["edge_search"]["theses"]) == len(THESES)
    assert packet["edge_search"]["summary"]["n_pruned"] == len(THESES)

    # track-record record carried (the live agent appears).
    agents = [a["agent"] for a in packet["track_record"]["agents"]]
    assert "twap_mr_regime_v1" in agents

    # chassis list mirrors the module constant.
    assert len(packet["chassis"]) == len(CHASSIS)
    assert packet["headline"].strip()


def test_markdown_renders_all_three_sections(conn):
    _fill(conn, "femr_v1", int(time.time() * 1000), pnl=1.0)
    md = to_markdown(build_allocator_packet(conn))
    assert md.startswith("# hl-bot allocator packet")
    assert "## Deployment chassis" in md
    # composed sub-reports keep their own headers.
    assert "# hl-bot track record" in md
    assert "# hl-bot edge-search summary" in md
    # every chassis source is shown.
    for c in CHASSIS:
        assert c.source in md


def test_export_writes_valid_json_and_markdown(conn, tmp_path):
    _fill(conn, "femr_v1", int(time.time() * 1000), pnl=1.0)
    jp, mp = export(conn, tmp_path / "ap")
    assert jp.exists() and mp.exists()
    loaded = json.loads(jp.read_text())
    assert "chassis" in loaded and "track_record" in loaded and "edge_search" in loaded
    assert len(loaded["edge_search"]["theses"]) == len(THESES)
    assert mp.read_text().startswith("# hl-bot allocator packet")
