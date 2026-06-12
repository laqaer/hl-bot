"""Moonshot-sleeve ring-fence checker (risk/sleeve.py, B17) — the invariants
docs/MOONSHOT.md promises: isolated-only bets, per-bet margin cap, bet count,
kill floor, sweep ratchet, address isolation."""

from __future__ import annotations

import pytest

from hl_bot.risk.sleeve import (
    SleeveConfig,
    evaluate_sleeve,
    parse_sleeve_positions,
)


def cfg(**kw) -> SleeveConfig:
    base = dict(hard_cap=100.0, max_bet_frac=0.25,
                max_concurrent_bets=2, kill_floor_frac=0.25)
    base.update(kw)
    return SleeveConfig(**base)


def pos(coin="DOGE", szi=10.0, margin=20.0, lev_type="isolated", lev=5,
        value=100.0, upnl=0.0) -> dict:
    return {"position": {
        "coin": coin, "szi": str(szi), "marginUsed": str(margin),
        "leverage": {"type": lev_type, "value": lev},
        "positionValue": str(value), "unrealizedPnl": str(upnl),
    }}


def state(equity=100.0, positions=()) -> dict:
    return {"marginSummary": {"accountValue": str(equity)},
            "assetPositions": list(positions)}


# --- config validation -------------------------------------------------------

@pytest.mark.parametrize("kw", [
    {"hard_cap": 0.0}, {"hard_cap": -5.0},
    {"max_bet_frac": 0.0}, {"max_bet_frac": 1.5},
    {"max_concurrent_bets": 0},
    {"kill_floor_frac": -0.1}, {"kill_floor_frac": 1.0},
])
def test_config_rejects_out_of_range(kw):
    with pytest.raises(ValueError):
        cfg(**kw)


def test_config_derived_levels():
    c = cfg(hard_cap=200.0, max_bet_frac=0.25, kill_floor_frac=0.3)
    assert c.max_bet_margin == 50.0
    assert c.kill_floor == 60.0


# --- parse -------------------------------------------------------------------

def test_parse_keeps_leverage_type_and_skips_malformed():
    st = state(positions=[
        pos(coin="DOGE", lev_type="isolated"),
        pos(coin="WIF", lev_type="cross"),
        {"position": {"coin": "BAD", "szi": "not-a-number"}},
        "garbage",
    ])
    bets = parse_sleeve_positions(st)
    assert [(b.coin, b.leverage_type) for b in bets] == [
        ("DOGE", "isolated"), ("WIF", "cross")]
    assert bets[0].margin_used == 20.0 and bets[0].leverage == 5.0


# --- evaluate ----------------------------------------------------------------

def test_clean_sleeve_is_ok():
    st = state(equity=95.0, positions=[pos(coin="DOGE", margin=25.0),
                                       pos(coin="WIF", margin=10.0)])
    r = evaluate_sleeve(cfg(), st, address="0x" + "a" * 40)
    assert r.status == "OK" and not r.violations and not r.notes
    assert r.committed_margin == 35.0
    assert r.kill_headroom == pytest.approx(70.0)
    assert r.sweep_excess == 0.0


def test_cross_margin_is_a_violation():
    st = state(positions=[pos(coin="WIF", lev_type="cross")])
    r = evaluate_sleeve(cfg(), st)
    assert r.status == "VIOLATIONS"
    assert any("WIF" in v and "isolated" in v for v in r.violations)


def test_oversized_bet_margin_is_a_violation():
    st = state(positions=[pos(coin="DOGE", margin=30.0)])  # cap 25
    r = evaluate_sleeve(cfg(), st)
    assert any("per-bet cap" in v for v in r.violations)


def test_too_many_bets_is_a_violation():
    st = state(positions=[pos(coin=c, margin=5.0) for c in ("A", "B", "C")])
    r = evaluate_sleeve(cfg(), st)
    assert any("3 open bets > max 2" in v for v in r.violations)


def test_kill_floor_means_dead_and_outranks_violations():
    st = state(equity=25.0, positions=[pos(coin="WIF", lev_type="cross")])
    r = evaluate_sleeve(cfg(), st)  # floor = 25.0, <= is dead
    assert r.dead and r.status == "DEAD"
    assert r.violations  # the cross breach is still reported
    assert any("DEAD" in n for n in r.notes)


def test_profit_above_cap_notes_sweep_not_violation():
    st = state(equity=140.0)
    r = evaluate_sleeve(cfg(), st)
    assert r.status == "OK" and r.sweep_excess == pytest.approx(40.0)
    assert any("sweep $40.00" in n for n in r.notes)


def test_core_address_match_is_ring_fence_breach_case_insensitive():
    sleeve = "0x" + "AB" * 20
    r = evaluate_sleeve(
        cfg(), state(), address=sleeve,
        core_addresses=(sleeve.lower(), ""))
    assert any("ring-fence breach" in v for v in r.violations)


def test_empty_state_is_no_data():
    r = evaluate_sleeve(cfg(), {})
    assert r.status == "NO_DATA" and not r.has_data


# --- CLI ---------------------------------------------------------------------

class _FakeInfoClient:
    payload: dict = {}

    def __init__(self, *a, **kw):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def post(self, url, json=None):
        assert json["type"] == "clearinghouseState"

        class _R:
            def json(self_inner):
                return _FakeInfoClient.payload
        return _R()


def _invoke(monkeypatch, payload, args):
    import httpx
    from typer.testing import CliRunner

    from hl_bot.cli.main import app
    _FakeInfoClient.payload = payload
    monkeypatch.setattr(httpx, "Client", _FakeInfoClient)
    return CliRunner().invoke(app, ["sleeve-check", *args])


SLEEVE_ADDR = "0x" + "1" * 40


def test_cli_clean_and_violation_arms(monkeypatch):
    monkeypatch.setenv("HLBOT_SLEEVE_ADDRESS", SLEEVE_ADDR)
    res = _invoke(monkeypatch, state(equity=95.0, positions=[pos()]),
                  ["--hard-cap", "100"])
    assert res.exit_code == 0, res.output
    assert "OK" in res.output and "DOGE" in res.output

    res2 = _invoke(monkeypatch,
                   state(positions=[pos(coin="WIF", lev_type="cross")]),
                   ["--hard-cap", "100"])
    assert res2.exit_code == 0
    assert "VIOLATIONS" in res2.output and "isolated" in res2.output


def test_cli_missing_or_malformed_address_exits_1(monkeypatch):
    monkeypatch.delenv("HLBOT_SLEEVE_ADDRESS", raising=False)
    res = _invoke(monkeypatch, state(), ["--hard-cap", "100"])
    assert res.exit_code == 1 and "no sleeve address" in res.output

    res2 = _invoke(monkeypatch, state(),
                   ["--hard-cap", "100", "--address", "0xnothex"])
    assert res2.exit_code == 1 and "malformed" in res2.output


def test_cli_core_address_collision_flagged(monkeypatch):
    monkeypatch.setenv("HL_TRADER_ADDRESS", SLEEVE_ADDR)
    monkeypatch.delenv("HL_VAULT_ADDRESS", raising=False)
    res = _invoke(monkeypatch, state(), ["--hard-cap", "100",
                                         "--address", SLEEVE_ADDR])
    assert res.exit_code == 0
    assert "ring-fence breach" in res.output


def test_cli_no_data_exits_1(monkeypatch):
    res = _invoke(monkeypatch, {}, ["--hard-cap", "100",
                                    "--address", SLEEVE_ADDR])
    assert res.exit_code == 1 and "no clearinghouse data" in res.output
