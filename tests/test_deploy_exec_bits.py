"""Pin the exec bit on every shell script systemd or an operator runs directly.

deploy/update.sh shipped as git mode 100644 from birth, so the auto-deploy unit
failed 203/EXEC on every box and the live deployment silently froze at its
install-day commit (found Iter 79). Both clones run with core.fileMode=false,
which makes a workdir chmod invisible to `git add -A` — the index mode is the
only mode a fresh checkout gets, so that is what this test checks (falling back
to the workdir bit where git is unavailable, e.g. an sdist).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# Every *.sh tracked under deploy/ is a direct entry point (systemd ExecStart or
# documented operator command); ralph/loop.sh is ExecStart'd by hlbot-loop.service.
EXPECTED_EXECUTABLE = sorted(
    [str(p.relative_to(REPO_ROOT)) for p in (REPO_ROOT / "deploy").glob("**/*.sh")]
    + ["ralph/loop.sh"]
)


def _index_modes() -> dict[str, str] | None:
    """{path: git index mode} for the pinned scripts, or None if git can't tell us."""
    try:
        out = subprocess.run(
            ["git", "ls-files", "-s", "--", *EXPECTED_EXECUTABLE],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    modes = {}
    for line in out.splitlines():
        meta, path = line.split("\t", 1)
        modes[path] = meta.split()[0]
    return modes or None


def test_entry_point_scripts_are_executable():
    assert EXPECTED_EXECUTABLE, "glob found no deploy scripts — repo layout changed?"
    modes = _index_modes()
    if modes is not None:
        not_exec = {p: m for p, m in modes.items() if m != "100755"}
        assert not not_exec, (
            f"git-tracked mode must be 100755 (fix: git update-index --chmod=+x <path>; "
            f"plain chmod is invisible under core.fileMode=false): {not_exec}"
        )
        missing = set(EXPECTED_EXECUTABLE) - set(modes)
        assert not missing, f"scripts not tracked by git: {missing}"
    else:
        not_exec = [p for p in EXPECTED_EXECUTABLE if not os.access(REPO_ROOT / p, os.X_OK)]
        assert not not_exec, f"scripts missing the exec bit: {not_exec}"
