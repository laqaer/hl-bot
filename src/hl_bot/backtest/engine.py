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
  position). Maker mode defaults to assuming the post-only limit rests and fills
  at mid with zero slippage — an optimistic upper bound for "what if we stopped
  crossing the spread". ``CostModel(maker_fill="resting")`` instead replays the
  live maker lifecycle honestly: entries rest and fill only if price comes to
  them, stale quotes cancel, exits stay taker (a pessimistic lower bound).
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
    funding_hourly: dict[str, float] = field(default_factory=dict)   # raw hourly rate
    day_ntl_vlm: dict[str, float] = field(default_factory=dict)
    open_interest: dict[str, float] = field(default_factory=dict)
    candles_1h: dict[str, dict] = field(default_factory=dict)        # coin -> {vwap, sigma}
    closes: dict[str, list[float]] = field(default_factory=dict)     # coin -> trailing closes
    spot_mids: dict[str, float] = field(default_factory=dict)
    liquidations: list[dict] = field(default_factory=list)
    # Intrabar extremes of THIS bar (B-FILL2): execution-replay data for the
    # resting maker-fill model, never shown to agents (live agents can't see
    # the forming bar's high/low either). Empty on legacy caches — the engine
    # degrades to close-only fill detection.
    highs: dict[str, float] = field(default_factory=dict)
    lows: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class CostModel:
    """Execution cost assumptions.

    Hyperliquid base fees are ~3.5 bps taker / ~1.0 bp maker (lower at volume).
    ``slippage_bps`` is the half-spread + impact crossed on a taker order.

    ``maker_fill`` selects the fill realism of maker mode:

    * ``"optimistic"`` (default, the historical behavior): every order fills
      instantly at mid with maker fees — an upper bound for "what if we stopped
      crossing the spread".
    * ``"resting"``: a faithful replica of the live ``--execution maker``
      proposal (entries maker, exits taker — see ``exec/maker.py``). Entries
      rest as post-only limits at the decision bar's mid and fill only when a
      LATER bar trades strictly through the limit (queue-conservative);
      quotes unfilled after ``maker_ttl_s`` are cancelled, like the live
      stale-quote sweep. Exits pay full taker fee + slippage. Fill detection
      uses the bar's intrabar low/high when frames carry them (B-FILL2) and
      falls back to the close mid on legacy data — close-only detection
      misses wick touches, which skew toward winners, so the fallback is
      extra-pessimistic.
    * ``"resting-close"``: the resting lifecycle with fill detection forced
      to close mids only, even when intrabar extremes are available — the
      pre-B-FILL2 lower bound, kept so the wick-detection tightening can be
      A/B'd on identical data.
    """

    taker_fee_bps: float = 4.5
    maker_fee_bps: float = 1.0
    slippage_bps: float = 2.0
    maker: bool = False
    maker_fill: str = "optimistic"
    maker_ttl_s: int = 1800   # live exec.maker.DEFAULT_MAX_REST_S

    def __post_init__(self) -> None:
        if self.maker_fill not in ("optimistic", "resting", "resting-close"):
            raise ValueError(
                f"maker_fill must be 'optimistic', 'resting' or 'resting-close', "
                f"got {self.maker_fill!r}"
            )

    @property
    def resting(self) -> bool:
        return self.maker and self.maker_fill in ("resting", "resting-close")

    @property
    def wick_fills(self) -> bool:
        return self.maker_fill == "resting"

    @property
    def exec_label(self) -> str:
        if not self.maker:
            return "taker"
        if not self.resting:
            return "maker"
        return "maker-rest" if self.wick_fills else "maker-restc"

    @property
    def fee_bps(self) -> float:
        return self.maker_fee_bps if self.maker else self.taker_fee_bps

    @property
    def slip(self) -> float:
        return 0.0 if self.maker else self.slippage_bps / 10_000.0

    @property
    def fee_rate(self) -> float:
        return self.fee_bps / 10_000.0

    # Exits: identical to entries except in resting-maker mode, where the live
    # design keeps exits taker (urgency beats fee savings on stops/reversions).
    @property
    def exit_fee_rate(self) -> float:
        return self.taker_fee_bps / 10_000.0 if self.resting else self.fee_rate

    @property
    def exit_slip(self) -> float:
        return self.slippage_bps / 10_000.0 if self.resting else self.slip


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


@dataclass
class _Resting:
    """A post-only entry quote waiting for the market to come to it."""

    side: str
    sz: float
    px: float            # limit price (decision bar's mid — offline touch proxy)
    placed_ts_ms: int
    cloid: str | None
    reasoning: str | None


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
    maker_fill_stats: dict[str, int] | None = None   # resting mode: rested/filled/expired

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
        out = (
            f"{self.agent} [{self.cost.exec_label}] over {self.n_bars} bars: "
            f"net ${sc.net_pnl:+.2f} · edge {edge} · trades {sc.n_trades} · "
            f"win {sc.win_rate*100:.0f}% · sharpe {sh} · maxDD {dd}"
        )
        st = self.maker_fill_stats
        if st and st.get("rested"):
            out += (
                f" · quotes {st['rested']} filled {st['filled']}"
                f" ({st['filled']/st['rested']*100:.0f}%) expired {st['expired']}"
            )
            if st.get("filled_wick"):
                out += f" · wick-fills {st['filled_wick']}"
        return out


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
        self._resting: dict[str, _Resting] = {}  # coin -> working maker quote
        # filled_wick ⊆ filled: fills only the intrabar low/high detected
        # (the close mid never crossed) — what B-FILL2 adds over close-only.
        self.maker_fill_stats = {"rested": 0, "filled": 0, "expired": 0, "filled_wick": 0}

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
        # Live MarketView.funding is the HOURLY rate (activeAssetCtx semantics);
        # frame.funding is per-bar-scaled for accrual, so agents must see the
        # hourly series or any rate threshold means 60× less at 1m than live.
        # Legacy frames (pre-funding_hourly caches) fall back to per-bar.
        return MarketView(
            ts_ms=frame.ts_ms,
            mids=dict(frame.mids),
            funding=dict(frame.funding_hourly or frame.funding),
            open_interest=dict(frame.open_interest),
            extra={
                "day_ntl_vlm": dict(frame.day_ntl_vlm),
                "candles_1h": dict(frame.candles_1h),
                "closes": {k: list(v) for k, v in frame.closes.items()},
                "spot_mids": dict(frame.spot_mids),
                "liquidations": list(frame.liquidations),
                "liquidations_feed": True,
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
        coin = d.coin or ""
        mid = frame.mids.get(coin)
        if not mid or not d.sz or d.side not in ("B", "A"):
            return
        if self.cost.resting:
            # Live `has_resting_order`: one working quote per coin; a re-signal
            # while a quote rests is dropped, not re-priced.
            if coin not in self._resting:
                self._resting[coin] = _Resting(
                    side=d.side, sz=d.sz, px=mid, placed_ts_ms=frame.ts_ms,
                    cloid=d.cloid, reasoning=d.reasoning,
                )
                self.maker_fill_stats["rested"] += 1
            return
        fill_px = mid * (1 + self.cost.slip) if d.side == "B" else mid * (1 - self.cost.slip)
        self._fill_open(agent, d, frame, fill_px)

    def _process_resting(self, agent: str, frame: Frame) -> None:
        """Fill or expire working maker quotes against this frame's bar.

        Stale check first: live cancels at the first tick past the TTL, so a
        cross on the same bar the quote went stale does not fill (pessimistic
        by at most one bar). Fill requires price to trade strictly through
        the limit — equality means an unknowable queue position. Detection
        prefers the bar's intrabar extreme (low for buys / high for sells,
        B-FILL2) over the close mid; frames without highs/lows (legacy
        caches) and maker_fill="resting-close" judge on the close mid only,
        which misses wick touches.
        """
        for coin, o in list(self._resting.items()):
            if frame.ts_ms - o.placed_ts_ms > self.cost.maker_ttl_s * 1000:
                del self._resting[coin]
                self.maker_fill_stats["expired"] += 1
                continue
            mid = frame.mids.get(coin)
            if not mid:
                continue
            crossed = mid < o.px if o.side == "B" else mid > o.px
            if not crossed and self.cost.wick_fills:
                ext = frame.lows.get(coin) if o.side == "B" else frame.highs.get(coin)
                if ext:
                    crossed = ext < o.px if o.side == "B" else ext > o.px
                    if crossed:
                        self.maker_fill_stats["filled_wick"] += 1
            if crossed:
                del self._resting[coin]
                self.maker_fill_stats["filled"] += 1
                self._fill_open(agent, Decision(
                    agent=agent, action="place", coin=coin, side=o.side, sz=o.sz,
                    px=o.px, cloid=o.cloid, reasoning=o.reasoning,
                ), frame, o.px)

    def _fill_open(self, agent: str, d: Decision, frame: Frame, fill_px: float) -> None:
        coin = d.coin or ""
        existing = self._book.get(coin)
        if existing and existing.side != d.side:
            # Opposite-side order: close up to the existing size first, then open
            # only the leftover in the new direction. Capture the size BEFORE the
            # close mutates the position (else remainder is wrong), and record a
            # fill for the OPENED size only — the close leg books its own fill —
            # so a reduce/flip never double-counts fees/notional.
            existing_sz = existing.sz
            self._close(agent, Decision(
                agent=agent, action="flatten", coin=coin,
                sz=min(d.sz or 0.0, existing_sz), px=frame.mids.get(coin), cloid=d.cloid), frame)
            open_sz = (d.sz or 0.0) - existing_sz
            if open_sz <= 1e-12:
                return  # pure reduce / exact flat — the close already booked it
        else:
            open_sz = d.sz or 0.0

        fee = fill_px * open_sz * self.cost.fee_rate
        self._realized -= fee
        existing = self._book.get(coin)  # may have been removed by the close above
        if existing and existing.side == d.side:
            tot = existing.sz + open_sz
            existing.entry_px = (existing.entry_px * existing.sz + fill_px * open_sz) / tot
            existing.sz = tot
        else:
            self._book[coin] = _Pos(
                side=d.side, sz=open_sz, entry_px=fill_px, entry_ts_ms=frame.ts_ms,
            )
        self._record_fill(agent, coin, d.side, open_sz, fill_px, fee, 0.0, d.cloid)
        log_decision(self.conn, Decision(
            agent=agent, action="place", coin=coin, side=d.side, sz=open_sz,
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
        # Exit costs may differ from entry (resting-maker mode keeps exits taker).
        if pos.side == "B":
            exit_px = mid * (1 - self.cost.exit_slip)
            price_pnl = (exit_px - pos.entry_px) * close_sz
            close_side = "A"
        else:
            exit_px = mid * (1 + self.cost.exit_slip)
            price_pnl = (pos.entry_px - exit_px) * close_sz
            close_side = "B"
        frac = close_sz / pos.sz if pos.sz else 1.0
        funding = pos.funding_accrued * frac
        fee = exit_px * close_sz * self.cost.exit_fee_rate
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
    def run(
        self, agent: Agent, frames: list[Frame], *, liquidate_at_end: bool = True
    ) -> BacktestResult:
        """Replay ``agent`` over ``frames`` and return a scored result.

        ``agent`` must have been constructed with ``conn=`` this engine's
        connection so its own position-tracking reads the simulated audit log.

        With ``liquidate_at_end`` (default), any position still open after the
        last frame is force-closed at that frame's mid, so accrued funding /
        unrealized PnL of held positions lands in the *realized* scorecard. This
        matters for carry strategies that hold to collect funding and would
        otherwise show ~zero realized PnL.
        """
        equity_curve: list[tuple[int, float]] = []
        for frame in frames:
            with frozen_clock(frame.ts_ms / 1000.0):
                self._accrue_funding(frame)
                # Working maker quotes fill/expire BEFORE decide so the agent
                # sees the new position this bar (live: WS userFills fold in
                # before gather_decisions). A position filled this bar starts
                # accruing funding next bar, same as a taker entry.
                self._process_resting(agent.name, frame)
                view = self._view(frame, agent)
                decisions = agent.decide(view)
                for d in decisions:
                    self._apply(agent.name, d, frame)
            unreal = sum(
                self._unrealized(coin, frame.mids.get(coin, pos.entry_px))
                for coin, pos in self._book.items()
            )
            equity_curve.append((frame.ts_ms, self.starting_capital + self._realized + unreal))

        if self._resting:
            # End-of-run quotes never filled: cancelled, counted as expired.
            self.maker_fill_stats["expired"] += len(self._resting)
            self._resting.clear()

        if liquidate_at_end and self._book and frames:
            last = frames[-1]
            with frozen_clock(last.ts_ms / 1000.0):
                for coin in list(self._book):
                    if last.mids.get(coin):
                        self._close(agent.name, Decision(
                            agent=agent.name, action="flatten", coin=coin,
                            sz=self._book[coin].sz, px=last.mids[coin],
                            reasoning="LIQUIDATE-AT-END",
                        ), last)

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
            maker_fill_stats=dict(self.maker_fill_stats) if self.cost.resting else None,
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
