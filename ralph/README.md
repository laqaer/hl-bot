# The Ralph loop

An autonomous self-improvement loop for hl-bot. It runs a fixed standing prompt
(`PROMPT.md`) over and over; each iteration the agent picks the top unblocked
item from `BACKLOG.md`, makes one tested change, gates on green tests+lint,
commits, and logs to `PROGRESS.md`. Named after the "Ralph Wiggum" technique:
a dumb `while` loop around a smart agent, where the intelligence lives in the
prompt + the backlog, not the control flow.

## Files

| File | Role |
|---|---|
| `PROMPT.md` | The standing instruction run every iteration. The brain. |
| `BACKLOG.md` | Prioritized task stack. The agent works it top-down. |
| `PROGRESS.md` | Append-only journal of what happened each iteration. |
| `loop.sh` | The dumb driver: run agent → verify green → commit → repeat. |

## Run it

```bash
ralph/loop.sh                 # 25 iterations, commit on green, no push
RALPH_ITERS=50 ralph/loop.sh  # more iterations
RALPH_PUSH=1 ralph/loop.sh    # push to the current branch after each green commit
touch ralph/STOP              # graceful stop after the current iteration
```

Requires the `claude` CLI on PATH and `uv` for tests/lint. Tune the model and
permission posture via `CLAUDE_MODEL` / `CLAUDE_FLAGS` (see `loop.sh` header).

## Guarantees & guardrails

- **Green-gated commits.** `loop.sh` re-runs `pytest` + `ruff` after every
  iteration; if red, it hard-resets the working tree to the last good commit. The
  history stays green.
- **No live trading, ever, from the loop.** `PROMPT.md` forbids enabling/scaling
  live capital, raising notional caps, or running live ticks. The loop is for
  research, backtests, code, and *proposals*. Going live stays a human decision.
- **Evidence before edge claims.** A strategy change isn't "done" without a
  backtest number recorded in `PROGRESS.md`.

## How it connects to the goal

The loop is the engine that grinds [`../docs/ROADMAP_TO_1M.md`](../docs/ROADMAP_TO_1M.md):
find one cost-surviving edge in the backtester → prove it on paper → human-gate it
to small live → scale via the existing 5×/1× risk machinery → build the track
record that justifies capital (Path A) or attracts AUM (Path C). Start every
iteration from the top of `BACKLOG.md`.
