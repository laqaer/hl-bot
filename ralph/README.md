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

**Unattended-safety knobs** (all env, see the `loop.sh` header):

| Env | Default | What |
|---|---|---|
| `RALPH_TIMEOUT` | `1800` | Per-iteration wall-clock cap (s); a hung session is killed, not left to wedge the loop. |
| `RALPH_MAX_FAILS` | `5` | Abort after N consecutive failed/no-op iterations (a stuck or quota-exhausted run stops burning OAuth instead of spinning). |
| `RALPH_SKIP_AUTH_CHECK` | `0` | Skip the startup auth probe. |

Before looping, `loop.sh` runs a one-shot **auth probe** (`claude -p "…READY"`):
if the OAuth/Max session or `ANTHROPIC_API_KEY` is missing/expired it exits
immediately with guidance instead of silently no-op'ing every iteration — the
most common unattended failure. Pushes use bounded exponential backoff
(2/4/8/16s), and a red iteration is reverted with a full `git clean -fd`
(gitignored `data/` + `*.sqlite` are preserved) so leftover untracked files
can't poison the next verify.

## Guarantees & guardrails

- **Green-gated commits.** `loop.sh` re-runs `pytest` + `ruff` after every
  iteration; if red, it hard-resets the working tree to the last good commit. The
  history stays green.
- **No live trading, ever, from the loop.** `PROMPT.md` forbids enabling/scaling
  live capital, raising notional caps, or running live ticks. The loop is for
  research, backtests, code, and *proposals*. Going live stays a human decision.
- **Evidence before edge claims.** A strategy change isn't "done" without a
  backtest number recorded in `PROGRESS.md`.

## Unattended operation

To let it run without you in the chat:

**Prerequisites**
- `claude` CLI authenticated (Claude Max/OAuth or `ANTHROPIC_API_KEY`).
- `uv` available (tests/lint gate).
- **Outbound network to `api.hyperliquid.xyz`** so the loop can fetch real
  history for backtests (the sandbox this was built in blocks it; a normal host
  doesn't). Run `uv run hlbot backtest-fetch` once to seed the offline cache, then
  backtests run cache-only.
- **No exchange keys are needed or wanted** — the loop does research only and is
  forbidden from live trading. Do not expose the API-wallet env to it.

**Run it under tmux / nohup**
```bash
tmux new -s ralph 'RALPH_ITERS=200 RALPH_PUSH=1 ralph/loop.sh'   # detach with C-b d
# or
nohup env RALPH_ITERS=200 RALPH_PUSH=1 ralph/loop.sh > ralph/loop.out 2>&1 &
touch ralph/STOP   # graceful stop after the current iteration
```

**Or as a systemd service** (oneshot + timer, or a long-running unit). Keep it on
a dev branch with `RALPH_PUSH=1`; review the commits before merging to `main`.

The loop is green-gated and live-trading-safe, so unattended runs can only ever
improve the repo or no-op — never place an order.

## How it connects to the goal

The loop is the engine that grinds [`../docs/ROADMAP_TO_1M.md`](../docs/ROADMAP_TO_1M.md):
find one cost-surviving edge in the backtester → prove it on paper → human-gate it
to small live → scale via the existing 5×/1× risk machinery → build the track
record that justifies capital (Path A) or attracts AUM (Path C). Start every
iteration from the top of `BACKLOG.md`.
