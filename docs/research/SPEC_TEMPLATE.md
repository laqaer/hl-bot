# Strategy Spec: <name_vN>

> One spec per strategy. Filled by a research session, consumed by the
> implementer (human/ralph) and the sweep harness. Costs first, always.

## Thesis
What structural reason does this make money? (forced flow, cash flow,
risk premium — not "the pattern repeats")

## Signal
- Inputs (must exist in MarketView or name the new data needed):
- Entry rule:
- Exit rule (normalization / stop / max-hold):
- Expected holding period:
- Expected trade frequency (per coin per day):

## Cost sensitivity (the kill question)
- Gross edge estimate (bps/round-trip) and source:
- Execution assumption: maker / taker / mixed
- Net edge at 1bp maker + 0 slip:        ___ bps
- Net edge at 4.5bp taker + 2bp slip:    ___ bps
- Breakeven fill rate if maker:

## Risk
- Direction exposure (neutral? net long crypto beta?):
- Worst regime (what kills it) and detection signal:
- Per-trade max loss; portfolio interaction with existing agents:

## Validation plan
- Sweep grid (params x universes) for configs/sweeps/<name>.yaml:
- Data needed beyond 1h candles+funding (liq feed? L2? spot?):
- G0 expectation: what OOS edge would CONFIRM the thesis; what would refute it.

## Promotion ladder proposal
(thresholds may only be stricter than tests/test_gate_minima.py minima)

## Sources
Links / papers / posts with one-line takeaways.
