"""Sweep harness: grid expansion, G0 reuse over synthetic frames, report."""

from __future__ import annotations

from hl_bot.agents.twap_mr import TwapMrAgent
from hl_bot.backtest.engine import Frame
from hl_bot.research.sweep import SweepSpec, render_markdown, run_sweep

HOUR = 3_600_000
COIN = "TST"


def _choppy(n: int = 40) -> list[Frame]:
    frames = []
    closes: list[float] = []
    for i in range(n):
        mid = 103.0 if i % 2 else 100.0
        closes.append(mid)
        frames.append(Frame(
            ts_ms=i * HOUR, mids={COIN: mid}, funding={COIN: 0.0},
            day_ntl_vlm={COIN: 50_000_000.0},
            candles_1h={COIN: {"vwap": 100.0, "sigma": 1.0, "n": 60}},
            closes={COIN: list(closes)},
        ))
    return frames


SPEC = SweepSpec(
    agent="twap_mr_v1", interval="1h", days=2, prefer="maker",
    universes=[[COIN]],
    grid={"entry_sigma": [1.5, 2.0], "max_notional_per_trade": [20.0]},
)


def test_combos_expand_grid():
    combos = SPEC.combos()
    assert len(combos) == 2
    assert {c["entry_sigma"] for c in combos} == {1.5, 2.0}
    assert all(c["max_notional_per_trade"] == 20.0 for c in combos)
    assert SweepSpec(agent="x").combos() == [{}]


def test_run_sweep_ranks_by_oos_edge():
    frames = {_k: _choppy() for _k in [(COIN,)]}
    rows = run_sweep(SPEC, frames,
                     lambda conn, cfg: TwapMrAgent(config=cfg, conn=conn))
    assert len(rows) == 2
    edges = [r.oos_edge_bps for r in rows]
    real = [e for e in edges if e is not None]
    assert real == sorted(real, reverse=True)   # best first


def test_run_sweep_handles_missing_frames():
    rows = run_sweep(SPEC, {}, lambda conn, cfg: TwapMrAgent(config=cfg, conn=conn))
    assert len(rows) == 2
    assert all(not r.confirmed for r in rows)   # no data can never confirm


def test_render_markdown_report():
    frames = {(COIN,): _choppy()}
    rows = run_sweep(SPEC, frames,
                     lambda conn, cfg: TwapMrAgent(config=cfg, conn=conn))
    md = render_markdown(SPEC, rows, date="2026-06-11")
    assert "# Sweep: twap_mr_v1 — 2026-06-11" in md
    assert "| # | verdict |" in md
    assert "combos confirmed" in md
    # gate-integrity message must appear when nothing confirms
    if not any(r.confirmed for r in rows):
        assert "do not loosen the gate" in md
