"""V3 — params_hash provenance.

`hlbot confirm` must validate the DEPLOYED config and stamp its params_hash;
promotion's require_g0 must match that hash so a tuned override can never
inherit a G0 confirmation earned for different params. These tests pin:

1. the canonical hash is stable, order-independent, and behaviour-sensitive;
2. the deployed roster's hash reflects agent_overrides.json;
3. g0_confirmed / evaluate gate on the hash (match promotes, mismatch blocks,
   legacy NULL-hash rows never satisfy a specific hash);
4. the confirmations table carries the params_hash column (migration 5).
"""

from __future__ import annotations

import time

import pytest

from hl_bot.agents.base import compute_params_hash
from hl_bot.config import CONFIG_DIR
from hl_bot.db.schema import init_db
from hl_bot.engine.runner import AGENT_FACTORIES, _load_overrides
from hl_bot.supervisor.goals import AgentGoals, evaluate, g0_confirmed
from hl_bot.supervisor.loop import deployed_params_hashes

NOW = int(time.time() * 1000)
DAY = 86_400_000


# --- canonical hash ---------------------------------------------------------

def test_hash_is_order_independent_and_stable():
    a = compute_params_hash({"z_enter": 3.0, "stop_pct": 0.02})
    b = compute_params_hash({"stop_pct": 0.02, "z_enter": 3.0})
    assert a == b
    assert len(a) == 12 and all(c in "0123456789abcdef" for c in a)


def test_hash_changes_when_a_param_changes():
    base = compute_params_hash({"z_enter": 3.0})
    assert base != compute_params_hash({"z_enter": 2.5})


def test_agent_fingerprint_captures_overrides():
    """The deployed hash must differ from defaults when an override changes a
    behaviour param — otherwise the stamp would not actually pin the config."""
    default = AGENT_FACTORIES["femr_v1"](None, {})
    tuned = AGENT_FACTORIES["femr_v1"](None, {"stop_loss_pct": 0.0225})
    assert default.params_hash() != tuned.params_hash()
    # an override equal to the default resolves to the SAME hash (behaviourally
    # identical configs are the same provenance)
    same = AGENT_FACTORIES["femr_v1"](
        None, {"stop_loss_pct": default.params_fingerprint()["stop_loss_pct"]})
    assert same.params_hash() == default.params_hash()


def test_deployed_params_hashes_reflect_overrides(tmp_path):
    conn = init_db(tmp_path / "t.sqlite")
    hashes = deployed_params_hashes(conn, CONFIG_DIR)
    assert hashes  # roster is non-empty
    ov = _load_overrides(CONFIG_DIR)
    # Every rostered agent's deployed hash == factory(defaults + its override).
    # (Retired agents whose overrides linger in the json are intentionally
    # absent from the roster, hence the gate never consults them.)
    for name in hashes:
        override = dict(ov.get(name) or {})
        assert hashes[name] == AGENT_FACTORIES[name](conn, override).params_hash()


# --- gate matching ----------------------------------------------------------

def _stamp(conn, *, confirmed=1, ts_ms=None, params_hash="abc123"):
    conn.execute(
        """INSERT INTO confirmations(agent, ts_ms, dataset, prefer, confirmed,
                                     oos_edge_bps, params_hash)
           VALUES('disl', ?, 'test', 'taker', ?, 6.0, ?)""",
        (ts_ms or NOW, confirmed, params_hash),
    )


@pytest.fixture()
def conn(tmp_path):
    return init_db(tmp_path / "t.sqlite")


def test_confirmations_has_params_hash_column(conn):
    cols = {r[1] for r in conn.execute("PRAGMA table_info(confirmations)").fetchall()}
    assert "params_hash" in cols


def test_g0_confirmed_matches_hash(conn):
    _stamp(conn, params_hash="abc123")
    # no hash requested -> any fresh confirmed row counts (back-compat)
    assert g0_confirmed(conn, "disl") is True
    # matching hash -> True
    assert g0_confirmed(conn, "disl", params_hash="abc123") is True
    # mismatched hash -> False (the whole point of V3)
    assert g0_confirmed(conn, "disl", params_hash="deadbeef") is False


def test_g0_legacy_null_hash_never_matches_specific_hash(conn):
    _stamp(conn, params_hash=None)
    assert g0_confirmed(conn, "disl") is True            # age-only still counts
    assert g0_confirmed(conn, "disl", params_hash="abc123") is False


LADDER = {
    "agent": "disl",
    "mode": "paper",
    "promotion_ladder": [{
        "from": "paper", "to": "live_small",
        "min_days_in_mode": 0, "require_g0": True,
        "persist_days": 0, "persist_evals": 1,
        "conditions": [
            {"metric": "n_trades", "window": "30d", "op": ">=", "threshold": 1,
             "source": "paper"},
        ],
    }],
}


def _seed_paper(conn):
    conn.execute("INSERT INTO agent_decisions(ts_ms, agent, action, is_paper) "
                 "VALUES(?, 'disl', 'hold', 1)", (NOW - 10 * DAY,))
    conn.execute(
        """INSERT INTO paper_fills(time_ms, agent, coin, side, px, sz, closed_pnl, fee)
           VALUES(?, 'disl', 'BTC', 'B', 100.0, 1.0, 5.0, 0.01)""",
        (NOW - DAY,))


def _promotes(conn, *, params_hash):
    g = AgentGoals.model_validate(LADDER)
    evals = evaluate(conn, g, current_mode="paper", params_hash=params_hash)
    return [e for e in evals if e.action == "promote"]


def test_promotion_requires_matching_params_hash(conn):
    _seed_paper(conn)
    _stamp(conn, params_hash="cfg_v1")
    # deployed params match the confirmed hash -> promotes
    assert len(_promotes(conn, params_hash="cfg_v1")) == 1
    # deployed params drifted (a tuned override) -> the old stamp does NOT count
    assert _promotes(conn, params_hash="cfg_v2") == []


def test_promotion_blocked_message_names_hash(conn):
    _seed_paper(conn)
    _stamp(conn, params_hash="cfg_v1")
    g = AgentGoals.model_validate(LADDER)
    evals = evaluate(conn, g, current_mode="paper", params_hash="cfg_v2")
    blocked = [e for e in evals if e.goal_name == "promotion" and e.status == "na"]
    assert blocked and "cfg_v2" in blocked[0].detail
