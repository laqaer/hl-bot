"""Perp-vs-spot basis reversion — the tenth, structurally-different thesis.

Thesis. Nine theses are pruned: eight *directional* (TWAP-MR, funding carry,
cross-sectional momentum ±regime, time-series momentum, majors-1d momentum, pairs
reversion, session-timing) and one *execution* (passive maker spread capture). The
one named candidate class never run (flagged in Iter 33/37) is **basis /
term-structure**: the price gap between a coin's perpetual and its *spot* market.

Hyperliquid lists spot markets for the wrapped majors (UBTC ``@142``, UETH ``@151``,
USOL ``@156``) plus the native HYPE (``@107``), so a perp-vs-spot basis is directly
measurable on the same venue. Funding ties the perp to spot, but only at the funding
cadence; between funding stamps the perp can trade rich or cheap to spot, and
cash-and-carry pressure pulls it back. The signal is therefore the **rolling
z-score of the basis** ``b = perp/spot − 1``:

  * basis rich (z ≥ +entry)  → the perp is expensive vs spot → **SHORT the perp**
  * basis cheap (z ≤ −entry) → the perp is cheap vs spot     → **LONG the perp**
  * exit when the basis reverts inside ``|z| ≤ exit``.

This is **perp-only and directional** (it does not hold the spot leg, so it is not a
delta-neutral cash-and-carry arb — the existing engine trades perps only). It is
orthogonal to all nine pruned theses: it keys off the *cross-market price gap of the
same asset*, not a coin's own return (momentum), funding *level* (carry), a *pairwise*
ratio of two coins (pairs), the *clock* (session), or *execution* microstructure.
REVIEW M5's prior is that majors basis is "tiny and well-arbitraged" — that is exactly
the kind of prior the harness exists to test with a real number rather than assume.

Why a dedicated model (not the `decide()`/engine path). The replay engine's `Frame`
carries only the perp series; it has no spot reference, so it cannot represent a
basis signal at all without invasive plumbing for an unproven thesis. This is a pure,
no-lookahead model over aligned perp+spot candle history (mirroring `maker_spread.py`)
that decomposes every round-trip into the captured perp move minus maker fees. Pure
and unit-testable; the real edge number comes from running it over real HL history.
"""

from __future__ import annotations

from dataclasses import dataclass

# Hyperliquid base maker fee ~1.0 bp (lower at volume). Conservative default.
DEFAULT_MAKER_FEE_BPS = 1.0

# Perp symbol -> spot candle coin id on Hyperliquid (wrapped majors + native HYPE).
# These are the only liquid perp/spot overlaps on HL; majors spot trades as the
# "U"-prefixed wrapped token (UBTC/UETH/USOL) vs USDC. Verified against spotMeta.
SPOT_MARKETS: dict[str, str] = {
    "BTC": "@142",   # UBTC/USDC
    "ETH": "@151",   # UETH/USDC
    "SOL": "@156",   # USOL/USDC
    "HYPE": "@107",  # HYPE/USDC
}


@dataclass(frozen=True)
class BasisBar:
    """One aligned bar: the perp close and the spot close at the same timestamp."""

    perp: float
    spot: float

    @property
    def basis(self) -> float:
        """Perp premium over spot as a fraction: ``perp/spot − 1`` (rich > 0)."""
        return self.perp / self.spot - 1.0 if self.spot > 0 else 0.0


def bars_from_candles(
    perp_candles: list[dict], spot_candles: list[dict]
) -> list[BasisBar]:
    """Align raw HL perp + spot candle dicts by open time ``t`` into ``BasisBar``s.

    Newest-last, keeping only timestamps present in *both* series with positive
    closes. Pure (no network)."""
    spot_by_t: dict[int, float] = {}
    for k in spot_candles:
        try:
            t = int(k.get("t", 0))
            c = float(k.get("c", 0))
        except (TypeError, ValueError):
            continue
        if c > 0:
            spot_by_t[t] = c
    out: list[BasisBar] = []
    for k in sorted(perp_candles, key=lambda r: int(r.get("t", 0))):
        try:
            t = int(k.get("t", 0))
            p = float(k.get("c", 0))
        except (TypeError, ValueError):
            continue
        s = spot_by_t.get(t)
        if p > 0 and s is not None and s > 0:
            out.append(BasisBar(perp=p, spot=s))
    return out


@dataclass(frozen=True)
class BasisReversionResult:
    """Outcome of trading the perp on its rolling basis-z reversion signal.

    All bps figures are per *completed round-trip* (entry+exit), except
    ``net_per_bar_bps`` (per quoted bar, folding in trade throughput). The
    decomposition holds by construction:
        ``net_edge_bps = gross_bps − fee_bps``
    where ``fee_bps`` is the round-trip maker fee (2× per-side).
    """

    n_bars: int            # bars eligible to trade (past warmup)
    n_trades: int          # completed round-trips (realized PnL)
    n_long: int            # trades that were LONG the perp (basis cheap)
    n_short: int           # trades that were SHORT the perp (basis rich)
    avg_hold_bars: float
    unclosed: int          # positions still open at series end (not booked)
    win_rate: float        # fraction of round-trips with positive gross move
    gross_bps: float       # avg captured perp move in the trade direction
    fee_bps: float         # round-trip maker fee (2× per-side)
    net_edge_bps: float    # per round-trip net of cost
    net_per_bar_bps: float # per eligible bar (folds in trade throughput)

    @property
    def profitable(self) -> bool:
        return self.net_edge_bps > 0.0

    def summary(self) -> str:
        return (
            f"basis_reversion: {self.n_trades} round-trips "
            f"({self.n_long} long / {self.n_short} short, avg_hold "
            f"{self.avg_hold_bars:.1f} bars, win {self.win_rate*100:.0f}%, "
            f"{self.unclosed} unclosed) over {self.n_bars} bars · "
            f"gross {self.gross_bps:+.2f} − fee {self.fee_bps:.2f} = "
            f"net {self.net_edge_bps:+.2f}bps/round-trip · "
            f"{self.net_per_bar_bps:+.3f}bps/bar"
        )


class _Accum:
    """Mutable running totals; pooled across coins into one ``BasisReversionResult``."""

    def __init__(self, fee_bps: float) -> None:
        self.fee_bps = fee_bps        # per side
        self.n_bars = 0
        self.n_trades = 0
        self.n_long = 0
        self.n_short = 0
        self.n_win = 0
        self.hold_sum = 0
        self.gross_sum = 0.0
        self.unclosed = 0

    def add_trade(self, gross_bps: float, hold_bars: int, *, is_long: bool) -> None:
        self.n_trades += 1
        self.gross_sum += gross_bps
        self.hold_sum += hold_bars
        if gross_bps > 0:
            self.n_win += 1
        if is_long:
            self.n_long += 1
        else:
            self.n_short += 1

    def result(self) -> BasisReversionResult:
        fee_rt = 2.0 * self.fee_bps
        if self.n_trades == 0 or self.n_bars == 0:
            return BasisReversionResult(
                n_bars=self.n_bars, n_trades=0, n_long=0, n_short=0,
                avg_hold_bars=0.0, unclosed=self.unclosed, win_rate=0.0,
                gross_bps=0.0, fee_bps=fee_rt, net_edge_bps=0.0,
                net_per_bar_bps=0.0,
            )
        gross = self.gross_sum / self.n_trades
        net = gross - fee_rt
        net_per_bar = net * (self.n_trades / self.n_bars)
        return BasisReversionResult(
            n_bars=self.n_bars, n_trades=self.n_trades, n_long=self.n_long,
            n_short=self.n_short, avg_hold_bars=self.hold_sum / self.n_trades,
            unclosed=self.unclosed, win_rate=self.n_win / self.n_trades,
            gross_bps=gross, fee_bps=fee_rt, net_edge_bps=net,
            net_per_bar_bps=net_per_bar,
        )


def _zscore(value: float, window: list[float]) -> float:
    """Population z-score of ``value`` against ``window`` (0.0 if std==0)."""
    n = len(window)
    if n == 0:
        return 0.0
    mean = sum(window) / n
    var = sum((w - mean) ** 2 for w in window) / n
    std = var ** 0.5
    return (value - mean) / std if std > 0 else 0.0


def _simulate_into(
    acc: _Accum,
    bars: list[BasisBar],
    *,
    lookback_bars: int,
    entry_z: float,
    exit_z: float,
) -> None:
    """No-lookahead basis-reversion fill sim for one coin into a shared accumulator.

    At bar ``i`` (``i >= lookback_bars − 1``) the basis z-score is computed over the
    trailing ``lookback_bars`` basis values *ending at i* (all known at close ``i`` —
    no lookahead). While flat: a z ≥ ``entry_z`` (perp rich) opens a SHORT perp at
    ``perp[i]``; a z ≤ ``−entry_z`` (perp cheap) opens a LONG. While in a position:
    when ``|z| ≤ exit_z`` it closes at ``perp[i]`` and books the perp move in the
    trade direction (``direction × (perp[i]/entry − 1)``). Positions open at series
    end are reported but not booked (no optimistic mark)."""
    basis = [b.basis for b in bars]
    pos = 0          # +1 long perp, -1 short perp, 0 flat
    entry_px = 0.0
    entry_i = 0
    for i in range(lookback_bars - 1, len(bars)):
        acc.n_bars += 1
        window = basis[i - lookback_bars + 1: i + 1]
        z = _zscore(basis[i], window)
        if pos == 0:
            if z >= entry_z:
                pos, entry_px, entry_i = -1, bars[i].perp, i
            elif z <= -entry_z:
                pos, entry_px, entry_i = 1, bars[i].perp, i
        elif abs(z) <= exit_z:
            ret = (bars[i].perp / entry_px - 1.0) * 10_000.0 if entry_px > 0 else 0.0
            acc.add_trade(pos * ret, i - entry_i, is_long=pos == 1)
            pos = 0
    if pos != 0:
        acc.unclosed += 1


def simulate_basis_reversion(
    bars: list[BasisBar],
    *,
    lookback_bars: int = 48,
    entry_z: float = 2.0,
    exit_z: float = 0.5,
    maker_fee_bps: float = DEFAULT_MAKER_FEE_BPS,
) -> BasisReversionResult:
    """Single-coin perp-vs-spot basis-reversion backtest. See ``_simulate_into``."""
    acc = _Accum(maker_fee_bps)
    _simulate_into(
        acc, bars, lookback_bars=lookback_bars, entry_z=entry_z, exit_z=exit_z
    )
    return acc.result()


def simulate_universe_basis(
    bars_by_coin: dict[str, list[BasisBar]],
    *,
    lookback_bars: int = 48,
    entry_z: float = 2.0,
    exit_z: float = 0.5,
    maker_fee_bps: float = DEFAULT_MAKER_FEE_BPS,
) -> BasisReversionResult:
    """Pool basis-reversion round-trips across a coin universe (equal-weight/trade)."""
    acc = _Accum(maker_fee_bps)
    for bars in bars_by_coin.values():
        _simulate_into(
            acc, bars, lookback_bars=lookback_bars, entry_z=entry_z, exit_z=exit_z
        )
    return acc.result()
