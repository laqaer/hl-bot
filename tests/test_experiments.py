"""Pre-registered experiment specs (B-G014 / B-EDGE2b) — offline, tmp dirs.

What must hold for a frozen spec to be trustworthy evidence machinery:
  1. A typo anywhere in a spec is a hard error, never a silent default —
     a mislabeled arm would poison the recorded evidence.
  2. Ripeness is judged on the WORST coin across the full arm universe; a
     missing or short store series means NOT ripe.
  3. The runner builds frames once per (universe, window) pair and passes
     each arm's frozen prefer/maker_fill/config plus the spec's thresholds
     to the confirm harness untouched.
  4. The committed specs load, target known agents, and pin the
     honesty-critical fields (maker arms judged on resting fills; B-G014
     waits for a 14d 1m store).
  5. CLI: an unripe spec refuses to run (exit 3) before any frame/network
     work; --check-only reports the span readout and stops.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hl_bot.backtest.confirm import ConfirmationResult, ScenarioResult
from hl_bot.backtest.experiments import (
    PERIODS_PER_YEAR,
    check_ripeness,
    load_spec,
    run_experiment,
)
from hl_bot.backtest.store import save_store, store_path

MIN = 60_000
DAY_BARS = 1_440  # 1m bars per day

SPECS_DIR = Path(__file__).resolve().parent.parent / "configs" / "experiments"


def _bar(t: int) -> dict:
    return {"t": t, "o": 100.0, "h": 100.0, "l": 100.0, "c": 100.0, "v": 1.0}


def _write_store(root: Path, coin: str, interval: str, n_bars: int, step: int = MIN) -> None:
    save_store(store_path(coin, interval, root), [_bar(i * step) for i in range(n_bars)])


def _spec_dict(**over) -> dict:
    base = {
        "name": "t_spec",
        "description": "test spec",
        "agent": "twap_mr_v1",
        "coins": ["BTC", "ETH"],
        "interval": "1m",
        "arms": [{"name": "a1"}],
    }
    base.update(over)
    return base


def _write_spec(tmp_path: Path, **over) -> Path:
    p = tmp_path / "spec.json"
    p.write_text(json.dumps(_spec_dict(**over)))
    return p


# ---------------------------------------------------------------------------
# load_spec
# ---------------------------------------------------------------------------


def test_load_spec_fields_and_arm_inheritance(tmp_path):
    p = _write_spec(
        tmp_path,
        days=0,
        vwap_window=60,
        min_span_days=14,
        min_edge_bps=3.0,
        decision="rule text",
        arms=[
            {"name": "base"},
            {"name": "alt", "coins": ["SOL"], "vwap_window": 240,
             "prefer": "maker", "maker_fill": "resting",
             "config": {"stop_loss_pct": 0.03}},
        ],
    )
    spec = load_spec(p)
    assert spec.agent == "twap_mr_v1"
    assert spec.min_span_days == 14
    base, alt = spec.arms
    # base inherits the spec; alt overrides everything
    assert spec.arm_coins(base) == ["BTC", "ETH"]
    assert spec.arm_window(base) == 60
    assert (base.prefer, base.maker_fill, base.config) == ("taker", "optimistic", {})
    assert spec.arm_coins(alt) == ["SOL"]
    assert spec.arm_window(alt) == 240
    assert alt.config == {"stop_loss_pct": 0.03}
    assert spec.universe() == ["BTC", "ETH", "SOL"]


@pytest.mark.parametrize(
    "over",
    [
        {"typo_key": 1},                                      # unknown spec key
        {"arms": [{"name": "a", "typo": 1}]},                 # unknown arm key
        {"arms": [{"name": "a", "prefer": "limit"}]},         # bad prefer
        {"arms": [{"name": "a", "maker_fill": "instant"}]},   # bad maker_fill
        {"arms": [{"name": "a"}, {"name": "a"}]},             # duplicate arm names
        {"arms": []},                                         # no arms
        {"arms": [{"name": "a", "config": [1]}]},             # non-object config
        {"arms": [{"name": "a", "coins": []}]},               # empty arm universe
        {"coins": "BTC"},                                     # coins not a list
        {"source": "csv"},                                    # unknown source
    ],
)
def test_load_spec_rejects_typos_hard(tmp_path, over):
    p = _write_spec(tmp_path, **over)
    with pytest.raises(ValueError):
        load_spec(p)


def test_load_spec_rejects_missing_key_and_non_object(tmp_path):
    d = _spec_dict()
    del d["agent"]
    p = tmp_path / "missing.json"
    p.write_text(json.dumps(d))
    with pytest.raises(ValueError, match="agent"):
        load_spec(p)
    p2 = tmp_path / "arr.json"
    p2.write_text("[1, 2]")
    with pytest.raises(ValueError, match="object"):
        load_spec(p2)
    p3 = tmp_path / "bad.json"
    p3.write_text("{nope")
    with pytest.raises(ValueError, match="JSON"):
        load_spec(p3)


# ---------------------------------------------------------------------------
# check_ripeness
# ---------------------------------------------------------------------------


def test_ripeness_worst_coin_governs(tmp_path):
    spec = load_spec(_write_spec(tmp_path, min_span_days=2))
    _write_store(tmp_path, "BTC", "1m", 3 * DAY_BARS + 1)  # exactly 3d span
    _write_store(tmp_path, "ETH", "1m", DAY_BARS + 1)      # exactly 1d span
    rep = check_ripeness(spec, root=tmp_path)
    assert not rep.ripe
    assert rep.min_span == pytest.approx(1.0)
    assert "NOT RIPE" in rep.summary()
    # top the short coin up past the bar → ripe
    _write_store(tmp_path, "ETH", "1m", 3 * DAY_BARS + 1)
    rep2 = check_ripeness(spec, root=tmp_path)
    assert rep2.ripe and "RIPE" in rep2.summary()


def test_ripeness_missing_coin_and_arm_universe(tmp_path):
    # arm-level coins join the spec universe; a coin with no store file
    # makes the whole spec unripe (min_span unknowable, not 0)
    spec = load_spec(_write_spec(
        tmp_path, coins=["BTC"], min_span_days=1,
        arms=[{"name": "a"}, {"name": "b", "coins": ["ETH"]}],
    ))
    _write_store(tmp_path, "BTC", "1m", 3 * DAY_BARS)
    rep = check_ripeness(spec, root=tmp_path)
    assert [s.coin for s in rep.spans] == ["BTC", "ETH"]
    assert rep.min_span is None and not rep.ripe
    assert "no stored bars" in rep.summary()


# ---------------------------------------------------------------------------
# run_experiment
# ---------------------------------------------------------------------------


def _fake_result(name: str) -> ConfirmationResult:
    s = ScenarioResult(name, 0.0, 1.0, 1.0, 30)
    return ConfirmationResult(agent=name, confirmed=True, reasons=[],
                              in_sample=s, out_of_sample=s)


def test_run_experiment_groups_frames_and_passes_arms_through(tmp_path):
    spec = load_spec(_write_spec(
        tmp_path,
        interval="1m",
        min_edge_bps=5.0,
        min_sharpe=1.5,
        min_trades=40,
        arms=[
            {"name": "base"},
            {"name": "w240", "vwap_window": 240},
            {"name": "stop-maker", "prefer": "maker", "maker_fill": "resting",
             "config": {"stop_loss_pct": 0.03}},
        ],
    ))
    loads: list[tuple[tuple[str, ...], int]] = []
    confirms: list[dict] = []

    def fake_load(coins, window):
        loads.append((tuple(coins), window))
        return [("frames", tuple(coins), window)]

    def fake_confirm(factory, frames, **kw):
        confirms.append({"factory": factory, "frames": frames, **kw})
        return _fake_result("fake")

    results = run_experiment(
        spec,
        factory_for=lambda cfg: ("factory", tuple(sorted(cfg.items()))),
        load_frames=fake_load,
        confirm_fn=fake_confirm,
    )
    assert [ar.arm.name for ar in results] == ["base", "w240", "stop-maker"]
    # one frames build per distinct (universe, window): base and stop-maker share
    assert loads == [(("BTC", "ETH"), 60), (("BTC", "ETH"), 240)]
    assert confirms[0]["frames"] is confirms[2]["frames"]
    # arm knobs and spec thresholds reach the confirm harness untouched
    assert confirms[0]["prefer"] == "taker" and confirms[0]["maker_fill"] == "optimistic"
    assert confirms[2]["prefer"] == "maker" and confirms[2]["maker_fill"] == "resting"
    assert confirms[2]["factory"] == ("factory", (("stop_loss_pct", 0.03),))
    for c in confirms:
        assert (c["min_edge_bps"], c["min_sharpe"], c["min_trades"]) == (5.0, 1.5, 40)
        assert c["periods_per_year"] == PERIODS_PER_YEAR["1m"]


# ---------------------------------------------------------------------------
# the committed specs — these ARE the pre-registration; pin what keeps them honest
# ---------------------------------------------------------------------------


def test_registered_specs_load_and_target_known_agents():
    from hl_bot.cli.main import _backtest_factories

    paths = sorted(SPECS_DIR.glob("*.json"))
    assert paths, "configs/experiments/ must contain the registered specs"
    for p in paths:
        spec = load_spec(p)
        assert spec.agent in _backtest_factories({}), f"{p.name}: unknown agent {spec.agent}"
        assert spec.decision, f"{p.name}: a spec without a frozen decision rule isn't pre-registered"


def test_b_g014_spec_pins():
    spec = load_spec(SPECS_DIR / "b_g014.json")
    assert (spec.agent, spec.interval, spec.source, spec.days) == ("twap_mr_v1", "1m", "store", 0.0)
    assert spec.min_span_days == 14
    assert (spec.min_edge_bps, spec.min_sharpe, spec.min_trades) == (3.0, 1.0, 20)
    assert len(spec.arms) == 6
    # every maker arm is judged on honest resting fills — optimistic maker
    # flipped the sign of the whole maker case (Iters 50/51)
    for arm in spec.arms:
        if arm.prefer == "maker":
            assert arm.maker_fill == "resting", arm.name
    # the three backlog-prescribed configs, each on both execution bases
    assert {spec.arm_window(a) for a in spec.arms} == {60, 240}
    stops = [a for a in spec.arms if a.config.get("stop_loss_pct") == 0.03]
    assert len(stops) == 2 and {a.prefer for a in stops} == {"taker", "maker"}
    # no stop+window combo arm (anti-synergistic, Iter 33)
    for a in spec.arms:
        assert not (a.config.get("stop_loss_pct") and spec.arm_window(a) != 60)


def test_b_edge2b_spec_pins():
    spec = load_spec(SPECS_DIR / "b_edge2b.json")
    assert (spec.agent, spec.interval, spec.source) == ("breakout_v1", "15m", "store")
    assert spec.min_span_days == 60  # first rerun waits for data beyond the frozen 52d window
    assert spec.vwap_window == 385  # breakout needs window >= lookback+1 to carry closes
    assert all(a.prefer == "taker" for a in spec.arms)  # maker fills aren't momentum evidence
    by_name = {a.name: a for a in spec.arms}
    assert set(by_name) == {"original-taker", "breadth-taker", "combined-er-taker"}
    combined = by_name["combined-er-taker"]
    assert len(spec.arm_coins(combined)) == 20
    assert combined.config["min_efficiency_ratio"] == 0.1
    for a in spec.arms:
        assert a.config["lookback_bars"] == 384 and a.config["exit_lookback_bars"] == 96


# ---------------------------------------------------------------------------
# CLI — ripeness gate fires before any frame/network work
# ---------------------------------------------------------------------------


def test_cli_experiment_unripe_refuses_and_check_only(tmp_path):
    from typer.testing import CliRunner

    from hl_bot.cli.main import app

    spec_path = _write_spec(tmp_path, min_span_days=2)
    _write_store(tmp_path, "BTC", "1m", DAY_BARS)  # < 2d
    _write_store(tmp_path, "ETH", "1m", DAY_BARS)
    res = CliRunner().invoke(
        app, ["experiment", str(spec_path), "--store-root", str(tmp_path)])
    assert res.exit_code == 3
    assert "NOT RIPE" in res.output and "refusing to run" in res.output

    res2 = CliRunner().invoke(
        app, ["experiment", str(spec_path), "--store-root", str(tmp_path), "--check-only"])
    assert res2.exit_code == 3

    _write_store(tmp_path, "BTC", "1m", 3 * DAY_BARS)
    _write_store(tmp_path, "ETH", "1m", 3 * DAY_BARS)
    res3 = CliRunner().invoke(
        app, ["experiment", str(spec_path), "--store-root", str(tmp_path), "--check-only"])
    assert res3.exit_code == 0
    assert "RIPE" in res3.output


def test_cli_experiment_bad_spec_exits_1(tmp_path):
    from typer.testing import CliRunner

    from hl_bot.cli.main import app

    p = _write_spec(tmp_path, typo_key=1)
    res = CliRunner().invoke(app, ["experiment", str(p)])
    assert res.exit_code == 1
    assert "unknown key" in res.output
