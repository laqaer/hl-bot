"""Forward-record live WS trades into fine-cadence candles for backtesting.

HL only retains ~one ``candleSnapshot`` cap of history (Iteration 39): ~17.5d at
5m, ~3.6d at 1m. So the *fine-cadence / sub-bar* durability research that every
remaining edge thesis would need (REVIEW C7, the dead "direction (a)") is
structurally impossible from HL's historical candle API — the data simply isn't
retained. The only route back to that research is to **forward-record our own
fine candles now**, so that months from now a 1m/5m archive exists that the
existing backtester can replay.

This module is that recorder, split (like ``ingest/ws.py``) into a pure,
unit-tested core and a thin network loop:

  * ``TradeCandleAggregator`` — pure: folds a stream of trades (the dicts
    ``MarketState`` already produces from the WS ``trades`` channel) into OHLCV
    candles, emitting the **exact same dict shape** ``fetch_candles`` returns
    (``{t,T,o,h,l,c,v,n}`` + ``coin``). So recorded candles flow straight into
    ``build_frames`` → the backtester with zero adaptation.
  * ``append_candles`` / ``load_recorded_candles`` — an append-only JSONL archive
    (under gitignored ``data/``) that a long-running recorder can safely restart
    against; ``load_recorded_candles`` returns a ``candles_by_coin`` dict that is
    a drop-in for ``build_frames``.
  * ``run_recorder`` — the thin WS connect loop (not unit-tested).
"""

from __future__ import annotations

import gzip
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .data import INTERVAL_MS

CandleDict = dict[str, Any]

_DAY_MS = 86_400_000


def bucket_open_ms(ts_ms: int, interval_ms: int) -> int:
    """Open timestamp of the candle bucket containing ``ts_ms`` (HL convention)."""
    return ts_ms - (ts_ms % interval_ms)


class TradeCandleAggregator:
    """Fold a trade stream into OHLCV candles at a fixed interval (pure).

    Trades are bucketed by open time. ``open``/``close`` are tracked by trade
    timestamp (not arrival order) so mildly out-of-order WS delivery still yields
    the correct first/last price. ``flush_completed(now_ms)`` hands back every
    bucket strictly older than the bucket containing ``now_ms`` (i.e. no longer
    receiving trades) and drops it, so memory stays bounded over a long run.
    """

    def __init__(self, interval: str = "1m") -> None:
        if interval not in INTERVAL_MS:
            raise ValueError(f"unknown interval {interval!r}; known: {sorted(INTERVAL_MS)}")
        self.interval = interval
        self.interval_ms = INTERVAL_MS[interval]
        # (coin, bucket_open_ms) -> mutable bucket record
        self._buckets: dict[tuple[str, int], dict[str, Any]] = {}

    def add_trade(self, coin: str, px: float, sz: float, ts_ms: int) -> None:
        """Fold one trade into its bucket. Non-positive px or empty coin ignored."""
        try:
            px = float(px)
            sz = float(sz)
            ts_ms = int(ts_ms)
        except (TypeError, ValueError):
            return
        if not coin or px <= 0:
            return
        t = bucket_open_ms(ts_ms, self.interval_ms)
        key = (coin, t)
        b = self._buckets.get(key)
        if b is None:
            self._buckets[key] = {
                "coin": coin, "t": t, "T": t + self.interval_ms - 1,
                "o": px, "h": px, "l": px, "c": px, "v": sz, "n": 1,
                "_first_ts": ts_ms, "_last_ts": ts_ms,
            }
            return
        b["h"] = max(b["h"], px)
        b["l"] = min(b["l"], px)
        b["v"] += sz
        b["n"] += 1
        if ts_ms <= b["_first_ts"]:
            b["o"] = px
            b["_first_ts"] = ts_ms
        if ts_ms >= b["_last_ts"]:
            b["c"] = px
            b["_last_ts"] = ts_ms

    @staticmethod
    def _clean(b: dict[str, Any]) -> CandleDict:
        return {k: v for k, v in b.items() if not k.startswith("_")}

    def pending_candles(self) -> list[CandleDict]:
        """All buckets currently held (oldest-first), without removing any."""
        return [self._clean(self._buckets[k]) for k in sorted(self._buckets, key=lambda k: (k[1], k[0]))]

    def flush_completed(self, now_ms: int) -> list[CandleDict]:
        """Pop + return candles for every bucket older than ``now_ms``'s bucket."""
        cur = bucket_open_ms(int(now_ms), self.interval_ms)
        done_keys = [k for k in self._buckets if k[1] < cur]
        done_keys.sort(key=lambda k: (k[1], k[0]))
        out = [self._clean(self._buckets.pop(k)) for k in done_keys]
        return out


# ---------------------------------------------------------------------------
# Append-only archive (JSONL; gitignored under data/)
# ---------------------------------------------------------------------------


def _open_append(path: Path) -> Any:
    return gzip.open(path, "at", encoding="utf-8") if path.suffix == ".gz" else path.open("a", encoding="utf-8")


def _open_read(path: Path) -> Any:
    return gzip.open(path, "rt", encoding="utf-8") if path.suffix == ".gz" else path.open("r", encoding="utf-8")


def append_candles(path: str | Path, candles: list[CandleDict]) -> int:
    """Append candle dicts to a JSONL archive (one per line). Returns count written.

    Append-only so a restarted recorder never rewrites history; re-flushing the
    same bucket simply writes a newer line that ``load_recorded_candles`` resolves
    by keeping the last occurrence per (coin, t).
    """
    p = Path(path)
    if not candles:
        return 0
    p.parent.mkdir(parents=True, exist_ok=True)
    with _open_append(p) as fh:
        for c in candles:
            fh.write(json.dumps(c, separators=(",", ":")) + "\n")
    return len(candles)


def load_recorded_candles(path: str | Path) -> dict[str, list[CandleDict]]:
    """Load a JSONL candle archive into a ``candles_by_coin`` dict for ``build_frames``.

    Dedupes by (coin, open time ``t``) keeping the **last** line written, and
    returns each coin's candles oldest-first — a drop-in for
    ``data.build_frames(candles_by_coin=...)``.
    """
    p = Path(path)
    if not p.exists():
        return {}
    latest: dict[tuple[str, int], CandleDict] = {}
    with _open_read(p) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                c = json.loads(line)
                key = (c["coin"], int(c["t"]))
            except (ValueError, KeyError, TypeError):
                continue
            latest[key] = c
    by_coin: dict[str, list[CandleDict]] = {}
    for (coin, _t), c in sorted(latest.items(), key=lambda kv: (kv[0][0], kv[0][1])):
        by_coin.setdefault(coin, []).append(c)
    return by_coin


# ---------------------------------------------------------------------------
# Coverage / readiness report (pure)
# ---------------------------------------------------------------------------
#
# The recorder accumulates a fine-cadence archive over calendar weeks (the only
# route back to the sub-bar / fine-cadence research HL's retention ceiling blocks,
# Iteration 39). The missing piece between "the recorder runs" and "re-run the
# fine-cadence theses" is knowing *when enough gap-free data exists*: a durability
# backtest over an archive with silent gaps (a recorder restart, a WS dropout)
# would be quietly corrupt. These pure functions answer that — per-coin span +
# gap accounting, and a single READY/NOT-READY verdict against the durability bar.


@dataclass(frozen=True)
class CoinCoverage:
    """Span + gap accounting for one coin's recorded candles."""

    coin: str
    n_candles: int
    first_t: int  # open ms of the earliest candle (0 if none)
    last_t: int  # open ms of the latest candle (0 if none)
    span_days: float  # (last_t - first_t) in days; a single candle spans 0
    expected: int  # buckets that *should* exist across [first_t, last_t]
    coverage: float  # n_candles / expected (1.0 = no gaps)
    largest_gap: int  # longest run of consecutive missing buckets


def coin_coverage(candles: list[CandleDict], interval: str, coin: str = "") -> CoinCoverage:
    """Coverage stats for one coin's candle list (any order; deduped by open ``t``).

    ``coin`` names the series (falls back to the candles' own ``coin`` field).
    """
    if interval not in INTERVAL_MS:
        raise ValueError(f"unknown interval {interval!r}; known: {sorted(INTERVAL_MS)}")
    step = INTERVAL_MS[interval]
    opens = sorted({int(c["t"]) for c in candles if "t" in c})
    name = coin or next((str(c.get("coin", "")) for c in candles if "t" in c), "")
    n = len(opens)
    if n == 0:
        return CoinCoverage(name, 0, 0, 0, 0.0, 0, 0.0, 0)
    first_t, last_t = opens[0], opens[-1]
    expected = (last_t - first_t) // step + 1
    span_days = (last_t - first_t) / _DAY_MS
    largest_gap = 0
    for a, b in zip(opens, opens[1:], strict=False):
        largest_gap = max(largest_gap, (b - a) // step - 1)
    coverage = n / expected if expected else 1.0
    return CoinCoverage(name, n, first_t, last_t, span_days, expected, coverage, largest_gap)


def archive_coverage(by_coin: dict[str, list[CandleDict]], interval: str) -> list[CoinCoverage]:
    """Per-coin :class:`CoinCoverage` for a whole archive, sorted by coin."""
    return [coin_coverage(by_coin[coin], interval, coin) for coin in sorted(by_coin)]


@dataclass(frozen=True)
class Readiness:
    """READY / NOT-READY verdict for running the durability bar off the archive."""

    ready: bool
    interval: str
    window_days: float
    n_windows: int
    min_coverage: float
    required_days: float  # window_days * n_windows of contiguous data needed
    coverages: list[CoinCoverage] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)


def archive_readiness(
    by_coin: dict[str, list[CandleDict]],
    interval: str,
    *,
    window_days: float,
    n_windows: int = 2,
    min_coverage: float = 0.98,
    min_coins: int = 2,
) -> Readiness:
    """Is the archive ready for an ``n_windows``×``window_days`` durability backtest?

    The bar needs ``n_windows`` disjoint windows of ``window_days`` each, so every
    coin needs ``required_days = window_days * n_windows`` of span at ``coverage >=
    min_coverage`` (gaps would silently corrupt a durability run). Cross-sectional
    theses need a basket, so ``min_coins`` coins must clear the bar. Returns a
    single verdict plus the per-coin blockers that explain a NOT-READY.
    """
    required_days = window_days * n_windows
    covs = archive_coverage(by_coin, interval)
    reasons: list[str] = []
    n_ok = 0
    for c in covs:
        short = c.span_days < required_days
        gappy = c.coverage < min_coverage
        if short:
            reasons.append(f"{c.coin}: span {c.span_days:.1f}d < required {required_days:.0f}d")
        if gappy:
            reasons.append(
                f"{c.coin}: coverage {c.coverage:.3f} < {min_coverage:.3f} "
                f"(largest gap {c.largest_gap} bars)"
            )
        if not short and not gappy:
            n_ok += 1
    if n_ok < min_coins:
        reasons.append(f"only {n_ok} coin(s) clear the bar; need {min_coins}")
    return Readiness(
        ready=n_ok >= min_coins,
        interval=interval,
        window_days=window_days,
        n_windows=n_windows,
        min_coverage=min_coverage,
        required_days=required_days,
        coverages=covs,
        reasons=reasons,
    )


# ---------------------------------------------------------------------------
# Connect loop (thin; network — not unit-tested)
# ---------------------------------------------------------------------------


def run_recorder(
    coins: list[str],
    archive_path: str | Path,
    *,
    interval: str = "1m",
    base_url: str = "https://api.hyperliquid.xyz",
    flush_interval_s: float = 5.0,
    duration_s: float | None = None,
) -> None:  # pragma: no cover - requires a live socket
    """Subscribe to HL WS trades for ``coins`` and append completed candles forever.

    Folds the live ``trades`` stream into ``interval`` candles and appends each
    completed bucket to ``archive_path`` (JSONL). Intended to be supervised
    (systemd) so a fine-cadence archive accumulates for future backtesting.
    ``duration_s=None`` runs forever.
    """
    from hyperliquid.info import Info

    agg = TradeCandleAggregator(interval)

    def on_trades(msg: dict[str, Any]) -> None:
        data = msg.get("data")
        if not isinstance(data, list):
            return
        for tr in data:
            try:
                agg.add_trade(tr.get("coin"), tr.get("px", 0), tr.get("sz", 0), int(tr.get("time", 0) or 0))
            except (TypeError, ValueError):
                continue

    info = Info(base_url, skip_ws=False)
    for coin in coins:
        info.subscribe({"type": "trades", "coin": coin}, on_trades)

    start = time.time()
    while duration_s is None or time.time() - start < duration_s:
        time.sleep(flush_interval_s)
        append_candles(archive_path, agg.flush_completed(int(time.time() * 1000)))
