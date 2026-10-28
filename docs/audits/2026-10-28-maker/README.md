# Legacy maker helper audit

The two legacy helpers have no production callers. The current maker lifecycle has separate tests that detect lost ownership and expiry.

## Scope and base

- Repository: `laqaer/hl-bot`.
- Base: `d10ed2f0aef787d6887427c8cfd69645280f944d`.
- Fetch date: 2026-10-28. `origin/main` still matched the base.
- Worktree: `hl-bot--deep-test-audit`, branch `audit/legacy-maker-helpers`.
- Main checkout: preserve the existing edits in `ralph/loop.sh` and `scripts/auto_tuner.py`.

This batch removes internal helpers and their two tests. It changes no CLI command, config field, database schema, or trading behavior.

## Deletion evidence

### Fill reconciliation

- Test: `tests/test_maker_lifecycle.py::test_fill_detection_makes_it_owned`.
- Actual failure detected: `reconcile_maker_fills` fails to report a filled coin or record its ownership.
- Control: replacing the helper with `return []` failed the test with `[] != ['BTC']`.
- Non-test callers: none. The complete tracked-file search found only the definition and this test.
- Remaining proof: `tests/test_lifecycle.py::test_apply_fill_logs_ownership` checks the current maker state and the `rest`/`place` audit sequence.
- Keeper control: disabling the current fill transition failed that test with `quoted != filled`.
- Unique old assertion: the removed test also checks that a `place` row clears the legacy `working_orders` projection.
- That assertion is obsolete, not duplicated by the current lifecycle test. Its only source consumer is the orphan `router.execute_decisions` path.
- History: `c5eba01` added the helper and test. `9473917` removed its last CLI caller when `femr_tick` delegated to `run`.
- Deletion unlocked: `reconcile_maker_fills`, its test, and the test-only `_fill` helper.
- Risk: low. The current engine uses `lifecycle.apply_actions` through `engine.runner.run_cycle`.

### Stale quote detection

- Test: `tests/test_maker_lifecycle.py::test_stale_detection`.
- Actual failure detected: `stale_working` fails to identify an old SOL quote while excluding a fresh BTC quote.
- Control: replacing the helper with `return []` failed the old-quote assertion with `[] != ['SOL']`.
- Non-test callers: none. The complete tracked-file search found only the definition and this test.
- Remaining proof: `tests/test_lifecycle.py::test_entry_expires_after_max_rest` checks expiry in the current maker state machine.
- Keeper control: disabling current entry expiry failed that test with `[] != ['expire']`.
- History: `c5eba01` added the helper and test. `9473917` removed its last CLI caller.
- Deletion unlocked: `stale_working`, its test, `DEFAULT_MAX_REST_S`, and the unused production `time` import.
- Risk: low. Current expiry, partial-fill, cancellation-failure, and kill-switch tests remain.

## Contract and preservation checks

`pyproject.toml` declares one command entry point: `hlbot = "hl_bot.cli.main:main"`. It declares no maker helper API.
`src/hl_bot/exec/__init__.py` exports no helper API. No tracked SDK, CLI, config, or documentation contract names either removed helper.

Keep `log_rest` and `log_cancel`. The current lifecycle still calls them.
Keep `maker_price` and `working_orders` in this batch. The router module references them; its separate audit has not landed.
Keep both remaining tests in `test_maker_lifecycle.py`. They check that resting orders have no position ownership and cancellation clears working state.
Keep `femr_tick` and its tests. That deprecated command remains a public compatibility path.

Remove the unused module logger and its import. Tracked callers import named maker helpers; none imports or reads `maker.log`.

The retained lifecycle tests check current persisted state and audit output. They remain lower-level tests; this audit does not call them black-box E2E tests.

## Proof recorded before editing

| Check | Result | Evidence |
|---|---|---|
| Current-main baseline, `uv run --frozen --offline pytest -q` | 344 passed; one existing deprecation warning | [baseline](logs/baseline-pytest.log) |
| Native CI lint, `uv run --frozen --offline ruff check .` | Passed | [lint](logs/baseline-ruff.log) |
| Legacy no-op control | Both candidate tests failed for the stated reasons | [log](logs/control-legacy-noop.log), [patch](controls/legacy-noop.patch) |
| Current lifecycle no-op control | Both retained tests failed for the stated reasons | [log](logs/control-keeper-noop.log), [patch](controls/keeper-noop.patch) |
| Helper deletion in a disposable copy | 342 passed; the same deprecation warning | [log](logs/control-exact-deletion.log), [patch](controls/exact-deletion.patch) |

The controls ran in separate copies of the same base. No runner was active in the worktree during source or test edits.
The worktree uses dependencies from `uv sync --frozen --all-extras --dev`. The initial sandbox download failed on DNS; the approved retry passed.

The test commands disabled dotenv loading and cleared trading and Telegram settings. They used an isolated home directory.
No check placed an order, sent a message, or changed the live database.

## Validation after editing

The following checks passed after the deletion:

```sh
uv run --frozen --offline pytest -q tests/test_maker_lifecycle.py tests/test_lifecycle.py tests/test_router.py tests/test_runner.py tests/test_orders_maker.py tests/test_femr_tick.py
uv run --frozen --offline pytest -q
uv run --frozen --offline ruff check .
uv run --frozen --offline hlbot --help
uv run --frozen --offline hlbot run --help
git diff --check
```

The focused owner and sibling run passed 58 tests. The full suite passed 342 tests.
Both runs reported the existing `femr_tick` deprecation warning. Ruff and both CLI help commands passed.
See [focused tests](logs/final-focused.log), [full tests](logs/final-pytest.log), [Ruff](logs/final-ruff.log), [CLI help](logs/final-cli-help.log), and [run help](logs/final-run-help.log).

The repository has no dedicated black-box E2E suite. The help commands are safe CLI import and command-registration smoke checks.
They do not prove live exchange connectivity or live order execution.

Production changes add four lines and remove 56 lines. Test changes add two lines and remove 49 lines.
The additional instruction and audit files contain no runtime behavior changes.

## Independent review

A separate Codex subagent reviewed the final source and test diff. It found no blocking coverage loss.
See [the preservation review](preservation-review.md) for the reviewed hashes, evidence, and limits.

## Documentation review

Reviewed this report, the changed module descriptions, and `CLAUDE.md` against the ten documentation checks.
The prose satisfies at least eight checks. Exact commands, identifiers, assertion output, and source pins retain their original spelling and length.
