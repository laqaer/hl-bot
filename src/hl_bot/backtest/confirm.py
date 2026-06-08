"""Strategy confirmation harness — the G0 gate, as code.

"Confirm a new strategy" must mean one repeatable, adversarial thing, not a
hopeful single backtest. ``confirm_strategy`` runs a candidate three ways and
returns an explicit PASS/FAIL:

1. **Walk-forward.** Fit-window vs a held-out out-of-sample tail. An edge that
   only exists in-sample is overfit and fails.
2. **Cost stress.** Re-price the whole run at maker and at 1x/2x/3x taker
   slippage. An edge that evaporates when costs double was never real.
3. **Verdict.** Confirmed only if the out-of-sample net-of-cost edge clears the
   threshold under the *preferred* execution AND in-sample agrees AND Sharpe
   clears the bar. Robustness to 2x slippage is reported separately.

This is intentionally strict: it is cheaper to reject a fake edge here than to
discover it live.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from ..agents.base import Agent
from ..db.schema import init_db
from .engine import Backtester, CostModel, Frame, _curve_stats

AgentFactory = Callable[[object], Agent]   # conn -> Agent


@dataclass
class ScenarioResult:
    name: str
    net_pnl: float
    edge_bps: float | None
    sharpe: float | None
    n_trades: int

    def row(self) -> str:
        edge = "—" if self.edge_bps is None else f"{self.edge_bps:+.1f}bps"
        sh = "—" if self.sharpe is None else f"{self.sharpe:+.2f}"
        return f"{self.name:14s} net ${self.net_pnl:+8.2f}  edge {edge:>10s}  sharpe {sh:>7s}  trades {self.n_trades}"


@dataclass
class ConfirmationResult:
    agent: str
    confirmed: bool
    reasons: list[str]
    in_sample: ScenarioResult
    out_of_sample: ScenarioResult
    cost_ladder: list[ScenarioResult] = field(default_factory=list)
    robust_to_2x_slippage: bool = False
    n_frames: int = 0
    prefer: str = "taker"

    def summary(self) -> str:
        verdict = "✅ CONFIRMED" if self.confirmed else "❌ NOT CONFIRMED"
        lines = [
            f"{verdict}  {self.agent}  ({self.n_frames} frames, prefer={self.prefer})",
            "  walk-forward:",
            f"    {self.in_sample.row()}",
            f"    {self.out_of_sample.row()}",
            "  cost stress (full sample):",
        ]
        lines += [f"    {s.row()}" for s in self.cost_ladder]
        lines.append(f"  robust to 2x slippage: {self.robust_to_2x_slippage}")
        for r in self.reasons:
            lines.append(f"  - {r}")
        return "\n".join(lines)


def _run(
    factory: AgentFactory, frames: list[Frame], cost: CostModel,
    name: str, periods_per_year: float, starting_capital: float,
) -> ScenarioResult:
    conn = init_db(":memory:")
    bt = Backtester(cost, conn=conn, starting_capital=starting_capital)
    res = bt.run(factory(conn), frames)
    sharpe, _, _ = _curve_stats(res.equity_curve, periods_per_year=periods_per_year)
    sc = res.scorecard
    return ScenarioResult(name=name, net_pnl=sc.net_pnl, edge_bps=sc.edge_bps,
                          sharpe=sharpe, n_trades=sc.n_trades)


def confirm_strategy(
    factory: AgentFactory,
    frames: list[Frame],
    *,
    prefer: str = "taker",
    oos_fraction: float = 0.3,
    min_edge_bps: float = 3.0,
    min_sharpe: float = 1.0,
    periods_per_year: float = 8_760,
    starting_capital: float = 1_000.0,
) -> ConfirmationResult:
    """Run the walk-forward + cost-stress confirmation. ``prefer`` ('taker' or
    'maker') selects the execution basis the PASS/FAIL verdict is judged on."""
    n = len(frames)
    reasons: list[str] = []
    pref_cost = CostModel(maker=(prefer == "maker"))

    if n < 10:
        empty = ScenarioResult("insufficient", 0.0, None, None, 0)
        return ConfirmationResult(
            agent="?", confirmed=False, reasons=[f"only {n} frames; need >=10"],
            in_sample=empty, out_of_sample=empty, n_frames=n, prefer=prefer,
        )

    split = max(1, int(n * (1 - oos_fraction)))
    in_frames, oos_frames = frames[:split], frames[split:]

    in_sample = _run(factory, in_frames, pref_cost, f"in-sample({prefer})",
                     periods_per_year, starting_capital)
    out_of_sample = _run(factory, oos_frames, pref_cost, f"oos({prefer})",
                         periods_per_year, starting_capital)

    ladder = [
        _run(factory, frames, CostModel(maker=True), "maker",
             periods_per_year, starting_capital),
        _run(factory, frames, CostModel(maker=False, slippage_bps=2.0), "taker-1x",
             periods_per_year, starting_capital),
        _run(factory, frames, CostModel(maker=False, slippage_bps=4.0), "taker-2x",
             periods_per_year, starting_capital),
        _run(factory, frames, CostModel(maker=False, slippage_bps=6.0), "taker-3x",
             periods_per_year, starting_capital),
    ]
    agent_name = factory(init_db(":memory:")).name

    taker_2x = next(s for s in ladder if s.name == "taker-2x")
    robust_2x = (taker_2x.edge_bps is not None and taker_2x.edge_bps > 0)

    # Verdict
    ok_oos_edge = out_of_sample.edge_bps is not None and out_of_sample.edge_bps >= min_edge_bps
    ok_in_edge = in_sample.edge_bps is not None and in_sample.edge_bps >= min_edge_bps
    ok_sharpe = out_of_sample.sharpe is not None and out_of_sample.sharpe >= min_sharpe

    if not ok_in_edge:
        reasons.append(f"in-sample edge {_fmt(in_sample.edge_bps)} < {min_edge_bps:+.0f}bps")
    if not ok_oos_edge:
        reasons.append(f"out-of-sample edge {_fmt(out_of_sample.edge_bps)} < {min_edge_bps:+.0f}bps (overfit/none)")
    if not ok_sharpe:
        reasons.append(f"oos sharpe {_fmtn(out_of_sample.sharpe)} < {min_sharpe:.1f}")
    if not robust_2x:
        reasons.append("edge does not survive 2x taker slippage (info; not required if maker-only)")

    confirmed = ok_in_edge and ok_oos_edge and ok_sharpe
    if confirmed and not reasons:
        reasons.append(f"clears +{min_edge_bps:.0f}bps in & out of sample with sharpe >= {min_sharpe:.1f}")

    return ConfirmationResult(
        agent=agent_name, confirmed=confirmed, reasons=reasons,
        in_sample=in_sample, out_of_sample=out_of_sample, cost_ladder=ladder,
        robust_to_2x_slippage=robust_2x, n_frames=n, prefer=prefer,
    )


def _fmt(v: float | None) -> str:
    return "—" if v is None else f"{v:+.1f}bps"


def _fmtn(v: float | None) -> str:
    return "—" if v is None else f"{v:+.2f}"
