# Prop / funded-account eval — prep checklist (CAPITAL.md Track B)

Goal: additive $100–200k of firm capital by passing one evaluation with the
**same guardrailed strategy the bot already runs** — never a special
"eval-mode" gamble. An eval is a risk contract: one breach of the firm's
rules forfeits the fee (and, once funded, the account). ~87% of prop traders
fail within 6 months; the plan is to be on the right side of that by only
entering with proven evidence and pre-screened rules.

**Gate (do not pay an eval fee before this):** a live strategy with G1 PASS
and live-small evidence trending to G2 in `hlbot gates`, and a clean
`hlbot prop-check` replay (below) over ≥30d of our own equity history.
Sequencing per CAPITAL.md: vault is the lead; one eval in parallel at
month 2–3, gated on the record.

## Step 0 — verify the firm's current terms (they change)

Candidates from June-2026 research (`CAPITAL.md` Track B): **Hypernova**,
**Propr** (Hyperliquid-native, on-chain payouts, API/bot-friendly,
~$100–200k), **Velotrade** (REST/WS API, ~90% split, $5k–$200k). For the one
you pick, record — from the firm's current docs, not this repo:

| Term | What to record | Why it matters |
|---|---|---|
| Eval fee + account size | $ fee per attempt, funded size | caps the eval budget (set one; e.g. 2 attempts max) |
| Daily loss rule | %, **base** (start balance vs day-open equity), **reset hour** | the rule most evals die on; equity-based, not realized |
| Max drawdown | %, **trailing vs static** | trailing HWM is stricter than our 7d-window guardrail |
| Profit target + days | % target, min/max trading days | sets required pace; slower = safer |
| Consistency rules | e.g. "no day > 40% of total profit" | can fail a passing curve retroactively |
| Bot/API policy | bots allowed? API keys? HL-native or simulator? | we only enter API-friendly evals |
| Payout terms | split, cadence, on-chain?, KYC | the actual prize |

## Step 1 — pre-screen with our own evidence (free, do this first)

Replay our real equity curve against the firm's exact numbers on the box
that trades (it has `equity_snapshots`):

```bash
hlbot prop-check --daily-loss-pct 0.04 --daily-loss-base start \
  --max-dd-pct 0.06 --dd-mode trailing --target-pct 0.08 \
  --min-trading-days 5 --boundary-hour 0 --days 60
```

- It replays **equity (incl. unrealized)** against the day-boundary daily
  line and the HWM drawdown line — the two rules our own guardrails do NOT
  model (`check_guardrails` is rolling-24h and realized-only).
- Pass bar: **zero breaches over ≥30d** AND headroom that never got thinner
  than ~⅓ of the daily allowance (snapshots are sampled; a real eval marks
  continuously — thin margin = fail).
- Also sanity-check pace: profit target ÷ our measured monthly return must
  fit inside the firm's max days without size we've never traded.

If this fails on our own history, the eval fee is a donation. Fix the
strategy first; do not shop for a looser firm.

## Step 2 — map the firm's rules into the bot, tighter

The bot must halt **before** the firm's line, with margin for the
realized-vs-equity gap:

- `GuardrailConfig.max_daily_loss` ≤ **50% of the firm's daily $ allowance**
  (ours is realized-only and rolling — an open position's unrealized dip is
  invisible to it until close, so the buffer carries that risk).
- `max_total_notional` / `max_per_order_notional`: size so a plausible
  adverse move on the full book (e.g. 5% on every open position) stays
  inside the daily allowance. No new risk machinery — tighten the existing
  caps for the eval account.
- Supervisor guardrails in the agent YAML: a `max_drawdown` guardrail with
  `capital:` = eval start balance, threshold well inside the firm's max DD.
- Same strategy, same params as the live book. If the eval needs different
  params to pass, we don't have an edge — we have a lottery ticket.

## Step 3 — isolation wiring

- Separate wallet/API key for the eval account; **separate DB**
  (`HLBOT_DB=data/hlbot_eval.sqlite`) and separate tick/ingest/ws services —
  never share a book with the personal account.
- `HL_TRADER_ADDRESS` → the eval account (HL-native firms); if the firm runs
  its own simulator endpoint, that's an integration task to scope before
  paying (file it; do not hand-trade the eval).
- Ingest cadence ≤ 15 min so `hlbot prop-check` sees a dense curve during
  the eval.

## Step 4 — during the eval

- Daily: `hlbot prop-check` with the firm's numbers on the eval DB; read
  headroom, not just verdict.
- Hard abort for the day when intraday loss hits **50% of the firm's daily
  allowance** (the bot's tightened guardrail enforces the realized part;
  flatten manually on an unrealized scare — walking away costs a day,
  a breach costs the account).
- Never loosen anything mid-eval. Tightening-only, same as live.

## Step 5 — funded / failed

- Funded: withdraw on every payout cadence; firm capital is for compounding
  *withdrawals*, not for growing the funded account's risk.
- Failed: stop. One re-attempt only if the breach was a rule-mapping error
  (not strategy variance), and only within the pre-set eval budget. A breach
  from variance means our size/rules mapping was wrong — fix Step 1/2 first.

_Tooling: `hlbot prop-check` (read-only) + `src/hl_bot/risk/prop.py`
(`EvalProfile`, `simulate_eval`) — also usable on any (ts_ms, equity) series,
e.g. a backtest equity curve, to pre-screen a strategy at bar resolution._
