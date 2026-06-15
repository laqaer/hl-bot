"""Config fingerprints + G0 params provenance (backlog V3).

A G0 stamp must belong to the config it was earned on: confirm validates an
agent's DEFAULTS, so a tuned override must not silently inherit that pass.
"""

from __future__ import annotations

import time

from hl_bot.agents.dislocation_reversion import DislocationReversionAgent
from hl_bot.agents.fingerprint import config_fingerprint, config_payload
from hl_bot.db.schema import init_db
from hl_bot.supervisor.goals import g0_confirmed

NOW = int(time.time() * 1000)


def test_fingerprint_is_deterministic_and_default_stable():
    a = DislocationReversionAgent(config={})
    b = DislocationReversionAgent(config={})
    assert config_fingerprint(a) == config_fingerprint(b)
    # 12 hex chars
    h = config_fingerprint(a)
    assert len(h) == 12 and all(c in "0123456789abcdef" for c in h)


def test_fingerprint_changes_when_a_param_changes():
    base = config_fingerprint(DislocationReversionAgent(config={}))
    tuned = config_fingerprint(DislocationReversionAgent(config={"z_enter": 2.5}))
    assert base != tuned


def test_fingerprint_captures_effective_not_supplied_config():
    # Passing a default value explicitly yields the SAME effective config as the
    # default, so the hash must match — it fingerprints the resolved cfg, not the
    # raw override dict.
    default = config_fingerprint(DislocationReversionAgent(config={}))
    explicit = config_fingerprint(DislocationReversionAgent(config={"z_enter": 3.0}))
    assert default == explicit


def test_payload_falls_back_to_dict_without_cfg():
    class Bare:
        config = {"b": 2, "a": 1}

    obj = Bare()
    assert config_payload(obj) == {"a": 1, "b": 2}
    # key order in the source dict must not change the hash
    class Bare2:
        config = {"a": 1, "b": 2}

    assert config_fingerprint(obj) == config_fingerprint(Bare2())


def test_confirmations_table_has_params_hash_column(tmp_path):
    conn = init_db(tmp_path / "t.sqlite")
    cols = {r[1] for r in conn.execute("PRAGMA table_info(confirmations)").fetchall()}
    assert "params_hash" in cols


def _stamp(conn, *, params_hash, confirmed=1, ts_ms=NOW):
    conn.execute(
        """INSERT INTO confirmations(agent, ts_ms, dataset, prefer, confirmed,
                                     oos_edge_bps, params_hash)
           VALUES('disloc', ?, 'test', 'taker', ?, 6.5, ?)""",
        (ts_ms, confirmed, params_hash),
    )


def test_g0_with_params_hash_matches_and_rejects(tmp_path):
    conn = init_db(tmp_path / "t.sqlite")
    _stamp(conn, params_hash="deadbeef0001")
    # legacy name-only check still passes
    assert g0_confirmed(conn, "disloc", now_ms=NOW) is True
    # provenance check: matching hash passes, a different one is refused
    assert g0_confirmed(conn, "disloc", now_ms=NOW, params_hash="deadbeef0001") is True
    assert g0_confirmed(conn, "disloc", now_ms=NOW, params_hash="0000ffff9999") is False


def test_g0_legacy_null_hash_does_not_satisfy_provenance(tmp_path):
    conn = init_db(tmp_path / "t.sqlite")
    _stamp(conn, params_hash=None)  # pre-provenance row
    assert g0_confirmed(conn, "disloc", now_ms=NOW) is True            # name-only
    assert g0_confirmed(conn, "disloc", now_ms=NOW, params_hash="abc123abc123") is False
