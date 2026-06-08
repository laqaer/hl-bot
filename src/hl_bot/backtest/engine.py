"""Backtest engine: replay an Agent over historical frames with a cost model.

Design goals
------------
* **Reuse production code.** Agents are driven through their real ``decide()``
  method and the resulting fills are scored with the same ``score_agent`` used
  live, so a backtest can never silently disagree with production accounting.
* **Honest costs.** Every simulated entry/exit pays a fee and slippage; held
  positions accrue funding. Taker vs maker execution is a single flag, because
  the central open question for this book is whether its negative edge is a
  strategy problem or an *execution* (taker) problem.
* **Simulated clock.** Agents call ``time.time()`` directly to compute hold
  durations and read their own decision audit log. The engine freezes
  ``time.time()`` to each frame's timestamp so hold/stop/age logic behaves
  exactly as it would live, while the decision/fill rows it writes carry the
  simulated timestamp.

Assumptions (documented so results are interpretable, not hidden):
* ``Frame.funding[coin]`` is the funding rate *for that bar's interval* (not
  annualized). Funding is folded into a position's realized PnL on close — this
  also fixes the live measurement gap where per-agent scorecards ignore funding.
* Orders always fill at ``mid`` adjusted by slippage (no partial fills, no queue
  position). Maker mode assumes the post-only limit rests and fills at mid with
  zero slippage — an optimistic but useful upper bound for "what if we stopped
  crossing the spread".
* One position per (agent, coin); a same-direction re-entry averages in.
"""

from __future__ import annotations

import contextlib
import sqlite3
import time
from dataclasses import dataclass, field

from ..agents.base import Agent, MarketView
from ..agents.decisions import Decision, log_decision
from ..db.schema import init_db
from ..scoring.metrics import Scorecard, score_agent

# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------


@dataclass
class Frame:
    """One time-slice of market state fed to an agent."""

    ts_ms: int
    mids: dict[str, float]
    funding: dict[str, float] = field(default_factory=dict)          # per-bar rate
    day_ntl_vlm: dict[str, float] = field(default_factory=dict)
    open_interest: dict[str, float] = field(default_factory=dict)
    candles_1h: dict[str, dict] = field(default_factory=dict)        # coin -> {vwap, sigma}
    closes: dict[str, list[float]] = field(default_factory=dict)     # coin -> trailing closes
    spot_mids: dict[str, float] = field(default_factory=dict)
    liquidations: list[dict] = field(default_factory=list)


@dataclass(frozen=True)
class CostModel:
    """Execution cost assumptions.

    Hyperliquid base fees are ~3.5 bps taker / ~1.0 bp maker (lower at volume).
    ``slippage_bps`` is the half-spread + impact crossed on a taker order.
    """

    taker_fee_bps: float = 4.5
    maker_fee_bps: float = 1.0
    slippage_bps: float = 2.0
    maker: bool = False

    @property
    def fee_bps(self) -> float:
        return self.maker_fee_bps if self.maker else self.taker_fee_bps

    @property
    def slip(self) -> float:
        return 0.0 if self.maker else self.slippage_bps / 10_000.0

    @property
    def fee_rate(self) -> float:
        return self.fee_bps / 10_000.0


# ---------------------------------------------------------------------------
# Internal position book
# ---------------------------------------------------------------------------


@dataclass
class _Pos:
    side: str            # 'B' long / 'A' short
    sz: float            # absolute size
    entry_px: float
    entry_ts_ms: int
    funding_accrued: float = 0.0

    @property
    def signed(self) -> float:
        return self.sz if self.side == "B" else -self.sz


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


@dataclass
class BacktestResult:
    agent: str
    scorecard: Scorecard
    equity_curve: list[tuple[int, float]]   # (ts_ms, equity)
    sharpe: float | None
    max_drawdown: float | None
    calmar: float | None
    n_bars: int
    starting_capital: float
    cost: CostModel

    @property
    def net_pnl(self) -> float:
        return self.scorecard.net_pnl

    @property
    def edge_bps(self) -> float | None:
        return self.scorecard.edge_bps

    def summary(self) -> str:
        sc = self.scorecard
        sh = "—" if self.sharpe is None else f"{self.sharpe:+.2f}"
        dd = "—" if self.max_drawdown is None else f"{self.max_drawdown*100:+.1f}%"
        edge = "—" if sc.edge_bps is None else f"{sc.edge_bps:+.1f}bps"
        exec_mode = "maker" if self.cost.maker else "taker"
        return (
            f"{self.agent} [{exec_mode}] over {self.n_bars} bars: "
            f"net ${sc.net_pnl:+.2f} · edge {edge} · trades {sc.n_trades} · "
            f"win {sc.win_rate*100:.0f}% · sharpe {sh} · maxDD {dd}"
        )


# ---------------------------------------------------------------------------
# Clock injection
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def frozen_clock(ts_s: float):
    """Temporarily make ``time.time()`` return ``ts_s`` process-wide.

    Agents do ``import time; time.time()``, which resolves the attribute at call
    time, so replacing ``time.time`` is sufficient. Single-threaded by design.
    """
    saved = time.time
    time.time = lambda: ts_s  # type: ignore[assignment]
    try:
        yield
    finally:
        time.time = saved  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


class Backtester:
    def __init__(
        self,
        cost: CostModel | None = None,
        *,
        conn: sqlite3.Connection | None = None,
        starting_capital: float = 1_000.0,
    ) -> None:
        self.cost = cost or CostModel()
        self.conn = conn or init_db(":memory:")
        self.starting_capital = float(starting_capital)
        self._book: dict[str, _Pos] = {}        # coin -> position
        self._realized = 0.0                     # cumulative realized incl. funding, net of fees

    # -- market view ------------------------------------------------------
    def _view(self, frame: Frame, agent: Agent) -> MarketView:
        live_positions = [
            {
                "coin": coin,
                "szi": pos.signed,
                "entry_px": pos.entry_px,
                "position_value": abs(pos.sz * frame.mids.get(coin, pos.entry_px)),
                "unrealized_pnl": self._unrealized(coin, frame.mids.get(coin, pos.entry_px)),
                "liquidation_px": 0.0,
                "leverage": None,
                "margin_used": 0.0,
            }
            for coin, pos in self._book.items()
        ]
        return MarketView(
            ts_ms=frame.ts_ms,
            mids=dict(frame.mids),
            funding=dict(frame.funding),
            open_interest=dict(frame.open_interest),
            extra={
                "day_ntl_vlm": dict(frame.day_ntl_vlm),
                "candles_1h": dict(frame.candles_1h),
                "closes": {k: list(v) for k, v in frame.closes.items()},
                "spot_mids": dict(frame.spot_mids),
                "liquidations": list(frame.liquidations),
                "live_positions": live_positions,
            },
        )

    def _unrealized(self, coin: str, mid: float) -> float:
        pos = self._book.get(coin)
        if not pos:
            return 0.0
        if pos.side == "B":
            return (mid - pos.entry_px) * pos.sz + pos.funding_accrued
        return (pos.entry_px - mid) * pos.sz + pos.funding_accrued

    # -- funding ----------------------------------------------------------
    def _accrue_funding(self, frame: Frame) -> None:
        for coin, pos in self._book.items():
            rate = frame.funding.get(coin)
            if rate is None:
                continue
            mid = frame.mids.get(coin, pos.entry_px)
            # Long pays when funding > 0; short receives. usdc = -signed*notional*rate.
            pos.funding_accrued += -pos.signed * mid * rate

    # -- execution --------------------------------------------------------
    def _open(self, agent: str, d: Decision, frame: Frame) -> None:
        mid = frame.mids.get(d.coin or "")
        if not mid or not d.sz or d.side not in ("B", "A"):
            return
        fill_px = mid * (1 + self.cost.slip) if d.side == "B" else mid * (1 - self.cost.slip)
        fee = fill_px * d.sz * self.cost.fee_rate
        self._realized -= fee
        existing = self._book.get(d.coin)  # type: ignore[arg-type]
        if existing and existing.side == d.side:
            tot = existing.sz + d.sz
            existing.entry_px = (existing.entry_px * existing.sz + fill_px * d.sz) / tot
            existing.sz = tot
        elif existing and existing.side != d.side:
            # opposite-side order reduces/flips — close then (maybe) open remainder
            self._close(agent, Decision(agent=agent, action="flatten", coin=d.coin,
                                        sz=min(d.sz, existing.sz), px=mid, cloid=d.cloid), frame)
            remainder = d.sz - existing.sz
            if remainder > 1e-12:
                self._book[d.coin] = _Pos(  # type: ignore[index]
                    side=d.side, sz=remainder, entry_px=fill_px, entry_ts_ms=frame.ts_ms,
                )
        else:
            self._book[d.coin] = _Pos(  # type: ignore[index]
                side=d.side, sz=d.sz, entry_px=fill_px, entry_ts_ms=frame.ts_ms,
            )
        self._record_fill(agent, d.coin, d.side, d.sz, fill_px, fee, 0.0, d.cloid)  # type: ignore[arg-type]
        log_decision(self.conn, Decision(
            agent=agent, action="place", coin=d.coin, side=d.side, sz=d.sz,
            px=fill_px, cloid=d.cloid, reasoning=d.reasoning, is_paper=True,
        ))

    def _close(self, agent: str, d: Decision, frame: Frame) -> None:
        coin = d.coin or ""
        pos = self._book.get(coin)
        mid = frame.mids.get(coin)
        if not pos or not mid:
            return
        close_sz = min(d.sz or pos.sz, pos.sz)
        if close_sz <= 0:
            return
        # Closing a long = sell (hit bid); closing a short = buy (lift ask).
        if pos.side == "B":
            exit_px = mid * (1 - self.cost.slip)
            price_pnl = (exit_px - pos.entry_px) * close_sz
            close_side = "A"
        else:
            exit_px = mid * (1 + self.cost.slip)
            price_pnl = (pos.entry_px - exit_px) * close_sz
            close_side = "B"
        frac = close_sz / pos.sz if pos.sz else 1.0
        funding = pos.funding_accrued * frac
        fee = exit_px * close_sz * self.cost.fee_rate
        closed_pnl = price_pnl + funding  # funding folded in; fee tracked separately
        self._realized += closed_pnl - fee
        self._record_fill(agent, coin, close_side, close_sz, exit_px, fee, closed_pnl, d.cloid)
        log_decision(self.conn, Decision(
            agent=agent, action="flatten", coin=coin, side=close_side, sz=close_sz,
            px=exit_px, cloid=d.cloid, reasoning=d.reasoning, is_paper=True,
        ))
        # shrink / remove
        pos.sz -= close_sz
        pos.funding_accrued -= funding
        if pos.sz <= 1e-12:
            self._book.pop(coin, None)

    def _record_fill(
        self, agent: str, coin: str, side: str, sz: float, px: float,
        fee: float, closed_pnl: float, cloid: str | None,
    ) -> None:
        ts = int(time.time() * 1000)
        # Unique-ish synthetic (hash, tid) primary key.
        h = f"bt-{agent}-{coin}-{ts}-{side}"
        tid = abs(hash((h, sz, px, closed_pnl))) % (2**62)
        self.conn.execute(
            """INSERT OR IGNORE INTO fills(
                hash, tid, time_ms, coin, side, px, sz, start_position, dir,
                closed_pnl, fee, fee_token, builder_fee, cloid, agent, raw_json)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (h, tid, ts, coin, side, px, sz, 0, "backtest",
             closed_pnl, fee, "USDC", 0, cloid, agent, "{}"),
        )

    def _apply(self, agent: str, d: Decision, frame: Frame) -> None:
        if d.action == "place":
            self._open(agent, d, frame)
        elif d.action == "flatten":
            self._close(agent, d, frame)
        # 'hold' / 'error' / advisory: nothing to execute.

    # -- run --------------------------------------------------------------
    def run(self, agent: Agent, frames: list[Frame]) -> BacktestResult:
        """Replay ``agent`` over ``frames`` and return a scored result.

        ``agent`` must have been constructed with ``conn=`` this engine's
        connection so its own position-tracking reads the simulated audit log.
        """
        equity_curve: list[tuple[int, float]] = []
        for frame in frames:
            with frozen_clock(frame.ts_ms / 1000.0):
                self._accrue_funding(frame)
                view = self._view(frame, agent)
                decisions = agent.decide(view)
                for d in decisions:
                    self._apply(agent.name, d, frame)
            unreal = sum(
                self._unrealized(coin, frame.mids.get(coin, pos.entry_px))
                for coin, pos in self._book.items()
            )
            equity_curve.append((frame.ts_ms, self.starting_capital + self._realized + unreal))

        scorecard = score_agent(self.conn, agent.name, "all")
        sharpe, dd, calmar = _curve_stats(equity_curve)
        return BacktestResult(
            agent=agent.name,
            scorecard=scorecard,
            equity_curve=equity_curve,
            sharpe=sharpe,
            max_drawdown=dd,
            calmar=calmar,
            n_bars=len(frames),
            starting_capital=self.starting_capital,
            cost=self.cost,
        )


def _curve_stats(
    curve: list[tuple[int, float]],
    periods_per_year: float = 365 * 24,
) -> tuple[float | None, float | None, float | None]:
    """Sharpe / max-drawdown / Calmar from an equity curve.

    ``periods_per_year`` defaults to hourly bars; pass the right cadence for
    other intervals. Returns (None, None, None) when there isn't enough data.
    """
    if len(curve) < 3:
        return None, None, None
    eq = [v for _, v in curve]
    rets = [
        (eq[i] - eq[i - 1]) / eq[i - 1]
        for i in range(1, len(eq))
        if eq[i - 1] != 0
    ]
    sharpe = None
    if rets:
        mean = sum(rets) / len(rets)
        var = sum((r - mean) ** 2 for r in rets) / len(rets)
        std = var ** 0.5
        if std > 0:
            sharpe = mean / std * (periods_per_year ** 0.5)
    peak = eq[0]
    max_dd = 0.0
    for v in eq:
        peak = max(peak, v)
        if peak > 0:
            max_dd = min(max_dd, (v - peak) / peak)
    dd = max_dd if max_dd < 0 else 0.0
    calmar = None
    if dd < 0 and rets:
        ann_ret = (1 + sum(rets) / len(rets)) ** periods_per_year - 1
        calmar = ann_ret / abs(dd)
    return sharpe, dd, calmar
