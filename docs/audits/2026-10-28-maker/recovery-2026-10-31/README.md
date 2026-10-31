# Issue 39 recovery and verification

## Task and boundary

Resume [issue #39](https://github.com/laqaer/hl-bot/issues/39). Recover the named branch, inspect its change, run repository checks, and integrate the result.

- Repository: `laqaer/hl-bot`. The requested `myrmitis/hl-bot` issue URL returned HTTP 404.
- Owner: the operator, through the 2026-10-31 request. Executor: OpenAI Codex, session `01a191c2-2788-7520-8b13-e6b96117a04b`.
- Worktree: `~/dev/hl-bot--deep-test-audit`; branch: `audit/legacy-maker-helpers`.
- Recovered head: [`f3bf1e3dc929379c2c01fce67f8541903dd0a266`](https://github.com/laqaer/hl-bot/commit/f3bf1e3dc929379c2c01fce67f8541903dd0a266).
- Original base: `d10ed2f0aef787d6887427c8cfd69645280f944d`.
- Current integration base: `f3bf1e3dc929379c2c01fce67f8541903dd0a266`.
- Scope: legacy maker helper removal, review, verification, and this recovery record.
- Exclusions: deployment, live trading, runtime acceptance, and unrelated edits in the main checkout.
- Completion boundary: source integration. Acceptance requires passing repository checks, independent review, current platform gates, and a merge receipt.

## Recovery findings

The named worktree was clean and contained the exact commit named by the issue. No restore from the backup ref was needed.
The backup ref itself was not fetched or verified.

A fresh fetch on 2026-10-31 showed that remote `main` already contained the exact recovered commit.
GitHub reported a successful [push CI run](https://github.com/laqaer/hl-bot/actions/runs/40618400409) for that commit on 2026-10-28.
The commit-to-PR API returned no associated PR. The issue's claim that the commit was unpushed was stale.
This follow-up PR records verification and closes the recovery task. It does not reapply the source deletion.
Use the PR linked from [issue #39](https://github.com/laqaer/hl-bot/issues/39) for the final integration receipt.

The existing main checkout had edits in `ralph/loop.sh` and `scripts/auto_tuner.py`. This session did not change them.
The portfolio still marks this bot as superseded and paper-only. No deployment or trading command ran.

## Fresh local checks

Checks ran at the recovered head on macOS arm64, with Python 3.11.15 and uv 0.11.19.
The existing uv environment supplied dependencies. The environment omitted trading and Telegram credentials, disabled dotenv loading, and selected paper mode.
The [command results](results.json) retain UTC start times, durations, exit codes, and log paths.
The run started at 2026-10-31 11:42:50 UTC, or 19:42:50 Asia/Shanghai.

| Check | Result | Evidence |
| --- | --- | --- |
| `uv run --frozen --offline pytest -q` | 342 passed | [pytest log](1-uv-pytest.log) |
| `uv run --frozen --offline ruff check .` | Passed | [lint log](2-uv-ruff.log) |
| `uv run --frozen --offline hlbot --help` | Passed | [CLI log](3-uv-hlbot.log) |
| `uv run --frozen --offline hlbot run --help` | Passed | [run help log](4-uv-hlbot.log) |
| `git diff --check` | Passed | [diff log](5-git-diff.log) |

Pytest reported the existing `femr_tick` deprecation warning and a sandbox cache-write warning. Neither caused a test failure.
No dedicated black-box E2E suite exists. Pytest is the documented repository suite; CLI help checks verify imports and command registration only.
These results do not establish exchange connectivity, live order execution, or deployed behavior.

## Independent review

A separate OpenAI Codex reviewer inspected `d10ed2f..f3bf1e3` and found no blocking correctness or requirements defect.
Reviewer: `/root/review_legacy_change`, session `01a191c3-9657-7541-9a1f-be57175dbc5d`.
The reviewer read source, relevant history, and selected evidence. It did not execute tests or edit files.

Both removed helpers have no production callers. Commit `9473917` removed their last CLI caller.
The current runner uses `lifecycle.plan_actions` and `apply_actions`; their implementation and tests remain.
The removed fill test uniquely checked the legacy working-order projection. Its router has no current production caller.
The [original audit](../README.md) accurately records that coverage distinction. The public `femr_tick` compatibility command and tests remain intact.
There were no findings to repair. Hosted checks and final merge status remain separate from this source review.

## Evidence limits and cost

The repository has no installed `scripts/project-ops/execution-evidence.mjs` validator. This record retains the observations without claiming schema validation.
The portfolio command also referenced a missing `projects/briefs/hyperliquid.md`; the registry and execution policy supplied the project boundary.
The recovered source is already integrated. This record's PR must still pass its current gates before merge.
No installation, deployment, or runtime acceptance is claimed.

One helper supplied independent review. No repair attempt was needed. Local command durations are in `results.json`.
Model, agent compute, and CI monetary costs are unknown, not zero.
The author checked the new prose against all ten documentation-style checks. Exact commands, source hashes, and frozen log output retain their original form.
