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


# ---------------------------------------------------------------------------
# Multi-window robustness — the out-of-time bar, as reusable machinery.
#
# Iteration 20 taught the hard lesson the expensive way: a strategy can clear the
# walk-forward + cost-stress G0 gate on the *trailing* 120d and still reverse sign
# on the immediately-preceding 120d (regime-gated momentum: +8.4bps → −7.8bps
# maker). Trailing-window G0 is therefore *necessary but not sufficient*. A real
# edge survives a fresh, disjoint time window; a window-specific artifact does not.
# ``confirm_across_windows`` makes that test a single call so every future
# candidate must clear it, not just the one window that happened to look good.
# ---------------------------------------------------------------------------


def preferred_full_sample(cr: ConfirmationResult) -> ScenarioResult:
    """The full-sample cost-ladder rung matching the verdict's execution basis
    (maker, or taker-1x for taker). This is the number whose *sign* must be
    stable across windows — a sign flip is the artifact signature."""
    target = "maker" if cr.prefer == "maker" else "taker-1x"
    return next(s for s in cr.cost_ladder if s.name == target)


@dataclass
class WindowResult:
    label: str
    confirmation: ConfirmationResult
    full_sample_edge_bps: float | None


@dataclass
class MultiWindowResult:
    agent: str
    durable: bool
    prefer: str
    windows: list[WindowResult] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)

    def summary(self) -> str:
        verdict = "✅ DURABLE" if self.durable else "❌ NOT DURABLE"
        lines = [f"{verdict}  {self.agent}  ({len(self.windows)} windows, prefer={self.prefer})"]
        for w in self.windows:
            cr = w.confirmation
            mark = "✅" if cr.confirmed else "❌"
            edge = _fmt(w.full_sample_edge_bps)
            lines.append(
                f"  {mark} {w.label:22s} full {edge:>10s}  "
                f"in {_fmt(cr.in_sample.edge_bps)}  oos {_fmt(cr.out_of_sample.edge_bps)}"
            )
        for r in self.reasons:
            lines.append(f"  - {r}")
        return "\n".join(lines)


def confirm_across_windows(
    factory: AgentFactory,
    windows: list[tuple[str, list[Frame]]],
    *,
    prefer: str = "taker",
    **confirm_kwargs: object,
) -> MultiWindowResult:
    """Run the G0 confirmation on each of several disjoint historical ``windows``
    and return a single durability verdict.

    ``windows`` is ``[(label, frames), ...]`` — each ``frames`` a distinct,
    ideally non-overlapping time window. The strategy is **durable** only if:

    1. there are at least two windows (one window is the trap Iteration 20 fell
       into — a trailing-only PASS), AND
    2. *every* window is individually ``confirmed`` (walk-forward + sharpe), AND
    3. the preferred-execution full-sample edge is positive in *every* window —
       i.e. it never flips sign. A sign flip across windows is the textbook
       artifact signature and is called out explicitly in the reasons.
    """
    results = [
        WindowResult(
            label=label,
            confirmation=(cr := confirm_strategy(factory, frames, prefer=prefer, **confirm_kwargs)),  # type: ignore[arg-type]
            full_sample_edge_bps=preferred_full_sample(cr).edge_bps if cr.cost_ladder else None,
        )
        for label, frames in windows
    ]
    agent_name = results[0].confirmation.agent if results else "?"
    reasons: list[str] = []

    if len(results) < 2:
        reasons.append(f"only {len(results)} window(s); need >=2 disjoint windows to claim durability")
        return MultiWindowResult(agent=agent_name, durable=False, prefer=prefer,
                                 windows=results, reasons=reasons)

    not_confirmed = [w.label for w in results if not w.confirmation.confirmed]
    if not_confirmed:
        reasons.append(f"not confirmed in: {', '.join(not_confirmed)}")

    edges = [w.full_sample_edge_bps for w in results]
    pos = [e for e in edges if e is not None and e > 0]
    neg = [e for e in edges if e is not None and e <= 0]
    if pos and neg:
        reasons.append(
            f"full-sample edge FLIPS SIGN across windows (+{max(pos):.1f} … {min(neg):+.1f}bps) "
            "— window-specific artifact, not a durable edge"
        )
    all_positive = bool(edges) and all(e is not None and e > 0 for e in edges)

    durable = not not_confirmed and all_positive
    if durable and not reasons:
        reasons.append(f"confirmed with positive {prefer} edge in all {len(results)} disjoint windows")

    return MultiWindowResult(agent=agent_name, durable=durable, prefer=prefer,
                             windows=results, reasons=reasons)


def _fmt(v: float | None) -> str:
    return "—" if v is None else f"{v:+.1f}bps"


def _fmtn(v: float | None) -> str:
    return "—" if v is None else f"{v:+.2f}"
