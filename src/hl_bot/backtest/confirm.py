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
   clears the bar AND both splits have enough trades to mean anything — a
   +10bps edge on 2 trades is noise, not evidence (a real 1d-cadence carry run
   "passed" exactly that way before the floor existed). Robustness to 2x
   slippage is reported separately, and so is profit time-concentration
   (``pocket_share`` — see ``max_window_pnl_share``): a PASS whose net lives
   in one quarter-of-sample window is a regime pocket wearing a G0 badge,
   the exact shape that killed xmom and the 1h breakout read (Iters 74–75).
   Informational only; the verdict logic does not consume it.

This is intentionally strict: it is cheaper to reject a fake edge here than to
discover it live.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from ..agents.base import Agent
from ..db.schema import init_db
from .engine import Backtester, CostModel, Frame, _curve_stats

AgentFactory = Callable[[object], Agent]   # conn -> Agent

# Window length for the profit-concentration diagnostic, as a fraction of the
# scenario's calendar span. 0.25 makes the reading self-normalizing: a diffuse
# edge earns ~0.25 of its net in its best quarter-of-sample window; an edge
# that is one regime pocket earns ~1.0 there (and >1.0 when the rest of the
# sample loses). Two strategy families (xmom Iter 74, Donchian Iter 75) passed
# G0 on samples whose entire profit later proved to be one Apr–Jun pocket —
# this metric makes that shape a number instead of an eyeball call.
POCKET_WINDOW_FRAC = 0.25


def max_window_pnl_share(
    equity_curve: list[tuple[int, float]],
    *,
    window_frac: float = POCKET_WINDOW_FRAC,
) -> tuple[float, int, int] | None:
    """Largest share of total net PnL earned in any contiguous window spanning
    ``window_frac`` of the curve's calendar time.

    Returns ``(share, start_ts_ms, end_ts_ms)`` for the best window, or
    ``None`` when the run is too short to judge (<3 points) or lost money
    overall (concentration of a loss is not the diagnostic this exists for).
    O(n) via a sliding-window minimum over the equity series.
    """
    if len(equity_curve) < 3:
        return None
    t_first, e_first = equity_curve[0]
    t_last, e_last = equity_curve[-1]
    total, span_ms = e_last - e_first, t_last - t_first
    if total <= 0 or span_ms <= 0:
        return None
    window_ms = span_ms * window_frac
    mins: deque[int] = deque()   # candidate start indices, equity strictly increasing
    best: tuple[float, int, int] | None = None
    for j, (tj, ej) in enumerate(equity_curve):
        while mins and tj - equity_curve[mins[0]][0] > window_ms:
            mins.popleft()
        if mins:
            gain = ej - equity_curve[mins[0]][1]
            if best is None or gain > best[0]:
                best = (gain, equity_curve[mins[0]][0], tj)
        # Pop equal equities too: the surviving (later) index gives the
        # tighter window for the same gain.
        while mins and equity_curve[mins[-1]][1] >= ej:
            mins.pop()
        mins.append(j)
    if best is None or best[0] <= 0:
        return None
    return best[0] / total, best[1], best[2]


def _utc_date(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, tz=UTC).strftime("%Y-%m-%d")


@dataclass
class ScenarioResult:
    name: str
    net_pnl: float
    edge_bps: float | None
    sharpe: float | None
    n_trades: int
    # Profit-concentration diagnostic (informational, never gates the verdict):
    # share of net PnL earned in the best pocket_window_frac-of-sample window,
    # the window's UTC dates, and the fraction the share was computed at —
    # recorded so a future frac change can't silently re-define old records.
    pocket_share: float | None = None
    pocket_window: str | None = None
    pocket_window_frac: float | None = None

    def row(self) -> str:
        edge = "—" if self.edge_bps is None else f"{self.edge_bps:+.1f}bps"
        sh = "—" if self.sharpe is None else f"{self.sharpe:+.2f}"
        base = f"{self.name:14s} net ${self.net_pnl:+8.2f}  edge {edge:>10s}  sharpe {sh:>7s}  trades {self.n_trades}"
        if self.pocket_share is not None:
            base += f"  pocket {self.pocket_share:.2f} ({self.pocket_window})"
        return base


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
        scenarios = [self.in_sample, self.out_of_sample, *self.cost_ladder]
        if any(s.pocket_share is not None for s in scenarios):
            frac = next(s.pocket_window_frac for s in scenarios if s.pocket_share is not None)
            lines.append(
                f"  pocket = share of net PnL in the best {frac:.0%}-of-sample window"
                f" (~{frac:.2f} diffuse, ~1 one pocket, >1 rest of sample loses)"
            )
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
    pocket = max_window_pnl_share(res.equity_curve)
    share = window = frac = None
    if pocket is not None:
        share = pocket[0]
        window = f"{_utc_date(pocket[1])}..{_utc_date(pocket[2])}"
        frac = POCKET_WINDOW_FRAC
    return ScenarioResult(name=name, net_pnl=sc.net_pnl, edge_bps=sc.edge_bps,
                          sharpe=sharpe, n_trades=sc.n_trades,
                          pocket_share=share, pocket_window=window,
                          pocket_window_frac=frac)


def confirm_strategy(
    factory: AgentFactory,
    frames: list[Frame],
    *,
    prefer: str = "taker",
    oos_fraction: float = 0.3,
    min_edge_bps: float = 3.0,
    min_sharpe: float = 1.0,
    min_trades: int = 20,
    periods_per_year: float = 8_760,
    starting_capital: float = 1_000.0,
    maker_fill: str = "optimistic",
) -> ConfirmationResult:
    """Run the walk-forward + cost-stress confirmation. ``prefer`` ('taker' or
    'maker') selects the execution basis the PASS/FAIL verdict is judged on.
    ``min_trades`` is the per-split sample floor: each of in-sample and
    out-of-sample must contain at least this many trades or the verdict is
    FAIL regardless of edge. ``maker_fill`` ('optimistic'/'resting'/
    'resting-close') sets the maker fill realism for every maker-priced arm
    (see ``CostModel``)."""
    n = len(frames)
    reasons: list[str] = []
    pref_cost = CostModel(maker=(prefer == "maker"), maker_fill=maker_fill)

    if n < 10:
        empty = ScenarioResult("insufficient", 0.0, None, None, 0)
        return ConfirmationResult(
            agent="?", confirmed=False, reasons=[f"only {n} frames; need >=10"],
            in_sample=empty, out_of_sample=empty, n_frames=n, prefer=prefer,
        )

    split = max(1, int(n * (1 - oos_fraction)))
    in_frames, oos_frames = frames[:split], frames[split:]

    pref_label = pref_cost.exec_label
    in_sample = _run(factory, in_frames, pref_cost, f"in-sample({pref_label})",
                     periods_per_year, starting_capital)
    out_of_sample = _run(factory, oos_frames, pref_cost, f"oos({pref_label})",
                         periods_per_year, starting_capital)

    maker_cost = CostModel(maker=True, maker_fill=maker_fill)
    ladder = [
        _run(factory, frames, maker_cost, maker_cost.exec_label,
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
    ok_in_n = in_sample.n_trades >= min_trades
    ok_oos_n = out_of_sample.n_trades >= min_trades

    if not ok_in_edge:
        reasons.append(f"in-sample edge {_fmt(in_sample.edge_bps)} < {min_edge_bps:+.0f}bps")
    if not ok_oos_edge:
        reasons.append(f"out-of-sample edge {_fmt(out_of_sample.edge_bps)} < {min_edge_bps:+.0f}bps (overfit/none)")
    if not ok_sharpe:
        reasons.append(f"oos sharpe {_fmtn(out_of_sample.sharpe)} < {min_sharpe:.1f}")
    if not ok_in_n:
        reasons.append(f"in-sample trades {in_sample.n_trades} < {min_trades} (sample too thin to judge)")
    if not ok_oos_n:
        reasons.append(f"out-of-sample trades {out_of_sample.n_trades} < {min_trades} (sample too thin to judge)")
    if not robust_2x:
        reasons.append("edge does not survive 2x taker slippage (info; not required if maker-only)")

    confirmed = ok_in_edge and ok_oos_edge and ok_sharpe and ok_in_n and ok_oos_n
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
