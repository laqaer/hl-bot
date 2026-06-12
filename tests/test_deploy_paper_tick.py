"""Pin the paper forward-test loop's safety invariants (Iter 85).

The live tick filters out unpromoted agents, so G1 paper evidence accumulates
ONLY where paper ticks run — and for the live box's first days nothing ran
them (the deploy DB held zero paper history; every candidate's 30d calendar
clock was stopped). hlbot-paper-tick.{service,timer} + run-paper-tick.sh close
that hole. These tests pin the properties that make the loop safe to run
unattended beside a live trader:

- it writes a DEDICATED DB (never the live one, even if /etc/hl-bot/env sets
  HLBOT_DB),
- it can never go live (no --live, and it must not consume HLBOT_TICK_ARGS,
  which carries the live box's "--live --execution maker"),
- the units are actually wired (install.sh enables the timer; update.sh's
  hlbot-*.timer self-enable glob reaches it on existing boxes).
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEPLOY = REPO_ROOT / "deploy"

SCRIPT = (DEPLOY / "run-paper-tick.sh").read_text()
SERVICE = (DEPLOY / "systemd" / "hlbot-paper-tick.service").read_text()
TIMER = (DEPLOY / "systemd" / "hlbot-paper-tick.timer").read_text()


def test_paper_tick_uses_dedicated_db():
    # The export must name a paper-specific file, not the live default.
    m = re.search(r'export HLBOT_DB="\$\{HLBOT_PAPER_DB:-([^}]+)\}"', SCRIPT)
    assert m, "run-paper-tick.sh must export HLBOT_DB (paper DB isolation)"
    assert m.group(1) != "data/hlbot.sqlite"
    assert "paper" in m.group(1)
    # The export happens before any hlbot invocation.
    assert SCRIPT.index("export HLBOT_DB") < SCRIPT.index("uv run hlbot")


def test_paper_tick_can_never_go_live():
    # Judge invocations, not comments (the comments explain these very rules).
    code_lines = [
        line for line in SCRIPT.splitlines() if not line.lstrip().startswith("#")
    ]
    run_lines = [line for line in code_lines if line.startswith("run ")]
    assert run_lines, "run-paper-tick.sh lost its run invocations?"
    assert not any("--live" in line for line in run_lines)
    assert not any("HLBOT_TICK_ARGS" in line for line in code_lines)
    # No ingest: live-account fills stay out of the paper evidence stream.
    assert not any("hlbot ingest" in line for line in run_lines)


def test_paper_tick_runs_tick_then_supervisor():
    # femr_tick builds the book; supervisor writes the G1 audit trail beside it.
    assert re.search(r"^run uv run hlbot femr_tick$", SCRIPT, re.MULTILINE)
    assert re.search(r"^run uv run hlbot supervisor$", SCRIPT, re.MULTILINE)


def test_paper_tick_service_execs_the_script_via_bash():
    # bash-wrapped ExecStart (B-DEPLOY-EXEC): a lost exec bit can't kill the loop.
    assert "ExecStart=/usr/bin/bash /opt/hl-bot/deploy/run-paper-tick.sh" in SERVICE
    assert "EnvironmentFile=/etc/hl-bot/env" in SERVICE


def test_paper_tick_timer_is_wired():
    assert "OnUnitActiveSec=" in TIMER
    assert "WantedBy=timers.target" in TIMER
    # install.sh must enable it for fresh boxes...
    install = (DEPLOY / "install.sh").read_text()
    assert "hlbot-paper-tick.timer" in install
    # ...and update.sh's self-enable glob must reach it on existing boxes.
    update = (DEPLOY / "update.sh").read_text()
    assert 'deploy/systemd/hlbot-*.timer' in update
