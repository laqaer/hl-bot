"""Confirmation tests for spot-perp carry (S4).

S4 holds long-spot + short-perp on the SAME coin, 1:1 notional, to collect the
funding baseline market-neutral. We build synthetic frames where we control BOTH
the perp mid (mids["HYPE"]) and the spot mid (mids["HYPE-SPOT"]) plus the perp
funding, and check:
  * leg sequencing — spot leg first, perp only after the spot fill is logged,
  * funding collection on a flat-price multi-day hold,
  * market-neutrality when both legs move together,
  * exit when funding drops below the exit band,
  * basis-stop unwind when spot/perp diverge,
  * a coin with no spot mid is skipped (no legs).

Funding is folded into realized PnL via the engine's liquidate-at-end.
"""

from __future__ import annotations

from hl_bot.agents.spot_perp_carry import SpotPerpCarryAgent
from hl_bot.backtest.engine import Backtester, CostModel, Frame
from hl_bot.db.schema import init_db

HOUR = 3_600_000
# ~11% APR baseline expressed per hour: 0.11 / 8760 ≈ 0.0000126/hr.
BASELINE = 0.11 / 8760.0


def _run(frames, config=None):
    conn = init_db(":memory:")
    bt = Backtester(CostModel(maker=True), conn=conn)
    res = bt.run(SpotPerpCarryAgent(config=config or {}, conn=conn), frames)
    return res, conn


def _open_legs(conn) -> dict[str, str]:
    """Replay the audit log -> {coin: side} of currently-open legs."""
    rows = conn.execute(
        """SELECT coin, action, side FROM agent_decisions
           WHERE agent='spot_perp_carry_v1' AND action IN ('place','flatten')
           ORDER BY ts_ms ASC, rowid ASC"""
    ).fetchall()
    open_by_coin: dict[str, str] = {}
    for coin, action, side in rows:
        if action == "place":
            open_by_coin[coin] = side
        else:
            open_by_coin.pop(coin, None)
    return open_by_coin


def _flat_carry_frames(n: int):
    return [
        Frame(ts_ms=i * HOUR,
              mids={"HYPE": 30.0, "HYPE-SPOT": 30.0},
              funding={"HYPE": BASELINE},
              day_ntl_vlm={"HYPE": 50_000_000.0},
              spot_mids={"HYPE": 30.0})
        for i in range(n)
    ]


def test_leg_sequencing_spot_then_perp():
    # First qualifying decide() emits ONLY the spot leg; once that fill is in the
    # audit log, the next decide() emits the perp leg. Drive it through the
    # engine (which logs the spot place) and assert BOTH legs end up open.
    conn = init_db(":memory:")
    bt = Backtester(CostModel(maker=True), conn=conn)
    agent = SpotPerpCarryAgent(config={}, conn=conn)
    frames = _flat_carry_frames(4)

    # Frame 0: only the spot leg is placed.
    from hl_bot.backtest.engine import frozen_clock
    with frozen_clock(frames[0].ts_ms / 1000.0):
        bt._accrue_funding(frames[0])
        d0 = agent.decide(bt._view(frames[0], agent))
    placed0 = [(d.coin, d.side) for d in d0 if d.action == "place"]
    assert placed0 == [("HYPE-SPOT", "B")], placed0
    for d in d0:
        bt._apply(agent.name, d, frames[0])

    # Frame 1: spot is now in the log -> the perp leg is placed.
    with frozen_clock(frames[1].ts_ms / 1000.0):
        bt._accrue_funding(frames[1])
        d1 = agent.decide(bt._view(frames[1], agent))
    placed1 = [(d.coin, d.side) for d in d1 if d.action == "place"]
    assert placed1 == [("HYPE", "A")], placed1
    for d in d1:
        bt._apply(agent.name, d, frames[1])

    legs = _open_legs(conn)
    assert legs.get("HYPE-SPOT") == "B"
    assert legs.get("HYPE") == "A"


def test_funding_collected_over_flat_hold():
    # Flat price, persistent baseline funding -> short-perp leg accrues positive
    # funding; net_pnl > 0 after maker costs on a multi-day hold. 4 fills =
    # 2 legs in + 2 legs out at liquidate-at-end.
    res, conn = _run(_flat_carry_frames(24 * 8))  # 8 days
    assert res.net_pnl > 0, res.net_pnl
    assert res.scorecard.n_fills >= 4, res.scorecard.n_fills


def test_market_neutral_price_moves_cancel():
    # Spot and perp move TOGETHER through a large swing (+20% then back). Because
    # both legs share every price, the directional PnL cancels: net stays a small
    # carry-minus-cost number, NOT a $5+ blowup the +20% move would create if the
    # legs weren't hedged. Contrast with the same hold at a flat price.
    flat, _ = _run(_flat_carry_frames(24 * 8))
    px = 30.0
    frames = []
    for i in range(24 * 8):
        # ramp up to +20% over the first half, back down over the second.
        px *= 1.005 if i < 24 * 4 else 1 / 1.005
        frames.append(Frame(
            ts_ms=i * HOUR,
            mids={"HYPE": px, "HYPE-SPOT": px},
            funding={"HYPE": BASELINE},
            day_ntl_vlm={"HYPE": 50_000_000.0},
            spot_mids={"HYPE": px},
        ))
    res, _ = _run(frames)
    # The swing peaks ~ +28% on $25 notional ≈ $7 of one-leg exposure; if the
    # hedge failed net would be dollars off. Instead it tracks the flat-price
    # carry result within a fraction of a dollar.
    assert abs(res.net_pnl - flat.net_pnl) < 0.5, (res.net_pnl, flat.net_pnl)


def test_exit_when_funding_drops():
    # Funding drops below the exit band (3% APR) after a few days -> carry
    # stopped -> both legs flatten.
    frames = []
    for i in range(24 * 6):
        f = BASELINE if i < 24 * 3 else 0.0
        frames.append(Frame(
            ts_ms=i * HOUR,
            mids={"HYPE": 30.0, "HYPE-SPOT": 30.0},
            funding={"HYPE": f},
            day_ntl_vlm={"HYPE": 50_000_000.0},
            spot_mids={"HYPE": 30.0},
        ))
    res, conn = _run(frames, config={"exit_apr": 0.03, "lookback_h": 8.0})
    # By the last frame the position is fully unwound (no leg open before
    # liquidate-at-end). Check the audit log directly mid-run is awkward; instead
    # confirm an S4 EXIT decision was emitted.
    exits = conn.execute(
        "SELECT COUNT(*) FROM agent_decisions WHERE agent='spot_perp_carry_v1' "
        "AND action='flatten' AND reasoning LIKE '%S4 EXIT%'"
    ).fetchone()[0]
    assert exits >= 2, exits  # both legs flattened by the carry-stop rule
    assert res.scorecard.n_trades >= 1


def test_basis_stop_unwinds():
    # Spot and perp diverge past basis_stop_bps -> unwind both legs.
    frames = []
    for i in range(24 * 3):
        # After day 1, push spot 1% above perp -> 100 bps basis > 50 bps stop.
        spot = 30.0 if i < 24 else 30.3
        frames.append(Frame(
            ts_ms=i * HOUR,
            mids={"HYPE": 30.0, "HYPE-SPOT": spot},
            funding={"HYPE": BASELINE},
            day_ntl_vlm={"HYPE": 50_000_000.0},
            spot_mids={"HYPE": spot},
        ))
    _, conn = _run(frames)
    basis_exits = conn.execute(
        "SELECT COUNT(*) FROM agent_decisions WHERE agent='spot_perp_carry_v1' "
        "AND action='flatten' AND reasoning LIKE '%BASIS-STOP%'"
    ).fetchone()[0]
    assert basis_exits >= 1, basis_exits


def test_no_spot_mid_skips_coin():
    # Perp + funding qualify, but no spot mid -> no legs are emitted at all.
    frames = [
        Frame(ts_ms=i * HOUR, mids={"HYPE": 30.0},
              funding={"HYPE": BASELINE}, day_ntl_vlm={"HYPE": 50_000_000.0})
        for i in range(12)
    ]
    res, conn = _run(frames)
    placed = conn.execute(
        "SELECT COUNT(*) FROM agent_decisions WHERE agent='spot_perp_carry_v1' "
        "AND action='place'"
    ).fetchone()[0]
    assert placed == 0
    assert res.scorecard.n_trades == 0


def test_spot_only_leg_hedged_even_after_apr_slips_below_enter():
    # Regression (Codex P2): the spot leg fills at funding above enter_apr, then
    # next tick funding eases to between exit_apr and enter_apr. The perp hedge
    # MUST still be placed — never leave long-spot unhedged because the *entry*
    # signal faded. (enter_apr default 0.10, exit_apr default 0.0.)
    hi = 0.12 / 8760.0      # 12% APR — above enter
    lo = 0.05 / 8760.0      # 5% APR — below enter, above exit(0), still positive
    frames = []
    for i in range(6):
        f = hi if i == 0 else lo   # spike on the entry tick, then ease
        frames.append(Frame(
            ts_ms=i * HOUR,
            mids={"HYPE": 30.0, "HYPE-SPOT": 30.0},
            funding={"HYPE": f},
            day_ntl_vlm={"HYPE": 50_000_000.0},
            spot_mids={"HYPE": 30.0},
        ))
    conn = init_db(":memory:")
    bt = Backtester(CostModel(maker=True), conn=conn)
    bt.run(SpotPerpCarryAgent(config={"lookback_h": 1.0}, conn=conn),
           frames, liquidate_at_end=False)   # inspect open legs mid-run
    legs = _open_legs(conn)
    # Both legs open and opposite-signed = hedged/market-neutral.
    assert legs.get("HYPE-SPOT") == "B", f"spot leg missing: {legs}"
    assert legs.get("HYPE") == "A", f"perp hedge not placed (unhedged spot!): {legs}"
