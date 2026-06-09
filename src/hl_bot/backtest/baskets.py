"""Canonical, named coin baskets for reproducible backtests.

Every recorded confirm/backtest result is only honest if the *exact* universe it
ran on is known. Across the edge search the same baskets are typed by hand over
and over (majors, high-funding alts, the held-out alt set, the wide-majors set),
and a single typo silently changes a result. This module pins those universes to
version-controlled names so a result can cite ``--coins majors`` and mean one
unambiguous, auditable list.

``resolve_basket`` expands preset names and passes bare symbols through, so it is
backward compatible: ``--coins BTC,ETH`` is unchanged, ``--coins majors`` expands,
and ``--coins majors,DOGE`` mixes the two (deduped, order-preserving).
"""

from __future__ import annotations

# Preset name -> coin list. Names are lower_snake; symbols are UPPER, so a token
# is unambiguously one or the other. Each basket is the one used in the cited
# iteration (see ralph/BACKLOG.md / PROGRESS.md), kept here so the provenance of
# every recorded number is reproducible.
BASKETS: dict[str, list[str]] = {
    # The standard liquid-majors universe (confirm default; the B-horizon lead).
    "majors": ["BTC", "ETH", "SOL", "HYPE"],
    # The original B1 taker-tax / 120d majors basket.
    "majors6": ["AVAX", "BTC", "ETH", "HYPE", "LINK", "SOL"],
    # Wider majors breadth (Iteration 27 slice-5): widening *breaks* sign-stability.
    "majors_wide": ["BTC", "ETH", "SOL", "HYPE", "DOGE", "XRP", "LTC",
                    "BNB", "AVAX", "LINK", "SUI", "AAVE"],
    # High-funding alts (B1-alt): carry thesis pruned here.
    "alts_highfunding": ["INJ", "PURR", "TRUMP", "AERO", "NIL",
                         "APT", "SPX", "PYTH", "EIGEN", "S"],
    # Disjoint liquid-alt held-out set (B-mom-regime-validate).
    "alts_heldout": ["SUI", "SEI", "TIA", "WLD", "ARB",
                     "OP", "ENA", "JUP", "LDO", "AAVE"],
}


def resolve_basket(spec: str) -> list[str]:
    """Expand a ``--coins`` spec into a concrete, deduped coin list.

    Each comma-separated token is either a preset name (expanded via ``BASKETS``)
    or a bare coin symbol (uppercased and kept as-is). Order is preserved by first
    occurrence and duplicates are dropped, so ``majors,DOGE,BTC`` yields the four
    majors then DOGE (BTC already present). Empty/whitespace tokens are ignored.
    """
    out: list[str] = []
    seen: set[str] = set()
    for raw in spec.split(","):
        token = raw.strip()
        if not token:
            continue
        expanded = BASKETS.get(token.lower(), [token.upper()])
        for coin in expanded:
            if coin not in seen:
                seen.add(coin)
                out.append(coin)
    return out
