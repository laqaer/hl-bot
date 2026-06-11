"""CI guard: promotion gates in configs/ can never be quietly weakened.

The autonomous research loop (ralph) is allowed to edit configs, so this test
encodes the documented minimum gate strictness. Loosening a threshold below
these minima must fail CI and therefore requires a deliberate human edit to
THIS file alongside it.
"""

from __future__ import annotations

from pathlib import Path

from hl_bot.supervisor.goals import load_goals

CONFIGS = Path(__file__).resolve().parents[1] / "configs"

# Documented minima (docs/GO_LIVE.md): looser than this is not allowed.
MIN_EDGE_BPS = 3.0
MIN_PAPER_TRADES = 40
MIN_LIVE_TRADES = 20
MIN_DAYS_PAPER = 7.0
MIN_DAYS_LIVE_SMALL = 14.0

# Agents allowed to skip the G0 confirmation for paper->live_small (must be
# justified in the config comment; currently only the WS-liquidation strategy,
# which the candle-replay confirm harness cannot reproduce).
G0_EXEMPT = {"liq_cascade_v1", "funding_arb_v1"}


def _all_goals():
    out = []
    for p in sorted([*CONFIGS.glob("*.yaml"), *CONFIGS.glob("moonshot/*.yaml")]):
        out.extend(load_goals(p))
    return out


def test_every_live_roster_agent_has_a_ladder():
    for g in _all_goals():
        if g.roster != "live":
            continue
        assert g.ladder(), f"{g.agent}: live roster requires a promotion ladder"


def test_paper_to_live_small_stages_meet_minima():
    for g in _all_goals():
        if g.roster != "live":
            continue   # paper/retired roster agents can never enter live execution
        for stage in g.ladder():
            if stage.from_mode != "paper":
                continue
            assert stage.min_days_in_mode >= MIN_DAYS_PAPER, \
                f"{g.agent}: paper min_days_in_mode {stage.min_days_in_mode} < {MIN_DAYS_PAPER}"
            if g.agent not in G0_EXEMPT:
                assert stage.require_g0, f"{g.agent}: paper->live_small must require_g0"
            by_metric = {c.metric: c for c in stage.conditions}
            assert "edge_bps" in by_metric, f"{g.agent}: paper stage must gate on edge_bps"
            assert by_metric["edge_bps"].threshold >= MIN_EDGE_BPS
            assert "n_trades" in by_metric, f"{g.agent}: paper stage must gate on n_trades"
            assert by_metric["n_trades"].threshold >= MIN_PAPER_TRADES
            # Promotion out of paper can only ever read paper evidence — a
            # live-source condition is structurally unsatisfiable there and
            # would silently freeze the ladder.
            for c in stage.conditions:
                assert c.source == "paper", \
                    f"{g.agent}: paper-stage condition {c.metric} must be source: paper"


def test_live_small_to_live_stages_meet_minima():
    for g in _all_goals():
        if g.roster != "live":
            continue
        for stage in g.ladder():
            if stage.from_mode != "live_small":
                continue
            assert stage.min_days_in_mode >= MIN_DAYS_LIVE_SMALL, \
                f"{g.agent}: live_small min_days {stage.min_days_in_mode} < {MIN_DAYS_LIVE_SMALL}"
            by_metric = {c.metric: c for c in stage.conditions}
            assert "edge_bps" in by_metric and by_metric["edge_bps"].threshold >= MIN_EDGE_BPS
            for c in stage.conditions:
                assert c.source == "live", \
                    f"{g.agent}: live_small->{stage.to_mode} must gate on REAL fills only"


def test_every_live_roster_agent_has_loss_guardrail():
    for g in _all_goals():
        if g.roster != "live":
            continue
        pauses = [gr for gr in g.guardrails
                  if gr.metric == "net_pnl" and gr.action in ("pause", "demote")]
        assert pauses, f"{g.agent}: needs a net_pnl pause/demote guardrail"
