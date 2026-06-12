"""Prop-eval rule simulator (risk/prop.py, B-PROP) — the rules a funded
account dies on: equity-based daily loss from a day boundary, HWM-anchored
drawdown. These differ from the bot's rolling/realized guardrails, which is
the whole reason the module exists."""

from __future__ import annotations

import sqlite3

import pytest
from pytest import approx

from hl_bot.risk.prop import (
    DAY_MS,
    HOUR_MS,
    EvalProfile,
    equity_points,
    fill_trading_days,
    parse_eval_profile,
    simulate_eval,
    trading_day_index,
)

T0 = 1_750_000_000_000  # fixed epoch anchor, ms
T0_DAY_START = (T0 // DAY_MS) * DAY_MS  # midnight UTC of T0's day


def profile(**kw) -> EvalProfile:
    base = dict(
        name="test", start_balance=1000.0, max_daily_loss_pct=0.05,
        daily_loss_base="start", max_drawdown_pct=0.10,
        drawdown_mode="trailing",
    )
    base.update(kw)
    return EvalProfile(**base)


def test_clean_curve_passes_target_and_days():
    p = profile(profit_target_pct=0.08, min_trading_days=3)
    pts = [(T0 + i * HOUR_MS, 1000 + i * 7) for i in range(24)]  # → 1161
    rep = simulate_eval(p, pts, trading_days={1, 2, 3})
    assert rep.verdict == "PASS"
    assert rep.breaches == []
    # target ~1080 first cleared at i=12 (1084; i=11 is 1077)
    assert rep.target_reached_ts_ms == T0 + 12 * HOUR_MS


def test_intraday_equity_dip_breaches_even_if_day_closes_green():
    # The case the bot's realized-only 24h check cannot see: an unrealized
    # mark-to-market dip below the daily floor, recovered by day end.
    p = profile()  # daily allowance = 5% of 1000 = $50
    day = T0_DAY_START
    pts = [
        (day + 1 * HOUR_MS, 1000.0),
        (day + 2 * HOUR_MS, 949.0),   # below 950 floor — breach
        (day + 3 * HOUR_MS, 1020.0),  # closes green anyway
    ]
    rep = simulate_eval(p, pts)
    assert rep.verdict == "FAIL"
    assert [b.rule for b in rep.breaches] == ["daily_loss"]
    assert rep.breaches[0].ts_ms == day + 2 * HOUR_MS
    assert rep.breaches[0].floor == approx(950.0)


def test_same_loss_split_across_boundary_is_no_breach():
    # $80 total dip, but $40 each side of the midnight reset: each day stays
    # inside the $50 allowance.
    p = profile()
    day1, day2 = T0_DAY_START, T0_DAY_START + DAY_MS
    pts = [
        (day1 + 20 * HOUR_MS, 1000.0),
        (day1 + 23 * HOUR_MS, 960.0),  # day1 floor 950
        (day2 + 1 * HOUR_MS, 940.0),   # day2 opens at 960 → floor 910
        (day2 + 2 * HOUR_MS, 925.0),
    ]
    rep = simulate_eval(p, pts)
    assert rep.breaches == []
    # ...whereas the same path inside ONE day breaches.
    pts_one_day = [(day1 + i * HOUR_MS, eq)
                   for i, (_, eq) in enumerate(pts, start=1)]
    rep2 = simulate_eval(p, pts_one_day)
    assert [b.rule for b in rep2.breaches] == ["daily_loss"]


def test_daily_loss_base_day_open_vs_start():
    # After growth to 2000, 'start' base allows only $50 of daily dip while
    # 'day_open' allows $100.
    day1, day2 = T0_DAY_START, T0_DAY_START + DAY_MS
    pts = [
        (day1 + 1 * HOUR_MS, 2000.0),
        (day2 + 1 * HOUR_MS, 1930.0),  # -$70 from day2 open (2000)
    ]
    rep_start = simulate_eval(profile(daily_loss_base="start"), pts)
    assert [b.rule for b in rep_start.breaches] == ["daily_loss"]
    rep_open = simulate_eval(profile(daily_loss_base="day_open"), pts)
    assert rep_open.breaches == []


def test_trailing_vs_static_drawdown():
    # Run up to 1500, then dip to 1320: 12% off the HWM (trailing breach)
    # but +32% over start (static fine). Daily rule disabled via huge pct.
    day = T0_DAY_START
    pts = [
        (day + 1 * HOUR_MS, 1000.0),
        (day + 2 * HOUR_MS, 1500.0),
        (day + 3 * HOUR_MS, 1320.0),
    ]
    rep_tr = simulate_eval(profile(max_daily_loss_pct=9.9), pts)
    assert [b.rule for b in rep_tr.breaches] == ["max_drawdown"]
    assert rep_tr.breaches[0].floor == approx(1350.0)  # 1500 * 0.9
    rep_st = simulate_eval(
        profile(max_daily_loss_pct=9.9, drawdown_mode="static"), pts)
    assert rep_st.breaches == []
    assert rep_st.drawdown_floor == approx(900.0)


def test_drawdown_episodes_collapse_but_reentry_counts_again():
    day = T0_DAY_START
    pts = [
        (day + 1 * HOUR_MS, 1000.0),
        (day + 2 * HOUR_MS, 880.0),  # below 900 — episode 1
        (day + 3 * HOUR_MS, 870.0),  # still below — same episode
        (day + 4 * HOUR_MS, 950.0),  # recovered
        (day + 5 * HOUR_MS, 890.0),  # below again — episode 2
    ]
    rep = simulate_eval(profile(max_daily_loss_pct=9.9), pts)
    assert [b.rule for b in rep.breaches] == ["max_drawdown", "max_drawdown"]


def test_target_without_min_days_is_in_progress():
    p = profile(profit_target_pct=0.05, min_trading_days=5)
    pts = [(T0 + i * HOUR_MS, 1000 + i * 10) for i in range(10)]
    rep = simulate_eval(p, pts, trading_days={1, 2})
    assert rep.target_reached_ts_ms is not None
    assert rep.verdict == "IN_PROGRESS"


def test_headroom_and_density():
    day = T0_DAY_START
    pts = [(day + 1 * HOUR_MS, 1000.0), (day + 13 * HOUR_MS, 980.0)]
    rep = simulate_eval(profile(), pts)
    assert rep.daily_floor == approx(950.0)
    assert rep.daily_headroom == approx(30.0)
    assert rep.drawdown_floor == approx(900.0)
    assert rep.drawdown_headroom == approx(80.0)
    assert rep.max_gap_hours == approx(12.0)
    assert rep.obs_per_day == approx(4.0)  # 2 points over half a day


def test_empty_curve_is_no_data():
    rep = simulate_eval(profile(), [])
    assert rep.verdict == "NO_DATA"
    assert rep.n_points == 0


def test_boundary_hour_shifts_the_reset():
    # A dip at 02:00 UTC belongs to the *previous* trading day when the
    # boundary is 04:00 — so day-open is yesterday's carry, not midnight's.
    p = profile(day_boundary_utc_hour=4)
    day = T0_DAY_START
    pts = [
        (day - 2 * HOUR_MS, 1000.0),  # 22:00, day A (boundary 04:00)
        (day + 2 * HOUR_MS, 955.0),   # 02:00, still day A → floor 950, fine
        (day + 6 * HOUR_MS, 920.0),   # 06:00, day B opens at 955 → floor 905
    ]
    rep = simulate_eval(p, pts)
    assert rep.breaches == []
    assert trading_day_index(day + 2 * HOUR_MS, 4) == trading_day_index(
        day - 2 * HOUR_MS, 4)


def test_db_helpers_roundtrip():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE equity_snapshots (ts_ms INTEGER PRIMARY KEY, "
        "account_value REAL NOT NULL)")
    conn.execute("CREATE TABLE fills (time_ms INTEGER)")
    conn.executemany(
        "INSERT INTO equity_snapshots VALUES (?, ?)",
        [(T0 + 2, 1010.0), (T0 + 1, 1000.0)])
    conn.executemany(
        "INSERT INTO fills VALUES (?)",
        [(T0,), (T0 + 1,), (T0 + 2 * DAY_MS,)])
    assert equity_points(conn) == [(T0 + 1, 1000.0), (T0 + 2, 1010.0)]
    assert equity_points(conn, since_ms=T0 + 2) == [(T0 + 2, 1010.0)]
    assert len(fill_trading_days(conn)) == 2
    assert len(fill_trading_days(conn, since_ms=T0 + DAY_MS)) == 1


def test_cli_prop_check_on_scratch_db(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from hl_bot.cli.main import app

    monkeypatch.setenv("HLBOT_DB", str(tmp_path / "t.sqlite"))
    from hl_bot.config import Settings
    from hl_bot.db.schema import init_db

    conn = init_db(Settings.from_env().db_path)
    day = T0_DAY_START
    conn.executemany(
        "INSERT INTO equity_snapshots (ts_ms, account_value, total_margin, "
        "total_ntl_pos, total_raw_usd, withdrawable, raw_json) "
        "VALUES (?, ?, 0, 0, 0, 0, '{}')",
        [(day + 1 * HOUR_MS, 1000.0),
         (day + 2 * HOUR_MS, 940.0),  # breaches the 5% daily floor
         (day + 3 * HOUR_MS, 1005.0)])
    conn.commit()
    conn.close()

    res = CliRunner().invoke(app, ["prop-check"])
    assert res.exit_code == 0, res.output
    assert "daily_loss" in res.output
    assert "FAIL" in res.output

    res2 = CliRunner().invoke(app, ["prop-check", "--daily-loss-pct", "0.10"])
    assert res2.exit_code == 0, res2.output
    assert "no breaches" in res2.output


# --- B-PROP2: backtest equity-curve pre-screen ------------------------------


def test_parse_eval_profile_defaults_and_overrides():
    p = parse_eval_profile("", start_balance=1000.0)
    assert (p.start_balance, p.max_daily_loss_pct, p.daily_loss_base) == (
        1000.0, 0.05, "start")
    assert (p.max_drawdown_pct, p.drawdown_mode) == (0.10, "trailing")
    assert (p.profit_target_pct, p.min_trading_days) == (0.0, 0)

    p2 = parse_eval_profile(
        '{"max_daily_loss_pct": 0.03, "daily_loss_base": "day_open",'
        ' "max_drawdown_pct": 0.06, "drawdown_mode": "static",'
        ' "profit_target_pct": 0.08, "min_trading_days": 5,'
        ' "day_boundary_utc_hour": 4}',
        start_balance=500.0)
    assert p2.start_balance == 500.0
    assert p2.max_daily_loss_pct == approx(0.03)
    assert p2.daily_loss_base == "day_open"
    assert p2.max_drawdown_pct == approx(0.06)
    assert p2.drawdown_mode == "static"
    assert p2.profit_target_pct == approx(0.08)
    assert (p2.min_trading_days, p2.day_boundary_utc_hour) == (5, 4)
    # int where float expected is fine
    assert parse_eval_profile(
        '{"max_daily_loss_pct": 1}', start_balance=1.0).max_daily_loss_pct == 1.0


def test_parse_eval_profile_rejects_garbage():
    bad = [
        "{bad",                                # malformed JSON
        "[0.05]",                              # not an object
        '{"nope": 1}',                         # unknown key
        '{"daily_loss_base": "weekly"}',       # bad enum
        '{"drawdown_mode": "rolling"}',        # bad enum
        '{"day_boundary_utc_hour": 24}',       # out of range
        '{"max_daily_loss_pct": 0}',           # would breach a flat curve
        '{"max_drawdown_pct": -0.1}',
        '{"profit_target_pct": -0.05}',
        '{"min_trading_days": -1}',
        '{"min_trading_days": 2.5}',           # wrong type for int field
        '{"max_daily_loss_pct": true}',        # bool is not a number
        '{"daily_loss_base": 1}',              # wrong type for str field
    ]
    for spec in bad:
        with pytest.raises(ValueError):
            parse_eval_profile(spec, start_balance=1000.0)
    # start_balance is NOT a rule key — it must come from the screened curve.
    with pytest.raises(ValueError, match="starting capital"):
        parse_eval_profile('{"start_balance": 10000}', start_balance=1000.0)


def test_report_summary_lines():
    day = T0_DAY_START
    flat = [(day + i * HOUR_MS, 1000.0) for i in range(1, 4)]

    rep_fail = simulate_eval(profile(), [
        (day + 1 * HOUR_MS, 1000.0),
        (day + 2 * HOUR_MS, 940.0),   # daily floor 950
        (day + 3 * HOUR_MS, 880.0),   # trailing DD floor 900
    ])
    s = rep_fail.summary()
    assert s.startswith("FAIL — 1 daily_loss day(s) + 1 max_drawdown episode(s)")
    assert "UTC" in s and "day floor 950.00" in s  # first breach detail named

    assert simulate_eval(profile(), flat).summary() == (
        "PASS — no breaches; headroom daily $50.00 / drawdown $100.00")

    s3 = simulate_eval(
        profile(profit_target_pct=0.5, min_trading_days=3), flat,
        trading_days={1}).summary()
    assert s3.startswith("IN_PROGRESS")
    assert "target +50.0% not reached" in s3 and "trading days 1/3" in s3

    assert simulate_eval(profile(), []).summary().startswith("NO_DATA")


def _bt_frames(path: list[float]):
    """Synthetic hourly frames around vwap=100 that make twap_mr_v1 trade
    ($200 notional per entry, sigma_enter at |z|>=2)."""
    from hl_bot.backtest.engine import Frame

    return [
        Frame(
            ts_ms=T0_DAY_START + h * HOUR_MS,
            mids={"TST": mid},
            funding={"TST": 0.0},
            day_ntl_vlm={"TST": 50_000_000.0},
            candles_1h={"TST": {"vwap": 100.0, "sigma": 1.0, "n": 60}},
        )
        for h, mid in enumerate(path)
    ]


def test_cli_backtest_prop_screen(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from hl_bot.cli.main import app

    monkeypatch.setenv("HLBOT_DB", str(tmp_path / "t.sqlite"))

    # Winner arm: classic mean-reversion path, +$12ish on $1000 — no breach.
    monkeypatch.setattr(
        "hl_bot.cli.main._load_backtest_frames",
        lambda *a, **k: _bt_frames([100.0, 103.0, 100.0, 97.0, 100.0]))
    res = CliRunner().invoke(
        app, ["backtest", "--coins", "TST", "--prop-profile", "{}"])
    assert res.exit_code == 0, res.output
    assert "prop screen" in res.output
    assert "prop[taker]: PASS — no breaches" in res.output
    assert "prop[maker]: PASS — no breaches" in res.output

    # Breach arm: short at 103, mark to 135 → ~-$60 on $1000, through the
    # -5% daily floor (the realized-only guardrail's blind spot).
    monkeypatch.setattr(
        "hl_bot.cli.main._load_backtest_frames",
        lambda *a, **k: _bt_frames([100.0, 103.0, 135.0, 135.0]))
    res2 = CliRunner().invoke(
        app, ["backtest", "--coins", "TST", "--no-compare",
              "--prop-profile", '{"max_daily_loss_pct": 0.05}'])
    assert res2.exit_code == 0, res2.output  # informational — never gates
    assert "prop[taker]: FAIL" in res2.output
    assert "daily_loss" in res2.output

    # A typo'd profile is a hard error, not a silent default screen.
    res3 = CliRunner().invoke(
        app, ["backtest", "--coins", "TST", "--prop-profile", "{bad"])
    assert res3.exit_code == 1
    assert "not valid JSON" in res3.output
