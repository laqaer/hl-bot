
from hl_bot.cli.factories import paper_roster


def test_paper_roster_includes_unconfirmed_agents():
    roster = paper_roster()
    assert "femr_v1" in roster
    assert "twap_mr_v1" in roster
    assert "twap_mr_regime_v1" in roster
    assert "liq_cascade_v1" in roster
    assert "basis_v1" in roster
