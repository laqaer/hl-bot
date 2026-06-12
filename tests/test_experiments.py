"""Pre-registered experiment specs (B-G014 / B-EDGE2b) — offline, tmp dirs.

What must hold for a frozen spec to be trustworthy evidence machinery:
  1. A typo anywhere in a spec is a hard error, never a silent default —
     a mislabeled arm would poison the recorded evidence.
  2. Ripeness is judged on the WORST coin across the full arm universe; a
     missing or short store series means NOT ripe — and so does a gappy one
     (span alone would "ripen" a sample a harvester outage already corrupted;
     ``days > 0`` judges only the window the run would actually use).
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
    ArmResult,
    check_ripeness,
    experiment_record,
    load_spec,
    run_experiment,
    write_experiment_record,
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


def _write_gappy_store(
    root: Path, coin: str, interval: str, n_bars: int,
    gap_start: int, gap_len: int, step: int = MIN,
) -> None:
    bars = [_bar(i * step) for i in range(n_bars)
            if not (gap_start <= i < gap_start + gap_len)]
    save_store(store_path(coin, interval, root), bars)


def test_ripeness_gaps_block_even_when_span_is_long_enough(tmp_path):
    # 3d span >= 2d min, but a 100-bar hole = 2.3% missing > the 1% default:
    # a harvester outage must not "ripen" by waiting out the span.
    spec = load_spec(_write_spec(tmp_path, coins=["BTC"], min_span_days=2))
    assert spec.max_missing_pct == 1.0  # default
    _write_gappy_store(tmp_path, "BTC", "1m", 3 * DAY_BARS + 1, gap_start=2000, gap_len=100)
    rep = check_ripeness(spec, root=tmp_path)
    assert rep.min_span == pytest.approx(3.0)  # span alone would have passed
    assert not rep.gaps_ok and not rep.ripe
    assert rep.worst_gap.missing == 100
    s = rep.summary()
    assert "NOT RIPE" in s and "BTC_1m" in s and "missing" in s and "allowed" in s
    # the hole is permanent, but the store growing dilutes it under the cap
    _write_gappy_store(tmp_path, "BTC", "1m", 15 * DAY_BARS + 1, gap_start=2000, gap_len=100)
    rep2 = check_ripeness(spec, root=tmp_path)
    assert rep2.ripe
    assert "100 missing" in rep2.summary()  # ripe, but the gap stays disclosed


def test_ripeness_days_window_trims_old_gaps(tmp_path):
    # 4d stored with a hole in day 1; the spec only uses the last 2d (days=2),
    # which are clean — an out-of-window gap must not block the spec forever.
    spec = load_spec(_write_spec(tmp_path, coins=["BTC"], min_span_days=1, days=2))
    _write_gappy_store(tmp_path, "BTC", "1m", 4 * DAY_BARS + 1, gap_start=100, gap_len=300)
    rep = check_ripeness(spec, root=tmp_path)
    assert rep.ripe and rep.worst_gap.missing == 0
    assert rep.min_span == pytest.approx(2.0)
    # the same store judged whole (days=0) is blocked by that gap
    spec0 = load_spec(_write_spec(tmp_path, coins=["BTC"], min_span_days=1, days=0))
    rep0 = check_ripeness(spec0, root=tmp_path)
    assert not rep0.ripe and rep0.worst_gap.missing == 300


def test_ripeness_spec_can_widen_the_gap_allowance(tmp_path):
    spec = load_spec(_write_spec(
        tmp_path, coins=["BTC"], min_span_days=2, max_missing_pct=5.0))
    _write_gappy_store(tmp_path, "BTC", "1m", 3 * DAY_BARS + 1, gap_start=2000, gap_len=100)
    assert check_ripeness(spec, root=tmp_path).ripe  # 2.3% <= 5% allowed


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
    assert spec.max_missing_pct == 1.0  # a harvester-outage hole can't ripen away
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
    assert spec.max_missing_pct == 1.0
    assert spec.vwap_window == 385  # breakout needs window >= lookback+1 to carry closes
    assert all(a.prefer == "taker" for a in spec.arms)  # maker fills aren't momentum evidence
    by_name = {a.name: a for a in spec.arms}
    assert set(by_name) == {"original-taker", "breadth-taker", "combined-er-taker"}
    combined = by_name["combined-er-taker"]
    assert len(spec.arm_coins(combined)) == 20
    assert combined.config["min_efficiency_ratio"] == 0.1
    for a in spec.arms:
        assert a.config["lookback_bars"] == 384 and a.config["exit_lookback_bars"] == 96


def test_b_edge2_1h_spec_pins():
    spec = load_spec(SPECS_DIR / "b_edge2_1h.json")
    assert (spec.agent, spec.interval, spec.source, spec.days) == ("breakout_v1", "1h", "store", 0.0)
    assert spec.min_span_days >= 150  # bumped after each rerun so the next waits for fresh data
    assert spec.max_missing_pct == 1.0
    assert spec.vwap_window == 97  # breakout needs window >= lookback+1 to carry closes
    assert all(a.prefer == "taker" for a in spec.arms)  # maker fills aren't momentum evidence
    by_name = {a.name: a for a in spec.arms}
    assert set(by_name) == {"original-taker", "breadth-taker", "combined-er-taker"}
    combined = by_name["combined-er-taker"]
    assert len(spec.arm_coins(combined)) == 20
    assert combined.config["min_efficiency_ratio"] == 0.1
    # same TIME horizons as b_edge2b's 15m arms (96h channel / 24h exit / 24h ER),
    # rescaled to 1h bars — the whole point is span-for-cadence, not new knobs
    assert combined.config["er_lookback_bars"] == 24
    for a in spec.arms:
        assert a.config["lookback_bars"] == 96 and a.config["exit_lookback_bars"] == 24
    # different exit cadence = different experiment: the spec must say it can't
    # adjudicate the 15m promotion case
    assert "b_edge2b" in spec.decision


def test_b_edge3_spec_pins():
    spec = load_spec(SPECS_DIR / "b_edge3.json")
    assert (spec.agent, spec.interval, spec.source, spec.days) == ("xmom_v1", "1h", "store", 0.0)
    assert spec.min_span_days >= 150  # bumped after each rerun so the next waits for fresh data
    assert spec.max_missing_pct == 1.0
    assert spec.vwap_window == 337  # xmom needs window >= lookback+skip+1 to carry closes
    assert all(a.prefer == "taker" for a in spec.arms)  # maker fills aren't momentum evidence
    by_name = {a.name: a for a in spec.arms}
    assert set(by_name) == {"combined-taker", "original-taker", "breadth-taker"}
    assert len(spec.arm_coins(by_name["combined-taker"])) == 20
    assert len(spec.arm_coins(by_name["original-taker"])) == 10
    assert len(spec.arm_coins(by_name["breadth-taker"])) == 10
    for a in spec.arms:
        # lb=336 was the selected knob; skip stays 0 (skip_bars=24 HURT, Iter 72)
        assert a.config["lookback_bars"] == 336 and a.config["skip_bars"] == 0


# ---------------------------------------------------------------------------
# experiment_record / write_experiment_record — the verdict must outlive stdout
# ---------------------------------------------------------------------------


def test_experiment_record_is_self_contained_and_serializable(tmp_path):
    spec = load_spec(_write_spec(
        tmp_path,
        min_span_days=2,
        decision="flip if w240 beats baseline",
        arms=[
            {"name": "base"},
            {"name": "w240-maker", "vwap_window": 240, "prefer": "maker",
             "maker_fill": "resting", "config": {"stop_loss_pct": 0.03}},
        ],
    ))
    _write_store(tmp_path, "BTC", "1m", 3 * DAY_BARS + 1)
    _write_store(tmp_path, "ETH", "1m", 3 * DAY_BARS + 1)
    rep = check_ripeness(spec, root=tmp_path)
    # one result carries a cost ladder + a None sharpe to pin serialization
    s = ScenarioResult("base", 0.0, 1.0, 1.0, 30, pocket_share=1.04,
                       pocket_window="2026-04-21..2026-06-03",
                       pocket_window_frac=0.25)
    full = ConfirmationResult(
        agent="base", confirmed=True, reasons=["ok"], in_sample=s, out_of_sample=s,
        cost_ladder=[ScenarioResult("taker-2x", -1.0, -2.0, None, 10)],
        robust_to_2x_slippage=True, n_frames=2,
    )
    results = [ArmResult(arm=spec.arms[0], result=full),
               ArmResult(arm=spec.arms[1], result=_fake_result("w240-maker"))]
    rec = experiment_record(
        spec, rep, results,
        ran_at="2026-06-20T12:03:01Z", spec_sha256="ab" * 32,
        forced=False, code_rev="deadbeef",
    )
    assert json.loads(json.dumps(rec)) == rec  # round-trips losslessly
    assert rec["spec"]["name"] == "t_spec" and rec["spec"]["sha256"] == "ab" * 32
    assert rec["spec"]["thresholds"] == {
        "min_edge_bps": 3.0, "min_sharpe": 1.0, "min_trades": 20}
    assert rec["spec"]["decision"] == "flip if w240 beats baseline"
    assert rec["forced"] is False and rec["code_rev"] == "deadbeef"
    assert rec["ran_at"] == "2026-06-20T12:03:01Z"
    assert rec["ripeness"]["ripe"] is True
    assert rec["ripeness"]["min_span_days_required"] == 2
    assert {sp["coin"] for sp in rec["ripeness"]["spans"]} == {"BTC", "ETH"}
    assert all(sp["missing_pct"] == 0.0 for sp in rec["ripeness"]["spans"])
    base, w240 = rec["arms"]
    # resolved knobs (spec inheritance applied), not the raw arm fields
    assert base["coins"] == ["BTC", "ETH"] and base["vwap_window"] == 60
    assert (base["prefer"], base["maker_fill"]) == ("taker", "optimistic")
    assert base["confirmed"] is True and base["reasons"] == ["ok"]
    assert base["robust_to_2x_slippage"] is True and base["n_frames"] == 2
    assert base["in_sample"]["n_trades"] == 30
    # the pocket diagnostic persists in the record (asdict carries it)
    assert base["in_sample"]["pocket_share"] == 1.04
    assert base["in_sample"]["pocket_window"] == "2026-04-21..2026-06-03"
    assert base["cost_ladder"][0] == {
        "name": "taker-2x", "net_pnl": -1.0, "edge_bps": -2.0,
        "sharpe": None, "n_trades": 10, "pocket_share": None,
        "pocket_window": None, "pocket_window_frac": None}
    assert w240["vwap_window"] == 240 and w240["maker_fill"] == "resting"
    assert w240["config"] == {"stop_loss_pct": 0.03}


def test_write_experiment_record_never_overwrites_and_marks_peeks(tmp_path):
    rec = {"spec": {"name": "b_g014"}, "ran_at": "2026-06-20T12:03:01Z", "forced": False}
    d = tmp_path / "results"
    p1 = write_experiment_record(rec, d)
    assert p1.name == "b_g014.20260620T120301Z.json"
    assert json.loads(p1.read_text()) == rec
    p2 = write_experiment_record(rec, d)  # same-second rerun must not clobber
    assert p2 != p1 and p1.exists() and p2.exists()
    peek = write_experiment_record({**rec, "forced": True}, d)
    assert ".peek" in peek.name
    # a hostile spec name can't escape the results dir
    hostile = write_experiment_record(
        {"spec": {"name": "../evil name"}, "ran_at": "", "forced": False}, d)
    assert hostile.parent == d and "/" not in hostile.name


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


# ---------------------------------------------------------------------------
# CLI — every run persists its verdict (peeks visibly flagged)
# ---------------------------------------------------------------------------


def _patch_fake_run(monkeypatch):
    """Fake the runner (no frames/network); the recording seam is what's under test."""
    import hl_bot.backtest.experiments as exps

    def fake_run(spec, *, factory_for, load_frames, confirm_fn=None):
        return [exps.ArmResult(arm=a, result=_fake_result(a.name)) for a in spec.arms]

    monkeypatch.setattr(exps, "run_experiment", fake_run)


def test_cli_experiment_records_verdict(tmp_path, monkeypatch):
    import hashlib

    from typer.testing import CliRunner

    from hl_bot.cli.main import app

    spec_path = _write_spec(tmp_path, min_span_days=2)
    for c in ("BTC", "ETH"):
        _write_store(tmp_path, c, "1m", 3 * DAY_BARS + 1)
    monkeypatch.setenv("HLBOT_DB", str(tmp_path / "t.sqlite"))
    _patch_fake_run(monkeypatch)
    rdir = tmp_path / "results"
    res = CliRunner().invoke(app, [
        "experiment", str(spec_path), "--store-root", str(tmp_path),
        "--results-dir", str(rdir)])
    assert res.exit_code == 0, res.output
    assert "verdict recorded" in res.output and "peek" not in res.output
    files = list(rdir.glob("t_spec.*.json"))
    assert len(files) == 1 and ".peek" not in files[0].name
    rec = json.loads(files[0].read_text())
    assert rec["forced"] is False and rec["ripeness"]["ripe"] is True
    assert [a["name"] for a in rec["arms"]] == ["a1"]
    # the sha pins WHICH frozen spec produced this verdict
    assert rec["spec"]["sha256"] == hashlib.sha256(spec_path.read_bytes()).hexdigest()
    assert rec["code_rev"] is None  # tmp dir isn't a git repo — degrades, never fails


def test_cli_experiment_forced_peek_recorded_as_peek_and_no_record_opts_out(
        tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from hl_bot.cli.main import app

    spec_path = _write_spec(tmp_path, min_span_days=2)
    for c in ("BTC", "ETH"):
        _write_store(tmp_path, c, "1m", DAY_BARS)  # 1d < 2d min: unripe
    monkeypatch.setenv("HLBOT_DB", str(tmp_path / "t.sqlite"))
    _patch_fake_run(monkeypatch)
    rdir = tmp_path / "results"
    res = CliRunner().invoke(app, [
        "experiment", str(spec_path), "--store-root", str(tmp_path),
        "--results-dir", str(rdir), "--force"])
    assert res.exit_code == 0, res.output
    files = list(rdir.glob("*.json"))
    assert len(files) == 1 and ".peek" in files[0].name
    rec = json.loads(files[0].read_text())
    assert rec["forced"] is True and rec["ripeness"]["ripe"] is False

    res2 = CliRunner().invoke(app, [
        "experiment", str(spec_path), "--store-root", str(tmp_path),
        "--results-dir", str(rdir), "--force", "--no-record"])
    assert res2.exit_code == 0, res2.output
    assert len(list(rdir.glob("*.json"))) == 1  # nothing new written
