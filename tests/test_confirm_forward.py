import time

from hl_bot.agents.base import MarketView
from hl_bot.agents.twap_mr import TwapMrAgent
from hl_bot.cli.factories import agent_config
from hl_bot.db.accrue import accrue_market_snapshot
from hl_bot.db.schema import init_db
from hl_bot.forward.confirm_forward import confirm_forward_for_agent

HOUR = 3_600_000


def _build_choppy_forward_window(conn, n: int = 50):
    """Build a forward window with the same MR signal as test_confirm.py.

    Explicit vwap/sigma are stored in the snapshot so forward reconstruction
    sees the same signal the live tick would provide.
    """
    now = int(time.time() * 1000)
    for i in range(n):
        mid = 103.0 if i % 2 else 100.0
        view = MarketView(
            ts_ms=now - (n - 1 - i) * HOUR,
            mids={"TST": mid},
            funding={"TST": 0.0},
            extra={
                "day_ntl_vlm": {"TST": 50_000_000.0},
                "candles_1h": {"TST": {"vwap": 100.0, "sigma": 1.0, "n": 60}},
            },
        )
        accrue_market_snapshot(conn, view)


def test_confirm_forward_promotes_paper_agent():
    conn = init_db(":memory:")
    _build_choppy_forward_window(conn, n=60)

    cfg, params_hash = agent_config("twap_mr_v1")
    TwapMrAgent(config=cfg, conn=conn)
    conn.execute("INSERT INTO agent_state(agent, mode, enabled) VALUES(?,?,?)", ("twap_mr_v1", "paper", 1))

    outcome = confirm_forward_for_agent(
        conn, "twap_mr_v1",
        window_days=30, min_is_trades=5, min_oos_trades=5, prefer="maker",
    )
    assert outcome.confirmed
    assert outcome.promoted
    assert outcome.params_hash == params_hash

    row = conn.execute("SELECT mode, confirmed_params_hash FROM agent_state WHERE agent=?", ("twap_mr_v1",)).fetchone()
    assert row["mode"] == "live_small"
    assert row["confirmed_params_hash"] == params_hash


def test_confirm_forward_fails_on_sample_size():
    conn = init_db(":memory:")
    # only a few frames
    _build_choppy_forward_window(conn, n=3)
    TwapMrAgent(config={}, conn=conn)
    conn.execute("INSERT INTO agent_state(agent, mode, enabled) VALUES(?,?,?)", ("twap_mr_v1", "paper", 1))

    outcome = confirm_forward_for_agent(conn, "twap_mr_v1")
    assert not outcome.confirmed
    assert "forward frames" in outcome.reasons[0]
