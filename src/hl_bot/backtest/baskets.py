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

# Named *pair* baskets for ``pairs_reversion_v1`` — the relative-value universe is a
# list of (rich-leg, cheap-leg) pairs, not a flat coin list, so it needs its own
# resolver. Each value is a canonical ``'A/B|C/D'`` string (``|`` separates pairs,
# ``/`` separates the two legs) matching the agent's ``--params pairs=`` syntax.
# Pinned to names so the held-out-pairs durability result (B-pairs slice 3) cites
# one auditable universe instead of a hand-typed string a typo can silently change.
PAIR_BASKETS: dict[str, str] = {
    # The agent's shipped default pairs (cross-cap / L1 / DeFi): the Iter-29 lead.
    "pairs_default": "ETH/BTC|SOL/AVAX|LINK/AAVE",
    # Disjoint liquid held-out pairs (B-pairs slice 3): no leg overlaps pairs_default
    # — two L2 govs (ARB/OP), two Move L1s (APT/SUI), two memes (DOGE/WIF). The
    # leave-pairs-out analogue of the leave-one-coin-out test.
    "pairs_heldout": "ARB/OP|APT/SUI|DOGE/WIF",
    # Pre-committed *larger diversified* book (B-pairs slice 7): the exact union of
    # pairs_default ∪ pairs_heldout — 6 pairs, every leg distinct, spanning six
    # economic buckets (cross-cap majors, L1 alts, DeFi, L2 govs, Move L1s, memes).
    # It introduces ZERO new pair choices (both halves were already pinned), so it
    # is the maximally-defensible *inverse of leave-one-out*: slice 4 showed the
    # 3-pair PASS is a portfolio/averaging effect, so the honest test is whether
    # pooling MORE imperfectly-correlated spreads (pre-committed, not hindsight)
    # makes the book MORE durable — or whether the held-out half's negativity drags
    # the pool down. Either answer is decisive; neither is basket selection.
    "pairs_diversified": "ETH/BTC|SOL/AVAX|LINK/AAVE|ARB/OP|APT/SUI|DOGE/WIF",
}


def resolve_pairs(spec: str) -> str:
    """Expand a pairs spec into a canonical ``'A/B|C/D'`` string.

    ``|``-separated tokens are each either a pair-basket name (expanded via
    ``PAIR_BASKETS``) or a bare ``'A/B'`` pair (legs upper-cased, passed through).
    Pairs are deduped by first occurrence (order-preserving), so a basket name and
    extra bare pairs mix cleanly — the pairs analogue of ``resolve_basket``. Bare
    specs like ``'ETH/BTC|SOL/AVAX'`` round-trip unchanged (backward compatible).
    """
    out: list[str] = []
    seen: set[str] = set()
    for raw in spec.split("|"):
        token = raw.strip()
        if not token:
            continue
        expanded = PAIR_BASKETS.get(token.lower())
        sub = expanded.split("|") if expanded else [token]
        for raw_pair in sub:
            pair = raw_pair.strip()
            if "/" not in pair:
                continue
            a, _, b = pair.partition("/")
            a, b = a.strip().upper(), b.strip().upper()
            if not a or not b or a == b:
                continue
            key = f"{a}/{b}"
            if key not in seen:
                seen.add(key)
                out.append(key)
    return "|".join(out)


def leave_one_pair_out(spec: str) -> list[tuple[str, str]]:
    """For a multi-pair spec, the ``(dropped_pair, remaining_spec)`` for each pair.

    The leave-pairs-out robustness probe (the pairs analogue of leave-one-coin-out):
    if a multi-pair basket's edge survives dropping *any* single pair, no one
    relationship carries the whole result alone. ``remaining_spec`` is a canonical
    ``'A/B|C/D'`` string (itself resolvable); ``dropped_pair`` is the one held out.
    Returns ``[]`` for a spec with <2 pairs (nothing to leave out).
    """
    pairs = [p for p in resolve_pairs(spec).split("|") if p]
    if len(pairs) < 2:
        return []
    return [
        (pairs[i], "|".join(pairs[:i] + pairs[i + 1:]))
        for i in range(len(pairs))
    ]


def coins_in_pairs(spec: str) -> list[str]:
    """Flat, deduped, order-preserving coin list for every leg in a pairs spec.

    Lets ``--coins pairs_heldout`` fetch exactly the legs the pair basket trades,
    so the candle universe and the pairs universe can never drift apart.
    """
    out: list[str] = []
    seen: set[str] = set()
    for pair in resolve_pairs(spec).split("|"):
        if not pair:
            continue
        for coin in pair.split("/"):
            if coin and coin not in seen:
                seen.add(coin)
                out.append(coin)
    return out


def resolve_basket(spec: str) -> list[str]:
    """Expand a ``--coins`` spec into a concrete, deduped coin list.

    Each comma-separated token is either a preset name (expanded via ``BASKETS``),
    a pair-basket name (expanded to its flat legs via ``PAIR_BASKETS`` so
    ``--coins pairs_heldout`` fetches exactly the pairs' legs), or a bare coin
    symbol (uppercased and kept as-is). Order is preserved by first occurrence and
    duplicates are dropped, so ``majors,DOGE,BTC`` yields the four majors then DOGE
    (BTC already present). Empty/whitespace tokens are ignored.
    """
    out: list[str] = []
    seen: set[str] = set()
    for raw in spec.split(","):
        token = raw.strip()
        if not token:
            continue
        low = token.lower()
        if low in PAIR_BASKETS:
            expanded = coins_in_pairs(PAIR_BASKETS[low])
        else:
            expanded = BASKETS.get(low, [token.upper()])
        for coin in expanded:
            if coin not in seen:
                seen.add(coin)
                out.append(coin)
    return out
