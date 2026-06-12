# Strategy Pipeline — idea to live capital

The meta-workflow that turns research into deployed, auto-promoted strategies.
Two legs: **deep research** (Claude Code web sessions, web access) and the
**data-heavy leg** (the EC2 host: nightly sweeps, paper soak, live execution).
Evidence flows one way — toward the gates — and capital only moves when gates
pass. Nothing in this pipeline may weaken a gate (CI enforces minima in
`tests/test_gate_minima.py`).

```
 idea ──► spec (docs/research/<name>.md) ──► agent impl + unit tests
      ──► hlbot sweep (nightly grid over real history)        [host]
      ──► hlbot confirm --record   (G0 stamp)                  [host]
      ──► paper soak (hlbot run simulates it automatically)    [host]
      ──► auto-promotion: paper → live_small → live            [supervisor]
```

## 1. Idea sourcing (deep-research sessions)

Run a Claude Code web session with web search and ask it to produce a spec
(template: [`research/SPEC_TEMPLATE.md`](research/SPEC_TEMPLATE.md)). Good
prompts to start from:

- *"Survey post-2023 literature and practitioner writing on perp funding-rate
  premium harvesting: cross-sectional carry, momentum in funding, basis
  convergence. For each candidate: signal definition, expected gross edge in
  bps, decay horizon, and cost sensitivity at 1bp maker / 4.5bp taker fees.
  Output a spec per viable strategy for a $1k–10k Hyperliquid account."*
- *"What do liquidation-cascade studies (crypto perps, 2022–2025) say about
  post-cascade drift magnitude and half-life? Calibrate entry/exit windows
  for a momentum strategy consuming a live liquidation feed."*
- *"Find documented Hyperliquid-specific microstructure edges: maker rebate
  tiers, HLP behavior, oracle update cadence, funding clamp mechanics.
  Which are harvestable at retail size?"*
- *"Audit our strategy spec <paste spec> like a quant PM: what would make
  this fail OOS? What's the capacity? What regime kills it?"*

Rules for research sessions:
- Every spec must state **costs first**: a signal that needs > 2 round-trips
  per day per coin is dead at taker; say which execution mode it assumes.
- Prefer strategies whose edge is a **structural cash flow** (funding, rebates,
  forced flows) over pattern prediction.
- The session's deliverables: `docs/research/<name>.md` + a `ralph/BACKLOG.md`
  item linking it. Specs are proposals — the sweep/confirm gates decide.

## 2. Implementation

- Agent in `src/hl_bot/agents/<name>.py` implementing `decide(view)`; factory
  registered in `engine/runner.py::AGENT_FACTORIES`; YAML contract in
  `configs/<name>.yaml` (roster, guardrails, ladder — minima enforced by CI);
  unit tests in the established synthetic-frames style.

## 3. Evidence (host, automatic)

- **Sweeps**: drop `configs/sweeps/<name>.yaml`; the nightly `hlbot-sweep`
  timer grids it over real history through the G0 harness and commits ranked
  results to `research/results/`. Read those before tuning anything.
- **G0 stamp**: `hlbot confirm --agent <name> --prefer maker --record` writes
  the confirmations row that `require_g0` ladder stages check.
- **Paper soak**: `hlbot run` simulates every paper-roster agent continuously
  (fills, funding accrual, scorecards). No manual action.

## 4. Capital (supervisor, automatic)

The promotion ladder in the agent's YAML does the rest: paper → live_small on
paper evidence + G0, live_small → live on real-fill evidence only. Sizing
grows through `risk/scaling.py` (5x/1x) + MetaAllocator. Backstops: sticky
kill switch, account daily-loss kill, equity-floor kill, order-rate limits.

## 5. Review loop

- `research/results/*.md` — what the data said last night.
- `hlbot report` / Telegram — what the book did today.
- `ralph/` — the autonomous loop that works the backlog between sessions.
