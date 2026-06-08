"""Tests for the evidence-driven strategy-health / research module.

This is the scaffolding that lets the bot self-improve: it measures each
agent's health from exchange-grounded fills (multi-window net_pnl + edge_bps,
per-coin contribution concentration, losing coins) and proposes ONLY
risk-reducing config changes. It must never propose raising notional caps and
must flag outlier-dominated edges (e.g. the ZEC-only TWAP profit) so a single
lucky coin can't make a bleeding agent look promotable.
"""

from __future__ import annotations

import time

import pytest

from hl_bot.db.schema import init_db
from hl_bot.research.strategy_health import (
    Proposal,
    agent_health,
    build_proposal_document,
    concentration_share,
    per_coin_contributions,
    propose_overrides,
)


@pytest.fixture
def conn(tmp_path):
    return init_db(tmp_path / "health.sqlite")


def _insert_fill(conn, agent, coin, t_ms, pnl, fee=0.1, sz=10.0, px=1.0):
    conn.execute(
        """INSERT INTO fills(hash, tid, time_ms, coin, side, px, sz,
           start_position, dir, closed_pnl, fee, fee_token, builder_fee,
           cloid, agent, raw_json)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (f"h{coin}{t_ms}{pnl}", t_ms, t_ms, coin, "B", px, sz, 0, "Close Long",
         pnl, fee, "USDC", 0, None, agent, "{}"),
    )


def test_per_coin_contributions_aggregates_net_and_notional(conn):
    now = int(time.time() * 1000)
    _insert_fill(conn, "twap_mr_v1", "ADA", now - 1000, pnl=-5.0, fee=0.1, sz=10, px=1)
    _insert_fill(conn, "twap_mr_v1", "ADA", now - 2000, pnl=-3.0, fee=0.1, sz=10, px=1)
    _insert_fill(conn, "twap_mr_v1", "SOL", now - 1000, pnl=4.0, fee=0.1, sz=1, px=100)

    contrib = per_coin_contributions(conn, "twap_mr_v1", since_ms=0)

    assert contrib["ADA"].n == 2
    assert contrib["ADA"].net == pytest.approx(-8.2)  # -8 pnl - 0.2 fees
    assert contrib["SOL"].net == pytest.approx(3.9)


def test_concentration_share_detects_single_coin_dominance():
    # One coin contributes +200 of +210 total positive -> ~0.95 concentration.
    contrib = {
        "ZEC": _stat("ZEC", net=200.0),
        "SOL": _stat("SOL", net=10.0),
        "ADA": _stat("ADA", net=-50.0),
    }
    share = concentration_share(contrib)
    assert share == pytest.approx(200.0 / 210.0, rel=1e-3)


def test_concentration_share_none_when_no_positive():
    contrib = {"ADA": _stat("ADA", net=-5.0)}
    assert concentration_share(contrib) is None


def test_propose_tightens_bleeding_twap_and_adds_vetoes(conn):
    now = int(time.time() * 1000)
    # Many losing ADA/HYPE trades in last 7d -> bleeding, edge deeply negative.
    for i in range(20):
        _insert_fill(conn, "twap_mr_v1", "ADA", now - (i + 1) * 1000, pnl=-1.0, sz=10, px=1)
        _insert_fill(conn, "twap_mr_v1", "HYPE", now - (i + 1) * 1000, pnl=-1.0, sz=10, px=1)

    health = agent_health(conn, "twap_mr_v1", now_ms=now)
    current = {"twap_mr_v1": {"sigma_enter": 2.0, "max_concurrent_positions": 3,
                              "max_notional_per_trade": 200.0}}

    proposals = propose_overrides([health], current)
    p = next(pr for pr in proposals if pr.agent == "twap_mr_v1")

    # Tightened entry threshold (higher sigma) and reduced concurrency.
    assert p.changes.get("sigma_enter", 2.0) > 2.0
    # Loss-bleeding coins flagged for veto.
    assert "ADA" in p.add_coin_vetoes
    # MUST NOT propose any increase to notional caps.
    assert "max_notional_per_trade" not in p.changes
    assert "max_total_notional" not in p.changes


def test_propose_flags_outlier_dominated_edge(conn):
    now = int(time.time() * 1000)
    # Net positive overall but driven almost entirely by one ZEC win.
    _insert_fill(conn, "twap_mr_v1", "ZEC", now - 1000, pnl=200.0, sz=1, px=100)
    for i in range(5):
        _insert_fill(conn, "twap_mr_v1", "ADA", now - (i + 2) * 1000, pnl=-1.0, sz=10, px=1)

    health = agent_health(conn, "twap_mr_v1", now_ms=now)
    proposals = propose_overrides([health], {"twap_mr_v1": {"sigma_enter": 2.0}})
    p = next(pr for pr in proposals if pr.agent == "twap_mr_v1")

    assert any("outlier" in f.lower() or "concentrat" in f.lower() for f in p.flags)


def test_zec_outlier_does_not_hide_bleeding_book(conn):
    """A single huge ZEC win must not stop the tuner tightening a book that is
    deeply negative once the outlier is excluded (anti-overfit to ZEC)."""
    now = int(time.time() * 1000)
    for i in range(25):
        _insert_fill(conn, "twap_mr_v1", "ADA", now - (i + 1) * 1000, pnl=-1.0, sz=10, px=1)
        _insert_fill(conn, "twap_mr_v1", "HYPE", now - (i + 1) * 1000, pnl=-1.0, sz=10, px=1)
    _insert_fill(conn, "twap_mr_v1", "ZEC", now - 1000, pnl=206.0, sz=1, px=100)

    health = agent_health(conn, "twap_mr_v1", now_ms=now)
    # Aggregate edge is positive (ZEC), but core edge (ex-ZEC) is deeply negative.
    assert health.windows["7d"].edge_bps is not None and health.windows["7d"].edge_bps > 0
    assert health.core_edge_bps is not None and health.core_edge_bps < -100

    proposals = propose_overrides(
        [health], {"twap_mr_v1": {"sigma_enter": 2.0, "max_concurrent_positions": 3}}
    )
    p = next(pr for pr in proposals if pr.agent == "twap_mr_v1")
    assert p.changes.get("sigma_enter") == pytest.approx(2.5)
    assert p.changes.get("max_concurrent_positions") == 2
    assert "ADA" in p.add_coin_vetoes and "HYPE" in p.add_coin_vetoes


def test_build_proposal_document_is_mergeable_and_carries_meta():
    proposals = [
        Proposal(agent="twap_mr_v1", changes={"sigma_enter": 2.5},
                 add_coin_vetoes=["ADA"], flags=["outlier x"], rationale=["bleeding"]),
        Proposal(agent="basis_v1"),  # nothing to say
    ]
    doc = build_proposal_document(proposals)

    # Mergeable overrides only for agents with real changes.
    assert doc["overrides"] == {"twap_mr_v1": {"sigma_enter": 2.5}}
    assert "basis_v1" not in doc["overrides"]
    assert "basis_v1" not in doc["_meta"]
    assert doc["_meta"]["twap_mr_v1"]["add_coin_vetoes"] == ["ADA"]
    # Never carries notional increases.
    assert "max_notional_per_trade" not in doc["overrides"].get("twap_mr_v1", {})


def _stat(coin, net):
    from hl_bot.research.strategy_health import CoinStat
    return CoinStat(coin=coin, n=1, net=net, notional=100.0,
                    edge_bps=(net / 100.0 * 10_000))
