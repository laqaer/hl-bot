"""Maker-spread capture — an *execution* edge, not a *direction* edge.

Thesis (the ninth, structurally-different class). Eight direction/relative theses
(TWAP-MR, funding carry, cross-sectional momentum ±regime, time-series momentum,
majors-1d momentum, pairs reversion, session-timing) are pruned or reduced to an
over-conditioned point, all sharing one failure mode: a price/funding/clock-derived
*directional* signal that proves regime-sensitive under the walk-forward durability
bar. REVIEW C1 says the structural money is in **execution, not direction** — the
taker tax is ~73% of the historical bleed (B1). B1 measured that *maker execution of
a directional signal* doesn't create edge, but it never tested capturing the
**spread/rebate itself** as the edge.

This module models that capture honestly. A passive maker rests a two-sided quote
just inside/at the touch; when filled it earns the realized half-spread (plus any
maker rebate) but is adversely selected — a resting bid fills precisely when the
market trades *down* into it, i.e. when being long is about to be wrong. The net
edge is therefore::

    net = captured_half_spread + rebate − adverse_selection − maker_fee

which is **not a directional bet**, so it may sidestep the regime-sensitivity that
killed the eight directional theses — or it may not, if adverse selection eats the
spread. That is the empirical question, and this is the backtest-able model for it.

Why a dedicated model (not the `decide()`/engine path). The replay engine fills
every ``place``/``flatten`` deterministically at the bar mid (maker mode = mid, zero
slippage). That can represent a *directional* maker bet but **cannot represent
spread capture or adverse selection**: it never fills *at* a resting limit price and
never conditions the fill on the price trading into the quote. So the honest model
is a pure, no-lookahead intrabar fill simulator over real OHLC candles, decomposing
each fill into captured-spread vs adverse-drift. Pure and unit-testable; the real
edge number comes from running it over real Hyperliquid candle history.
"""

from __future__ import annotations

from dataclasses import dataclass

# Hyperliquid base maker fee ~1.0 bp (lower at volume); rebate is 0 for the base
# tier (HL pays maker rebates only to high-volume market-maker tiers). Defaults
# are deliberately conservative — no assumed rebate.
DEFAULT_MAKER_FEE_BPS = 1.0
DEFAULT_MAKER_REBATE_BPS = 0.0


@dataclass(frozen=True)
class MakerBar:
    """One OHLC bar. ``mid`` is the close (the mark); ``high``/``low`` decide fills."""

    mid: float
    high: float
    low: float


def bars_from_candles(candles: list[dict]) -> list[MakerBar]:
    """Adapt raw Hyperliquid candle dicts ({o,h,l,c,...}) into ``MakerBar``s.

    Newest-last, skipping any malformed/non-positive row. Pure (no network)."""
    out: list[MakerBar] = []
    for k in sorted(candles, key=lambda r: int(r.get("t", 0))):
        try:
            c = float(k.get("c", 0))
            h = float(k.get("h", 0))
            low = float(k.get("l", 0))
        except (TypeError, ValueError):
            continue
        if c > 0 and h > 0 and low > 0 and h >= low:
            out.append(MakerBar(mid=c, high=h, low=low))
    return out


@dataclass(frozen=True)
class MakerSpreadResult:
    """Outcome of resting a passive two-sided maker quote over a bar series.

    All bps figures are per-fill averages relative to the quote's reference mid,
    except ``net_per_quote_bps`` (per quoted bar, so it folds in the fill rate).
    The decomposition holds by construction:
        ``net_edge_bps = gross_spread_bps − adverse_bps − fee_bps + rebate_bps``.
    """

    n_quotes: int            # bars where a quote rested (have a prior mid anchor)
    n_bid_fills: int
    n_ask_fills: int
    n_both_fills: int        # bars where BOTH sides filled (full round-trip, flat)
    fill_rate: float         # fills / (2 * n_quotes) — per-side fill probability
    gross_spread_bps: float  # avg captured half-spread vs mid, per fill
    adverse_bps: float       # avg adverse drift (mid moved past the fill), per fill
    fee_bps: float
    rebate_bps: float
    net_edge_bps: float      # per-fill net of cost
    net_per_quote_bps: float # per quoted bar (net_edge * fill throughput)
    n_fills: int

    @property
    def profitable(self) -> bool:
        return self.net_edge_bps > 0.0

    def summary(self) -> str:
        return (
            f"maker_spread: {self.n_fills} fills over {self.n_quotes} quotes "
            f"(fill_rate {self.fill_rate*100:.1f}%) · "
            f"gross {self.gross_spread_bps:+.2f} − adverse {self.adverse_bps:.2f} "
            f"− fee {self.fee_bps:.2f} + rebate {self.rebate_bps:.2f} = "
            f"net {self.net_edge_bps:+.2f}bps/fill · "
            f"{self.net_per_quote_bps:+.3f}bps/quote"
        )


class _Accum:
    """Mutable running totals; pooled across coins to build one ``MakerSpreadResult``."""

    def __init__(self, fee_bps: float, rebate_bps: float) -> None:
        self.fee_bps = fee_bps
        self.rebate_bps = rebate_bps
        self.n_quotes = 0
        self.n_bid = 0
        self.n_ask = 0
        self.n_both = 0
        self.gross_sum = 0.0
        self.adverse_sum = 0.0

    def add_fill(self, gross_bps: float, adverse_bps: float) -> None:
        self.gross_sum += gross_bps
        self.adverse_sum += adverse_bps

    def result(self) -> MakerSpreadResult:
        n_fills = self.n_bid + self.n_ask
        if n_fills == 0 or self.n_quotes == 0:
            return MakerSpreadResult(
                n_quotes=self.n_quotes, n_bid_fills=self.n_bid, n_ask_fills=self.n_ask,
                n_both_fills=self.n_both, fill_rate=0.0, gross_spread_bps=0.0,
                adverse_bps=0.0, fee_bps=self.fee_bps, rebate_bps=self.rebate_bps,
                net_edge_bps=0.0, net_per_quote_bps=0.0, n_fills=0,
            )
        gross = self.gross_sum / n_fills
        adverse = self.adverse_sum / n_fills
        net_per_fill = gross - adverse - self.fee_bps + self.rebate_bps
        fill_rate = n_fills / (2.0 * self.n_quotes)
        # net per quoted bar: each bar quotes both sides, so expected fills/bar =
        # 2*fill_rate; net_per_quote = net_per_fill * (n_fills / n_quotes).
        net_per_quote = net_per_fill * (n_fills / self.n_quotes)
        return MakerSpreadResult(
            n_quotes=self.n_quotes, n_bid_fills=self.n_bid, n_ask_fills=self.n_ask,
            n_both_fills=self.n_both, fill_rate=fill_rate,
            gross_spread_bps=gross, adverse_bps=adverse,
            fee_bps=self.fee_bps, rebate_bps=self.rebate_bps,
            net_edge_bps=net_per_fill, net_per_quote_bps=net_per_quote, n_fills=n_fills,
        )


def _simulate_into(
    acc: _Accum,
    bars: list[MakerBar],
    *,
    half_spread_bps: float,
) -> None:
    """Run the no-lookahead fill sim for one coin's bars into a shared accumulator.

    At bar ``i`` (``i>=1``) a bid rests at ``m0*(1-hs)`` and an ask at ``m0*(1+hs)``
    where ``m0`` = bar ``i-1``'s mid (known at the start of the bar — no lookahead).
    The bid fills iff this bar's low touches it; the ask fills iff this bar's high
    touches it. A filled lot is marked to this bar's close ``mi``:

      * bid fill (long from ``bid_px`` → ``mi``): pnl = (mi − bid_px); split into
        captured spread (m0 − bid_px ≈ hs) and adverse drift −(mi − m0).
      * ask fill (short from ``ask_px`` → ``mi``): pnl = (ask_px − mi); split into
        captured spread (ask_px − m0 ≈ hs) and adverse drift (mi − m0).

    Adverse selection thus emerges from the realized path, not an assumption.
    """
    hs = half_spread_bps / 10_000.0
    for i in range(1, len(bars)):
        m0 = bars[i - 1].mid
        bar = bars[i]
        if m0 <= 0:
            continue
        acc.n_quotes += 1
        bid_px = m0 * (1.0 - hs)
        ask_px = m0 * (1.0 + hs)
        filled_bid = bar.low <= bid_px
        filled_ask = bar.high >= ask_px
        mi = bar.mid
        if filled_bid:
            acc.n_bid += 1
            gross = (m0 - bid_px) / m0 * 10_000.0
            adverse = -(mi - m0) / m0 * 10_000.0   # price fell after buying -> +adverse
            acc.add_fill(gross, adverse)
        if filled_ask:
            acc.n_ask += 1
            gross = (ask_px - m0) / m0 * 10_000.0
            adverse = (mi - m0) / m0 * 10_000.0     # price rose after selling -> +adverse
            acc.add_fill(gross, adverse)
        if filled_bid and filled_ask:
            acc.n_both += 1


def simulate_maker_spread(
    bars: list[MakerBar],
    *,
    half_spread_bps: float,
    maker_fee_bps: float = DEFAULT_MAKER_FEE_BPS,
    maker_rebate_bps: float = DEFAULT_MAKER_REBATE_BPS,
) -> MakerSpreadResult:
    """Single-coin passive two-sided maker-quote backtest. See ``_simulate_into``."""
    acc = _Accum(maker_fee_bps, maker_rebate_bps)
    _simulate_into(acc, bars, half_spread_bps=half_spread_bps)
    return acc.result()


def simulate_universe(
    bars_by_coin: dict[str, list[MakerBar]],
    *,
    half_spread_bps: float,
    maker_fee_bps: float = DEFAULT_MAKER_FEE_BPS,
    maker_rebate_bps: float = DEFAULT_MAKER_REBATE_BPS,
) -> MakerSpreadResult:
    """Pool fills across a coin universe into one edge estimate (equal-notional).

    Each fill contributes equally (per-fill bps are notional-normalized), so this
    is the per-fill edge a maker quoting the whole universe would realize."""
    acc = _Accum(maker_fee_bps, maker_rebate_bps)
    for bars in bars_by_coin.values():
        _simulate_into(acc, bars, half_spread_bps=half_spread_bps)
    return acc.result()


# ---------------------------------------------------------------------------
# Inventory-skew / round-trip variant (B-exec slice 2)
#
# Slice 1 found the naive *symmetric* two-sided quote is net-negative & sign-stable:
# adverse selection (a bid fills precisely on down-bars) runs ~1.5–2bps above the
# captured half-spread, and the whole bleed is carried by *single-sided* fills that
# inherit inventory into the adverse move. The one positive structure was the
# adverse-free both-sides-fill bar (you end the bar flat, earning ~2×spread − 2×fee).
#
# This variant tests whether disciplined round-tripping rescues the thesis. It holds
# at most one lot and **skews fully against inventory**: while flat it quotes both
# sides, but the moment one side fills it cancels the same side and quotes ONLY the
# reducing (exit) side, resting at a half-spread the other way, until that exit
# fills. PnL is realized only on a completed round-trip, decomposed into captured
# spread (≈2 half-spreads) minus the mid drift over the hold (adverse) minus 2 maker
# fees. No lookahead: every quote is anchored to the prior bar's mid.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MakerInventoryResult:
    """Outcome of the inventory-skew (≤1 lot, round-trip-only) maker.

    All bps figures are per *completed round-trip* (two maker fills), except
    ``net_per_quote_bps`` (per quoted bar, folding in round-trip throughput). The
    decomposition holds by construction:
        ``net_edge_bps = gross_spread_bps − adverse_bps − fee_bps + rebate_bps``
    where ``fee_bps``/``rebate_bps`` are the *round-trip* totals (2× per-fill).
    """

    n_quotes: int             # bars with a prior-mid anchor (quote opportunities)
    n_round_trips: int        # completed entry+exit round-trips (realized PnL)
    n_inbar_round_trips: int  # both sides filled same bar -> flat, adverse-free
    n_carried_round_trips: int  # entry then exit on a LATER bar (eats hold drift)
    avg_hold_bars: float      # mean bars inventory was held (0 for in-bar)
    unclosed_inventory: int   # lots still open at series end (PnL not booked)
    gross_spread_bps: float   # avg captured spread per round-trip (~2×half_spread)
    adverse_bps: float        # avg mid drift against the lot over the hold
    fee_bps: float            # round-trip maker fee (2× per-fill)
    rebate_bps: float         # round-trip maker rebate (2× per-fill)
    net_edge_bps: float       # per round-trip net of cost
    net_per_quote_bps: float  # per quoted bar (folds in round-trip throughput)

    @property
    def profitable(self) -> bool:
        return self.net_edge_bps > 0.0

    def summary(self) -> str:
        return (
            f"maker_inventory: {self.n_round_trips} round-trips "
            f"({self.n_inbar_round_trips} in-bar / {self.n_carried_round_trips} carried, "
            f"avg_hold {self.avg_hold_bars:.1f} bars, {self.unclosed_inventory} unclosed) "
            f"over {self.n_quotes} quotes · "
            f"gross {self.gross_spread_bps:+.2f} − adverse {self.adverse_bps:.2f} "
            f"− fee {self.fee_bps:.2f} + rebate {self.rebate_bps:.2f} = "
            f"net {self.net_edge_bps:+.2f}bps/round-trip · "
            f"{self.net_per_quote_bps:+.3f}bps/quote"
        )


class _InvAccum:
    """Running totals for the inventory-skew maker; pooled across coins."""

    def __init__(self, fee_bps: float, rebate_bps: float) -> None:
        self.fee_bps = fee_bps        # per fill
        self.rebate_bps = rebate_bps  # per fill
        self.n_quotes = 0
        self.n_rt = 0
        self.n_inbar = 0
        self.n_carried = 0
        self.hold_sum = 0
        self.gross_sum = 0.0
        self.adverse_sum = 0.0
        self.unclosed = 0

    def add_round_trip(
        self, gross_bps: float, adverse_bps: float, hold_bars: int, *, inbar: bool
    ) -> None:
        self.n_rt += 1
        self.gross_sum += gross_bps
        self.adverse_sum += adverse_bps
        self.hold_sum += hold_bars
        if inbar:
            self.n_inbar += 1
        else:
            self.n_carried += 1

    def result(self) -> MakerInventoryResult:
        fee_rt = 2.0 * self.fee_bps
        rebate_rt = 2.0 * self.rebate_bps
        if self.n_rt == 0 or self.n_quotes == 0:
            return MakerInventoryResult(
                n_quotes=self.n_quotes, n_round_trips=0, n_inbar_round_trips=0,
                n_carried_round_trips=0, avg_hold_bars=0.0,
                unclosed_inventory=self.unclosed, gross_spread_bps=0.0,
                adverse_bps=0.0, fee_bps=fee_rt, rebate_bps=rebate_rt,
                net_edge_bps=0.0, net_per_quote_bps=0.0,
            )
        gross = self.gross_sum / self.n_rt
        adverse = self.adverse_sum / self.n_rt
        net = gross - adverse - fee_rt + rebate_rt
        net_per_quote = net * (self.n_rt / self.n_quotes)
        return MakerInventoryResult(
            n_quotes=self.n_quotes, n_round_trips=self.n_rt,
            n_inbar_round_trips=self.n_inbar, n_carried_round_trips=self.n_carried,
            avg_hold_bars=self.hold_sum / self.n_rt,
            unclosed_inventory=self.unclosed, gross_spread_bps=gross,
            adverse_bps=adverse, fee_bps=fee_rt, rebate_bps=rebate_rt,
            net_edge_bps=net, net_per_quote_bps=net_per_quote,
        )


def _simulate_inventory_into(
    acc: _InvAccum,
    bars: list[MakerBar],
    *,
    half_spread_bps: float,
) -> None:
    """No-lookahead inventory-skew fill sim for one coin into a shared accumulator.

    Holds at most one lot. While **flat** it quotes both sides at ``m0*(1∓hs)``
    (``m0`` = prior bar's mid). A bar that fills both sides is an in-bar round-trip:
    you end flat earning the full ``2×hs`` with zero adverse. A bar that fills one
    side leaves a lot; from the next bar the maker quotes **only the exit side**
    (skew fully against inventory) at ``m0*(1±hs)`` until it fills, then books the
    round-trip and resumes two-sided quoting. Realized PnL decomposes into captured
    spread (≈2 half-spreads) minus the mid drift over the hold (adverse) — adverse
    emerges from the realized path, not an assumption.
    """
    hs = half_spread_bps / 10_000.0
    inventory = 0       # +1 long lot, -1 short lot, 0 flat
    entry_px = 0.0
    entry_anchor = 0.0  # the mid the entry quote was anchored to (e0)
    entry_idx = 0
    for i in range(1, len(bars)):
        m0 = bars[i - 1].mid
        bar = bars[i]
        if m0 <= 0:
            continue
        acc.n_quotes += 1
        bid_px = m0 * (1.0 - hs)
        ask_px = m0 * (1.0 + hs)
        if inventory == 0:
            filled_bid = bar.low <= bid_px
            filled_ask = bar.high >= ask_px
            if filled_bid and filled_ask:
                # In-bar round-trip: bought bid, sold ask, end flat. gross = 2*hs.
                gross = (ask_px - bid_px) / m0 * 10_000.0
                acc.add_round_trip(gross, 0.0, 0, inbar=True)
            elif filled_bid:
                inventory = 1
                entry_px = bid_px
                entry_anchor = m0
                entry_idx = i
            elif filled_ask:
                inventory = -1
                entry_px = ask_px
                entry_anchor = m0
                entry_idx = i
        elif inventory == 1:
            # Hold long; quote only the exit ask. Fills iff the bar trades up to it.
            if bar.high >= ask_px:
                e0 = entry_anchor
                pnl = (ask_px - entry_px) / e0 * 10_000.0
                adverse = -(m0 - e0) / e0 * 10_000.0  # price fell while long -> +adverse
                acc.add_round_trip(pnl + adverse, adverse, i - entry_idx, inbar=False)
                inventory = 0
        else:  # inventory == -1
            # Hold short; quote only the exit bid. Fills iff the bar trades down to it.
            if bar.low <= bid_px:
                e0 = entry_anchor
                pnl = (entry_px - bid_px) / e0 * 10_000.0
                adverse = (m0 - e0) / e0 * 10_000.0  # price rose while short -> +adverse
                acc.add_round_trip(pnl + adverse, adverse, i - entry_idx, inbar=False)
                inventory = 0
    if inventory != 0:
        acc.unclosed += 1


def simulate_maker_inventory(
    bars: list[MakerBar],
    *,
    half_spread_bps: float,
    maker_fee_bps: float = DEFAULT_MAKER_FEE_BPS,
    maker_rebate_bps: float = DEFAULT_MAKER_REBATE_BPS,
) -> MakerInventoryResult:
    """Single-coin inventory-skew (≤1 lot, round-trip-only) maker. See ``_simulate_inventory_into``."""
    acc = _InvAccum(maker_fee_bps, maker_rebate_bps)
    _simulate_inventory_into(acc, bars, half_spread_bps=half_spread_bps)
    return acc.result()


def simulate_universe_inventory(
    bars_by_coin: dict[str, list[MakerBar]],
    *,
    half_spread_bps: float,
    maker_fee_bps: float = DEFAULT_MAKER_FEE_BPS,
    maker_rebate_bps: float = DEFAULT_MAKER_REBATE_BPS,
) -> MakerInventoryResult:
    """Pool inventory-skew round-trips across a coin universe (equal-notional, ≤1 lot/coin)."""
    acc = _InvAccum(maker_fee_bps, maker_rebate_bps)
    for bars in bars_by_coin.values():
        _simulate_inventory_into(acc, bars, half_spread_bps=half_spread_bps)
    return acc.result()
