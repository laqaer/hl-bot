# Strategy Spec: S5 — cross-venue funding signal (phase 1: signal only)

> Priority 2 in docs/ALPHA_ROADMAP.md §2. No second-venue account needed for
> phase 1 — this is data plumbing that sharpens the existing carry agents.

## Thesis
Funding on one venue mean-reverts toward the cross-venue consensus: when HL
funding is far above Binance/Bybit funding for the same coin, HL's print is
the outlier (venue-local crowding) and is more likely to stay rich briefly
then converge — exactly the episodes worth shorting on HL. Cross-venue
spread is a *cleaner* carry entry signal than HL funding alone, and it's
free.

## Signal
- Inputs (NEW data): Binance `GET /fapi/v1/premiumIndex` and Bybit
  `GET /v5/market/tickers` funding fields, fetched on the host each cycle of
  a small poller (no auth, public, rate-limit friendly at 1/min). Persisted
  to a `xvenue_funding` table; exposed in MarketView extra as
  `funding_xvenue: {coin: {binance: r, bybit: r}}`.
- Use (phase 1): carry agents require HL funding ≥ consensus + spread
  threshold (sweep: 5/10/20 bps per 8h equivalent) instead of an absolute
  threshold alone; femr uses the spread z-score as its extremity measure.
- Phase 2 (separate spec, needs accounts/capital split): short rich venue /
  long cheap venue when spread exceeds round-trip costs on both venues.

## Cost sensitivity
Phase 1 adds zero execution cost — it only changes WHICH trades the existing
agents take. The validation question is selectivity: does conditioning on
cross-venue spread raise realized bps/trade on the 180d backtest?

## Risk
Phase 1: none beyond existing agents'. Symbol mapping errors (kPEPE vs
1000PEPE etc.) are the real hazard — mapping table with explicit tests.

## Validation plan
- Backfill: Binance funding history is downloadable (fundingRate endpoint,
  1000-row pages) for the same 180d window; join against cached HL funding;
  measure conditional edge of HL-carry entries with vs. without the spread
  filter. Pure offline study — do this BEFORE any live plumbing.
- G0 expectation: spread-filtered entries beat unfiltered by ≥ 2bps/trade
  net CONFIRMS; no improvement REFUTES (then HL funding alone is sufficient
  and this becomes dead weight to delete).

## Sources
Binance/Bybit public API docs; perp funding convergence is documented across
venues (the basis trade literature); symbol-mapping gotchas from practice.
