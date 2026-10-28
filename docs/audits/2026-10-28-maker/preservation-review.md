# hl-bot maker helper preservation review

- Date: 2026-10-28.
- Provider: OpenAI Codex subagent.
- Reviewer identity: `/root/agents_audit`.
- Mode: independent source and history review. The reviewer made no repository edits and ran no tests.
- Worktree: `/Users/andyzhou/dev/hl-bot--deep-test-audit`.
- Branch: `audit/legacy-maker-helpers`.
- Base: `d10ed2f0aef787d6887427c8cfd69645280f944d`.
- Reviewed tracked diff SHA-256: `8ae9198231ccc5ee9ac4da594fd2280e4c23e4723a3cb820c7e06c07b8666ee5`.
- Hash command: `git diff --binary | shasum -a 256`.
- Production/test-only diff SHA-256: `4c307790fb984d8d97b0b62e880fb618396089b4ccf3d57a6c25a3022dde0360`.
- Production/test hash command: `git diff --binary -- src/hl_bot/exec/maker.py tests/test_maker_lifecycle.py | shasum -a 256`.
- Reviewed tracked files: `CLAUDE.md`, `src/hl_bot/exec/maker.py`, and `tests/test_maker_lifecycle.py`.
- Diff size: documentation +3/-0; production +4/-56; tests +2/-49.
- Untracked `docs/audits/` evidence existed. This review inspected selected controls and caller/history logs, not every evidence file.

## Disposition

No blocking preservation finding applies to this diff.
The two functions have no current non-test callers.
The diff preserves the current lifecycle owner, shared audit functions, and the named CLI compatibility wrapper.
This conclusion applies to the reviewed diff hash. Reread any later source or test changes before landing.

## Removal evidence

| Item | Failure the removed test detects | Callers and retirement evidence | Remaining proof and disposition |
|---|---|---|---|
| `tests/test_maker_lifecycle.py::test_fill_detection_makes_it_owned` and `maker.reconcile_maker_fills` | The old helper stops creating a live `place` row for a matching fill. The assertions also check ownership and resolution of the old working-order projection. | At base, only this test calls the helper. Commit `9473917b6b80e3a64a22e4c6d934092d998781dc` removed its last production invocation from `femr_tick`. | `tests/test_lifecycle.py::test_full_fill_detected`, `test_apply_fill_logs_ownership`, `test_fills_by_cloid_aggregates`, and `test_partial_fill_owned_on_expire` protect the current owner. Remove the obsolete helper test. See the qualification below. |
| `tests/test_maker_lifecycle.py::test_stale_detection` and `maker.stale_working` | The old predicate misclassifies a 4,000-second-old quote or a fresh quote against its 1,800-second argument. | At base, only this test calls the predicate. The same retirement commit removed its last production invocation. | `tests/test_lifecycle.py::test_entry_expires_after_max_rest` protects current configured expiry. `test_entries_not_allowed_expires_entry_quotes` protects cancellation during a halt. Remove the obsolete predicate test. The old 1,800-second default is not the current lifecycle default. |

Commit `c5eba013391135c8f7e4c01c59592755cf68f98a` introduced both helpers for the original maker CLI loop.
Commit `fc7fc3c1bf2f9fbb348c4f26bc275b97cf528433` later changed fill handling.
The retirement commit replaced the duplicated CLI loop with `run(live=live, execution=execution, max_cycles=1)`.
The diff also removes the unused `DEFAULT_MAX_REST_S`, `time` imports, and the removed test's private `_fill` fixture helper.
A final reread also covered removal of the unused module logger and its import.
These removals do not change the retained functions.

## Current owner and retained contracts

`pyproject.toml` registers only `hlbot = hl_bot.cli.main:main` as a command entry point.
`src/hl_bot/exec/__init__.py` exports neither removed function.
No README, current operational document, plugin registry, or dynamic import names either removed function.
A source import remains technically possible. This review found no declared Python SDK contract for these functions.

`engine/runner.py::run_cycle` uses the lifecycle v2 owner.
It reads `open_orders`, refreshes fills, aggregates them with `fills_by_cloid`, and calls `plan_actions` followed by `apply_actions`.
`lifecycle.py` still imports `maker.log_rest` and `maker.log_cancel`.
The current owner handles partial fills, repricing, expiry, and exit escalation.
The removed helper instead used the first matching fill row.

The diff preserves `maker_price`, `working_orders`, `log_rest`, and `log_cancel`.
It preserves both remaining audit-state tests in `test_maker_lifecycle.py`.
It does not change `lifecycle.py`, `runner.py`, or exchange primitives.
The module description now points to the current lifecycle owner and removes the stale taker-default claim.

`cli/main.py::femr_tick` remains a registered CLI command.
Its compatibility contract explicitly preserves existing host scripts and manual usage.
`tests/test_femr_tick.py` still checks single-cycle delegation and invalid execution rejection.
Neither the wrapper nor its tests appears in the diff.

## Coverage qualification

Do not claim that every assertion in the removed fill test has an identical keeper.
That test uniquely checks that `working_orders` removes a same-client-order-ID `place` row.
The current keeper checks the `maker_orders` state and the `rest`/`place` audit sequence instead.
The old helper's return list and first-fill behavior also disappear with that unused implementation.

`working_orders` feeds `exec/router.py::execute_decisions` and retained tests.
The router has no current source caller after the same CLI retirement.
Therefore, the missing projection assertion does not identify a lost contract on the current CLI path.
It is an obsolete-path assertion, rather than an exact duplicate of current-owner proof.
The router remains outside this deletion diff and requires its own candidate assessment.

## Validation boundary

The reviewer read source, tests, packaging, CLI registration, deployment script, CI routing, and relevant Git history.
The reviewer did not run tests because the worker owned validation in this worktree.
Selected worker control logs showed failures when current lifecycle ownership and expiry were disabled.
Those logs are supporting worker evidence, not independent test execution by this reviewer.
A final read of `docs/audits/2026-10-28-maker/README.md` found consistent scope, counts, and coverage qualifications.
Its recorded production change is +4/-56, and its recorded test change is +2/-49.
The README distinguishes 342 passing tests from CLI help smoke checks and states the missing black-box E2E boundary.
The worker must report the final baseline, focused checks, full suite, lint, and CLI smoke outcomes separately.

This review does not establish live exchange behavior or the revision running on a deployed host.
It does not inspect private external Python consumers.
It does not authorize live trading, migrations, or changes to stored trading state.

## Documentation review

The reviewer applied all ten checks in `~/.agents/docs/documentation-style.md` to this report.
The report uses direct statements, consistent terms, and explicit evidence limits.
Exact identifiers, commands, hashes, and the evidence table retain their full spelling for accuracy.
