"""Edge-search summary — the negative-result finding as a publishable artifact.

The backtest search (Iterations 16–48) tested twelve structurally different alpha
theses against real Hyperliquid history through one adversarial durability bar
(two disjoint ~120d windows, walk-forward, maker-cost net). Every one was pruned:
net-negative after costs, or non-durable (regime-/window-/param-specific). That
negative result is itself a Path-C deliverable — an allocator or
the supervisor's go-live gate needs to know *what was searched, on what universe,
over what windows, and why each was rejected*, not just "no edge yet".

This module is the canonical, auditable record of that search. It is pure data +
rendering: each :class:`Thesis` cites its backlog id and the PROGRESS iteration
that produced the number, so every row can be checked against ``ralph/PROGRESS.md``.
No network, no DB — the search is over fixed history and the verdicts are final.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

# Cadence/window the durability bar runs at unless a thesis says otherwise.
DEFAULT_BAR = "1h candles, two disjoint ~120d windows, walk-forward, maker-cost net"


@dataclass(frozen=True)
class Thesis:
    """One structurally-different alpha thesis and how the search rejected it."""

    num: int  # 1..N, the order it was investigated
    key: str  # backlog id (auditable against ralph/BACKLOG.md)
    name: str
    klass: str  # directional | execution | cross-market | cross-sectional
    iterations: str  # PROGRESS iteration(s) that produced the verdict
    universe: str
    bar: str  # cadence / windows the verdict rests on
    headline: str  # the recorded net-of-cost number
    prune_reason: str


# The search, in investigation order. Numbers are transcribed from the cited
# iterations in ralph/PROGRESS.md; do not edit a row without re-citing the source.
THESES: tuple[Thesis, ...] = (
    Thesis(
        num=1,
        key="B1",
        name="TWAP mean-reversion (the original live strategy)",
        klass="directional",
        iterations="16",
        universe="majors (BTC/ETH/SOL/HYPE/AVAX/LINK)",
        bar=DEFAULT_BAR,
        headline="taker tax ~5.7bps round-trip (~73% of the bleed); "
        "twap_mr −7.7→−2.0bps taker→maker; maker-alone confirm NOT CONFIRMED",
        prune_reason="Maker removes most of the bleed but does not create edge — "
        "flat in-sample, negative OOS. No agent passes G0 on majors.",
    ),
    Thesis(
        num=2,
        key="B1-alt",
        name="Funding carry (single-name + cross-sectional)",
        klass="directional",
        iterations="16–17",
        universe="majors + high-funding alts (realized 14–48% APR |funding|)",
        bar=DEFAULT_BAR,
        headline="majors dormant (realized funding ~57% APR < 88–130% thresholds); "
        "alts NOT CONFIRMED (xfund_carry oos −16.8bps, funding_carry oos −33.2bps)",
        prune_reason="Carry collected is smaller than maker cost + the directional "
        "noise of the imperfectly-neutral legs; negative at every selectivity threshold.",
    ),
    Thesis(
        num=3,
        key="B-mom",
        name="Cross-sectional momentum / reversion (dollar-neutral rank)",
        klass="directional",
        iterations="18",
        universe="majors + high-funding alts",
        bar=DEFAULT_BAR,
        headline="NOT CONFIRMED both universes; momentum in −4.7→oos +15.8bps, "
        "reversion +2.7→oos −17.8bps (majors, maker); maker full-sample ≈ flat",
        prune_reason="The cross-sectional edge flips sign between in-sample and OOS — "
        "a mid-window regime inversion the walk-forward correctly rejects.",
    ),
    Thesis(
        num=4,
        key="B-mom-regime",
        name="Regime-gated cross-sectional momentum",
        klass="directional",
        iterations="19–20",
        universe="high-funding alts",
        bar=DEFAULT_BAR,
        headline="Iter-19 G0 PASS (maker full +8.4bps, oos sharpe +3.38) but Iter-20 "
        "out-of-time FAIL: prior 120d reverses to maker full −7.8bps; held-out basket marginal",
        prune_reason="The +8.4bps was window-specific. The regime gate fixes the within-window "
        "sign-flip but cannot make the agent survive a genuinely different time period.",
    ),
    Thesis(
        num=5,
        key="B-tsmom",
        name="Time-series (absolute) momentum / CTA trend",
        klass="directional",
        iterations="23",
        universe="majors + high-funding alts",
        bar=DEFAULT_BAR,
        headline="NOT DURABLE; trailing marginally + (majors +2.8 / alts +2.4) but "
        "in-sample negative, and the older 120d window flips sign (−4.6 / −10.6)",
        prune_reason="Both relative and absolute momentum fail identically — at the 1h/120d "
        "horizon price-return momentum in any form is regime-dominated, not a persistent edge.",
    ),
    Thesis(
        num=6,
        key="B-horizon",
        name="Majors 1-day cross-sectional momentum (lb≈14)",
        klass="directional",
        iterations="25–27",
        universe="majors (4 coins, then widened to 12)",
        bar="1d candles, up to three disjoint 240d windows",
        headline="sign-stable lead (trailing maker +46.2bps, taker-3x +36.7bps survivable) "
        "but each window fails its own walk-forward; widening the basket FLIPS sign",
        prune_reason="A real, cost-surviving, sign-stable signal on the trailing narrow basket, "
        "but regime-sensitive within every window — never clears lookback/length/breadth.",
    ),
    Thesis(
        num=7,
        key="B-pairs",
        name="Pairs / relative-value mean-reversion (log-ratio spread)",
        klass="directional",
        iterations="29–33",
        universe="3 default pairs (ETH/BTC, SOL/AVAX, LINK/AAVE) + held-out / diversified",
        bar=DEFAULT_BAR,
        headline="the only candidate to ever clear the bar (3-pair +5.3 / +8.2 DURABLE), "
        "but PASS holds ONLY at lb∈[48,56] AND entry_z≈2.0 AND exactly those 3 pairs",
        prune_reason="Fails leave-pairs-out (sign-flips), leave-one-pair-out, AND pre-committed "
        "diversification — a portfolio-averaging illusion, an over-conditioned single point.",
    ),
    Thesis(
        num=8,
        key="B-session",
        name="Session-timing (a-priori UTC hour band, zero price/funding)",
        klass="directional",
        iterations="34–35, 50",
        universe="majors (then wider majors + liquid alts)",
        bar="1h, two 120d windows; also re-run at 4h / 240d×2 (~480d)",
        headline="strongest lead: sign-stable on majors across both windows AND a 2× baseline, "
        "mirror-coherent, hour-robust — but NOT durable; alt basket sign-flips",
        prune_reason="The within-window walk-forward regime-sensitivity is NOT a boundary artifact "
        "(persists at 2× baseline) and the effect sign-flips on a disjoint liquid-alt basket. "
        "The finer time-of-day decomposition (Iter 50) does not rescue it: narrowing the hold to "
        "the US-open hours SIGN-FLIPS at neighbouring exit hours (14–15Z +2.1/−4.0, 14–17Z "
        "+4.3/−11.8 — a knife-edge), and the only sign-stable sub-window (14–16Z) is still NOT "
        "durable (older window flat +0.0).",
    ),
    Thesis(
        num=9,
        key="B-exec",
        name="Maker spread/rebate capture (symmetric + inventory-skew)",
        klass="execution",
        iterations="36–37",
        universe="majors (real OHLC intrabar fill sim)",
        bar="1h + 5m, two 120d windows, maker_fee=1bp",
        headline="symmetric quote net −2.6 to −3.3bps/fill (sign-stable, both windows); "
        "inventory-skew worse, −4.9 to −6.5bps/round-trip",
        prune_reason="Adverse selection (fill-when-wrong) exceeds the captured half-spread. The one "
        "positive structure — adverse-free in-bar round-trips — is real but unharvestable (can't pre-select).",
    ),
    Thesis(
        num=10,
        key="B-basis",
        name="Perp-vs-spot basis reversion (same-venue price gap)",
        klass="cross-market",
        iterations="38",
        universe="BTC/ETH/SOL/HYPE (the only liquid HL perp/spot overlaps)",
        bar=DEFAULT_BAR,
        headline="one sign-stable-positive point (lb=48, z=2.0, exit=0.5 → +1.6 / +11.1bps) "
        "but it is the ONLY one in the whole sweep",
        prune_reason="Knife-edge (every neighbor flips) AND a per-coin averaging artifact "
        "(BTC −7.0→+17.6, HYPE +5.2→−7.6). Universe cannot be widened. Confirms REVIEW M5.",
    ),
    Thesis(
        num=11,
        key="B-lowvol",
        name="Cross-sectional low-volatility / betting-against-volatility (BAB)",
        klass="cross-sectional",
        iterations="44",
        universe="majors + high-funding alts",
        bar=DEFAULT_BAR,
        headline="majors base net-NEGATIVE & sign-stable (full −32.7 / −8.5bps, oos −134 / −77); "
        "high-funding alts marginally + but NOT DURABLE (+5.9 / +4.3, oos −1.2 / −15.7)",
        prune_reason="BAB is the wrong sign on crypto majors (high-vol / lottery-chase outperforms — "
        "the invert mirror is + but not durable) and only a regime-sensitive lead on alts. Clears neither.",
    ),
    Thesis(
        num=12,
        key="B-illiq",
        name="Cross-sectional illiquidity / Amihud premium (price-impact per $ volume)",
        klass="cross-sectional",
        iterations="45–48",
        universe="high-funding alts + held-out alts (also majors)",
        bar="1h, two 120d windows (a 3rd disjoint window is retention-blocked at 1h; re-tested at 4h)",
        headline="the strongest lead since pairs: ✅ DURABLE on hi-fund alts (+42.0 / +13.2), "
        "survives leave-one-coin-out with NO sign-flip — but the durable PASS is over-conditioned "
        "(lb≈48 AND full basket AND the exact Amihud ratio form)",
        prune_reason="Neither standalone Amihud component is durable (pure size carries the bigger "
        "magnitude +73.6 but SIGN-FLIPS to −19.4), so the edge is an un-attributable ratio interaction, "
        "not a liquidity premium; and the decisive 3rd-window test (only possible at 4h — 1h history caps "
        "at ~208d) SIGN-FLIPS at the calendar-matched lookback (+41.1 / −10.6 / +139.5), so even the "
        "2-window PASS does not survive a cadence change.",
    ),
)

# Why the search is considered exhausted rather than merely paused (Iter 39).
SEARCH_BOUNDARY = (
    "Fine-cadence (5m/15m/1m) durability research is structurally blocked by HL data "
    "retention, not tooling: candleSnapshot caps at ~5000 bars/request and HL retains "
    "only ~one cap per interval total (1m≈3.6d, 5m≈17.5d, 15m≈52d, 1h≈208d). The "
    "durability bar needs two disjoint ~120d windows, which is impossible below 1h. "
    "Re-testing the fast edges at the cadence REVIEW C7 says they live at would require "
    "an external tick/candle archive (forward-recording or 3rd-party) — an infrastructure "
    "bet, not a candle fetch. The same ~208d/1h ceiling also caps the durability bar at "
    "TWO disjoint 120d windows at 1h: a 3rd disjoint window (Iter 48, the illiq deploy-vs-"
    "prune test) is only reachable at a coarser cadence (4h), where the illiq edge sign-flips."
)


def build_edge_search() -> dict[str, Any]:
    """Assemble the full machine-readable edge-search record."""
    by_class: dict[str, int] = {}
    for t in THESES:
        by_class[t.klass] = by_class.get(t.klass, 0) + 1
    return {
        "summary": {
            "n_theses": len(THESES),
            "n_pruned": len(THESES),  # all of them; none deployable
            "by_class": by_class,
            "verdict": "No thesis cleared the durability bar with net-of-cost edge. "
            "Every candidate was net-negative after costs or non-durable "
            "(regime-/window-/param-specific) on HL majors+alts.",
            "search_boundary": SEARCH_BOUNDARY,
        },
        "theses": [asdict(t) for t in THESES],
    }


def to_markdown(record: dict[str, Any]) -> str:
    s = record["summary"]
    lines = [
        "# hl-bot edge-search summary",
        "",
        f"**{s['n_theses']} structurally-different theses searched · "
        f"{s['n_pruned']} pruned · 0 deployable.**",
        "",
        s["verdict"],
        "",
        "## Theses",
        "| # | thesis | class | iter | net-of-cost result | why pruned |",
        "|--:|---|---|---|---|---|",
    ]
    for t in record["theses"]:
        lines.append(
            f"| {t['num']} | {t['name']} (`{t['key']}`) | {t['klass']} | "
            f"{t['iterations']} | {t['headline']} | {t['prune_reason']} |"
        )
    lines += [
        "",
        "## Durability bar",
        f"Unless a row says otherwise: {DEFAULT_BAR}. A thesis is durable only if every "
        "window confirms in *and* out of sample and the preferred-execution full-sample "
        "edge never flips sign across windows (a sign-flip is the artifact signature).",
        "",
        "## Why the search is exhausted",
        s["search_boundary"],
        "",
        "*Every number is transcribed from the cited iteration in `ralph/PROGRESS.md` and is "
        "reproducible from `src/hl_bot/backtest/` against real Hyperliquid history. "
        "Maker-only / research-only — nothing here touched capital.*",
    ]
    return "\n".join(lines)


def export(out_dir: str | Path) -> tuple[Path, Path]:
    """Write edge_search.{json,md}; return their paths."""
    record = build_edge_search()
    d = Path(out_dir)
    d.mkdir(parents=True, exist_ok=True)
    jp = d / "edge_search.json"
    mp = d / "edge_search.md"
    jp.write_text(json.dumps(record, indent=2))
    mp.write_text(to_markdown(record))
    return jp, mp
