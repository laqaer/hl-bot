from hl_bot.agents.twap_mr import TwapMrAgent
from hl_bot.backtest.confirm import confirm_strategy
from hl_bot.backtest.engine import Frame
from hl_bot.backtest.persist_confirm import save_confirmation_result
from hl_bot.config_hash import hash_config
from hl_bot.db.schema import init_db

HOUR = 3_600_000
COIN = "TST"


def _choppy(n: int = 40) -> list[Frame]:
    frames = []
    closes: list[float] = []
    for i in range(n):
        mid = 103.0 if i % 2 else 100.0
        closes.append(mid)
        frames.append(Frame(
            ts_ms=i * HOUR, mids={COIN: mid}, funding={COIN: 0.0},
            day_ntl_vlm={COIN: 50_000_000.0},
            candles_1h={COIN: {"vwap": 100.0, "sigma": 1.0, "n": 60}},
            closes={COIN: list(closes)},
        ))
    return frames


def test_save_confirmation_result_writes_row():
    conn = init_db(":memory:")
    cfg = {}
    params_hash = hash_config(cfg)
    TwapMrAgent(config=cfg, conn=conn)  # registers the config
    res = confirm_strategy(
        lambda conn: TwapMrAgent(config=cfg, conn=conn),
        _choppy(40), prefer="maker", min_sharpe=0.5,
        min_is_trades=5, min_oos_trades=5,
        params_hash=params_hash,
    )
    row_id = save_confirmation_result(
        conn, res, window_start_ms=_choppy(40)[0].ts_ms,
        window_end_ms=_choppy(40)[-1].ts_ms,
    )
    assert row_id > 0
    row = conn.execute(
        "SELECT * FROM confirmation_results WHERE id=?", (row_id,)
    ).fetchone()
    assert row["agent"] == "twap_mr_v1"
    assert row["params_hash"] == params_hash
    assert row["confirmed"] == 1
    assert row["is_trades"] == res.in_sample.n_trades
