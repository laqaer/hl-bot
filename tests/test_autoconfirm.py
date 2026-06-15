"""P1c — nightly forward auto-confirm target selection.

The loop must re-confirm exactly the agents the flywheel is waiting on: paper-
mode agents whose paper→live_small stage requires G0. It must skip agents
already promoted past paper, and agents with no require_g0 paper stage (e.g. the
moonshot new_listing soak), while honouring an explicit --agents allow-list.
"""

from __future__ import annotations

from hl_bot.cli.main import _autoconfirm_targets
from hl_bot.config import CONFIG_DIR
from hl_bot.db.schema import init_db
from hl_bot.engine.runner import _load_overrides, build_roster


def _roster():
    conn = init_db(":memory:")
    return build_roster(conn, CONFIG_DIR, _load_overrides(CONFIG_DIR))


def test_default_targets_paper_g0_agents_only():
    roster = _roster()
    names = {e.agent.name for e in _autoconfirm_targets(roster, modes={}, explicit=set())}
    # funding_crowding_fade: roster:live, mode:paper, require_g0 paper stage -> targeted
    assert "funding_crowding_fade_v1" in names
    # new_listing_reversion: roster:paper, NO ladder -> never auto-confirmed here
    assert "new_listing_reversion_v1" not in names


def test_skips_agents_already_promoted_past_paper():
    roster = _roster()
    modes = {"funding_crowding_fade_v1": "live_small"}
    names = {e.agent.name for e in _autoconfirm_targets(roster, modes, explicit=set())}
    assert "funding_crowding_fade_v1" not in names


def test_explicit_allowlist_overrides_the_rule():
    roster = _roster()
    # an explicit pick is honoured even though it has no require_g0 paper stage
    names = {e.agent.name for e in _autoconfirm_targets(
        roster, modes={}, explicit={"new_listing_reversion_v1"})}
    assert names == {"new_listing_reversion_v1"}
