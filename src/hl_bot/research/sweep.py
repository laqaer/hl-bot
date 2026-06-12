"""Nightly parameter/universe sweeps through the G0 confirmation harness.

The research meta-workflow's data-heavy leg: a sweep spec (YAML) declares an
agent, coin universes and a parameter grid; every combination is run through
``confirm_strategy`` (the same walk-forward + cost-stress gate used for
promotion) over cached real history. Results land as JSON (machine) and a
ranked markdown report (committed to ``research/results/`` so every Claude /
ralph session starts from fresh evidence).

Spec format (configs/sweeps/<name>.yaml):

    agent: xfund_carry_v1
    interval: 1h
    days: 180
    prefer: maker
    universes:
      - [BTC, ETH, SOL, HYPE]
      - [BTC, ETH, SOL, HYPE, DOGE, XRP, WIF, kPEPE]
    grid:
      enter_funding_per_hr: [0.00008, 0.0001, 0.00015]
      top_k: [2, 3]
"""

from __future__ import annotations

import itertools
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from ..backtest.confirm import ConfirmationResult, confirm_strategy
from ..backtest.engine import Frame

PERIODS_PER_YEAR = {"1m": 525_600, "5m": 105_120, "15m": 35_040,
                    "1h": 8_760, "4h": 2_190, "1d": 365}


@dataclass
class SweepSpec:
    agent: str
    interval: str = "1h"
    days: int = 180
    prefer: str = "maker"
    min_edge_bps: float = 3.0
    min_sharpe: float = 1.0
    universes: list[list[str]] = field(default_factory=list)
    grid: dict[str, list[Any]] = field(default_factory=dict)

    @classmethod
    def load(cls, path: str | Path) -> SweepSpec:
        raw = yaml.safe_load(Path(path).read_text()) or {}
        return cls(**raw)

    def combos(self) -> list[dict[str, Any]]:
        if not self.grid:
            return [{}]
        keys = sorted(self.grid)
        return [dict(zip(keys, vals, strict=True))
                for vals in itertools.product(*(self.grid[k] for k in keys))]


@dataclass
class SweepRow:
    universe: list[str]
    params: dict[str, Any]
    confirmed: bool
    oos_edge_bps: float | None
    oos_sharpe: float | None
    oos_net_pnl: float
    n_trades: int
    reasons: list[str]

    @classmethod
    def from_result(cls, universe: list[str], params: dict[str, Any],
                    res: ConfirmationResult) -> SweepRow:
        return cls(
            universe=universe, params=params, confirmed=res.confirmed,
            oos_edge_bps=res.out_of_sample.edge_bps,
            oos_sharpe=res.out_of_sample.sharpe,
            oos_net_pnl=res.out_of_sample.net_pnl,
            n_trades=res.out_of_sample.n_trades,
            reasons=res.reasons,
        )


def run_sweep(
    spec: SweepSpec,
    frames_by_universe: dict[tuple[str, ...], list[Frame]],
    agent_factory,
) -> list[SweepRow]:
    """Run every (universe x params) combo through the G0 gate. Pure given
    frames; ``agent_factory(conn, cfg) -> Agent`` is the runner-style factory."""
    rows: list[SweepRow] = []
    per_year = PERIODS_PER_YEAR.get(spec.interval, 8_760)
    for universe in spec.universes or [[]]:
        frames = frames_by_universe.get(tuple(universe), [])
        for params in spec.combos():
            res = confirm_strategy(
                lambda conn, _p=params: agent_factory(conn, dict(_p)),
                frames, prefer=spec.prefer,
                min_edge_bps=spec.min_edge_bps, min_sharpe=spec.min_sharpe,
                periods_per_year=per_year,
            )
            rows.append(SweepRow.from_result(universe, params, res))
    rows.sort(key=lambda r: (r.oos_edge_bps is None,
                             -(r.oos_edge_bps or float("-inf"))))
    return rows


def render_markdown(spec: SweepSpec, rows: list[SweepRow], *, date: str | None = None) -> str:
    """Ranked, human/agent-readable sweep report for research/results/."""
    date = date or time.strftime("%Y-%m-%d")
    lines = [
        f"# Sweep: {spec.agent} — {date}",
        "",
        f"- dataset: {spec.days}d of {spec.interval} candles, prefer={spec.prefer}",
        f"- gate: OOS edge ≥ {spec.min_edge_bps} bps, sharpe ≥ {spec.min_sharpe}, 2x-slippage robust",
        f"- combos: {len(rows)} (ranked by out-of-sample edge)",
        "",
        "| # | verdict | OOS edge (bps) | OOS sharpe | OOS net | trades | universe | params |",
        "|---:|---|---:|---:|---:|---:|---|---|",
    ]
    for i, r in enumerate(rows, 1):
        edge = "—" if r.oos_edge_bps is None else f"{r.oos_edge_bps:+.1f}"
        sharpe = "—" if r.oos_sharpe is None else f"{r.oos_sharpe:+.2f}"
        verdict = "✅" if r.confirmed else "❌"
        lines.append(
            f"| {i} | {verdict} | {edge} | {sharpe} | {r.oos_net_pnl:+.2f} "
            f"| {r.n_trades} | {','.join(r.universe)} | `{json.dumps(r.params)}` |")
    confirmed = [r for r in rows if r.confirmed]
    lines += [
        "",
        f"**{len(confirmed)}/{len(rows)} combos confirmed.**",
    ]
    if confirmed:
        best = confirmed[0]
        lines += [
            "",
            "Next actions:",
            f"- Best combo `{json.dumps(best.params)}` on `{','.join(best.universe)}` "
            f"→ consider promoting into configs/agent_overrides.json and stamping "
            f"`hlbot confirm --agent {spec.agent} --prefer {spec.prefer} --record`.",
        ]
    else:
        lines += ["", "No combo cleared the gate — do not loosen the gate; "
                      "improve the strategy or the execution model."]
    return "\n".join(lines) + "\n"


def write_outputs(
    spec: SweepSpec, rows: list[SweepRow], *,
    json_dir: str | Path, md_dir: str | Path, date: str | None = None,
) -> tuple[Path, Path]:
    date = date or time.strftime("%Y-%m-%d")
    jd, md = Path(json_dir), Path(md_dir)
    jd.mkdir(parents=True, exist_ok=True)
    md.mkdir(parents=True, exist_ok=True)
    jpath = jd / f"{date}_{spec.agent}.json"
    jpath.write_text(json.dumps([r.__dict__ for r in rows], indent=1, default=str))
    mpath = md / f"{date}_{spec.agent}.md"
    mpath.write_text(render_markdown(spec, rows, date=date))
    return jpath, mpath
