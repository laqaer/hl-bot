"""Runtime harness: the tested tick pipeline shared by paper and live paths.

One function owns each slice of a tick (REVIEW M3 / B12): roster construction
(`build_roster` + `load_agent_overrides`), the view pipeline (`build_tick_view`
= fetch → enrich → WS overlay), account/risk state (`fetch_account_state`,
`apply_allocator_caps`), position truth (`positions_from_clearinghouse`,
`reconcile_agents`, `classify_position_ownership`, `synthesize_paper_positions`),
decision gathering (`gather_decisions`), and live order routing
(`execute_decisions`). The `femr_tick` CLI composes both the paper (default)
and live tick from these pieces and keeps only presentation; the old separate
paper `tick` command is retired (B12j).
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import sqlite3
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import httpx

from ..agents.base import Agent, MarketView
from ..agents.basis import BASIS_COINS
from ..agents.decisions import Decision, log_decision

log = logging.getLogger(__name__)

DEFAULT_VWAP_WINDOW = 60  # 1m bars -> 1h rolling VWAP/sigma (historical live config)
SPOT_SANITY_BAND = 0.05  # adopt a spot mid only within ±5% of the perp mid


def resolve_vwap_window(
    cli_value: int = 0,
    env: Mapping[str, str] | None = None,
    default: int = DEFAULT_VWAP_WINDOW,
) -> int:
    """Resolve the live tick's VWAP window (in 1m bars).

    Precedence: explicit CLI value (>0) > ``HLBOT_VWAP_WINDOW`` env > default.
    Anything unparseable or < 2 (rolling_vwap_sigma's floor) falls back to the
    next source so a typo in the env can never silence the signal entirely.
    """
    if cli_value >= 2:
        return cli_value
    raw = (env or {}).get("HLBOT_VWAP_WINDOW", "")
    with contextlib.suppress(TypeError, ValueError):
        v = int(raw)
        if v >= 2:
            return v
    return default


def closes_15m_bars(agents: list[Agent]) -> int:
    """Bars of 15m closes this roster needs (0 = no agent consumes the feed).

    An agent opts in by exposing ``cfg.closes_key == "closes_15m"`` (e.g. the
    breakout roster entry); the feed must carry its longest channel plus the
    in-progress bar, so the requirement is ``max(lookback, exit_lookback) + 1``.
    Returns the max across such agents so one fetch serves them all, and 0 when
    none ask — the tick then pays zero extra API calls (live mode today, where
    breakout isn't promoted into the roster).
    """
    bars = 0
    for a in agents:
        cfg = getattr(a, "cfg", None)
        if getattr(cfg, "closes_key", None) != "closes_15m":
            continue
        need = 1 + max(
            int(getattr(cfg, "lookback_bars", 0)),
            int(getattr(cfg, "exit_lookback_bars", 0)),
        )
        bars = max(bars, need)
    return bars


def load_agent_overrides(path: Path | None = None) -> dict[str, dict]:
    """Load auto-tuner per-agent config overrides (configs/agent_overrides.json).

    Every failure mode degrades to ``{}`` — i.e. the agents' built-in defaults —
    with a warning: missing file, unreadable file, malformed JSON, a top level
    that isn't a JSON object, or a per-agent value that isn't an object (those
    entries are dropped individually). A corrupt overrides file must never
    abort a tick: the defaults are the long-running tested baseline, and the
    hard risk caps (compute_notional_cap / apply_allocator_caps) clamp sizing
    downstream regardless of which config wins. Previously a non-object top
    level passed ``json.loads`` and crashed the tick at roster build.
    """
    if path is None:
        from ..config import CONFIG_DIR
        path = CONFIG_DIR / "agent_overrides.json"
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text())
    except (ValueError, OSError) as e:
        log.warning("agent overrides unreadable (%s); using built-in defaults", e)
        return {}
    if not isinstance(raw, dict):
        log.warning(
            "agent overrides top level is %s, not an object; using built-in defaults",
            type(raw).__name__,
        )
        return {}
    out: dict[str, dict] = {}
    for name, cfg in raw.items():
        if cfg is None:
            continue
        if not isinstance(cfg, dict):
            log.warning(
                "agent override for %s is %s, not an object; entry ignored",
                name, type(cfg).__name__,
            )
            continue
        out[name] = cfg
    return out


def build_roster(
    conn: sqlite3.Connection,
    overrides: Mapping[str, dict] | None = None,
) -> list[Agent]:
    """The canonical tick roster: every wired agent with its default config.

    One tested function owns which agents exist and what they run with
    (REVIEW M3) — paper ticks evaluate this full roster; the live path narrows
    it via :func:`filter_live_agents`. ``overrides`` (see
    :func:`load_agent_overrides`) merge over each agent's defaults by name;
    unknown names are ignored.
    """
    from .basis import BasisAgent
    from .breakout import BreakoutAgent
    from .femr import FemrAgent
    from .liq_cascade import LiqCascadeAgent
    from .twap_mr import TwapMrAgent
    from .twap_mr_regime import TwapMrRegimeAgent

    ov = overrides or {}

    def cfg(agent_name: str, defaults: dict) -> dict:
        merged = dict(defaults)
        merged.update(ov.get(agent_name) or {})
        return merged

    return [
        FemrAgent(config=cfg("femr_v1", {
            "max_notional_per_trade": 20.0,
            "max_total_notional": 40.0,
            "funding_enter_per_hr": 0.00015,
            "funding_exit_per_hr": 0.00005,
        }), conn=conn),
        TwapMrAgent(config=cfg("twap_mr_v1", {}), conn=conn),
        TwapMrRegimeAgent(config=cfg("twap_mr_regime_v1", {}), conn=conn),
        LiqCascadeAgent(config=cfg("liq_cascade_v1", {}), conn=conn),
        BasisAgent(config=cfg("basis_v1", {}), conn=conn),
        # B-EDGE2a: the G0-validated 15m-bar Donchian config (96h channel,
        # 24h exit). Paper-only until promoted in agent_state — the live
        # filter drops it, which also zeroes the 15m feed in live mode.
        BreakoutAgent(config=cfg("breakout_v1", {
            "lookback_bars": 384,
            "exit_lookback_bars": 96,
            "closes_key": "closes_15m",
            "max_notional_per_trade": 20.0,
            "max_total_notional": 60.0,
        }), conn=conn),
        # B-EDGE2e: same channel with the trend-quality (efficiency-ratio)
        # entry gate ON — the config that turned the combined 20-coin G0
        # FAIL into a PASS. Paper A/B arm beside the unfiltered breakout_v1.
        BreakoutAgent(name="breakout_er_v1", config=cfg("breakout_er_v1", {
            "lookback_bars": 384,
            "exit_lookback_bars": 96,
            "min_efficiency_ratio": 0.1,
            "er_lookback_bars": 96,
            "closes_key": "closes_15m",
            "max_notional_per_trade": 20.0,
            "max_total_notional": 60.0,
        }), conn=conn),
    ]


def filter_live_agents(
    conn: sqlite3.Connection, agents: list[Agent],
) -> tuple[list[Agent], dict[str, str]]:
    """Return agents allowed to place live orders plus skipped reasons.

    Paper/default state is safe: an agent must be explicitly enabled and in
    live_small/live mode before it enters the live execution roster.
    """
    rows = conn.execute("SELECT agent, mode, enabled FROM agent_state").fetchall()
    state = {r["agent"]: (r["mode"], int(r["enabled"])) for r in rows}
    live_agents = []
    skipped: dict[str, str] = {}
    for agent in agents:
        mode, enabled = state.get(agent.name, ("paper", 1))
        if enabled == 1 and mode in ("live_small", "live"):
            live_agents.append(agent)
        else:
            skipped[agent.name] = f"mode={mode} enabled={enabled}"
    return live_agents, skipped


def fetch_market_view(base_url: str, coins: list[str]) -> MarketView:
    """Fetch mids + 1h funding + 24h volume for all coins via /info.

    NOTE: returns ALL coins from the universe, not just the requested ones,
    because FEMR needs to scan the whole universe for funding extremes.
    The `coins` parameter is kept for backward compatibility but ignored.
    """
    with httpx.Client(timeout=15) as client:
        mids_raw = client.post(base_url + "/info", json={"type": "allMids"}).json() or {}
        meta_ctx = client.post(base_url + "/info", json={"type": "metaAndAssetCtxs"}).json()
    mids: dict[str, float] = {}
    for k, v in mids_raw.items():
        with contextlib.suppress(TypeError, ValueError):
            mids[k] = float(v)
    funding: dict[str, float] = {}
    open_interest: dict[str, float] = {}
    day_ntl_vlm: dict[str, float] = {}
    if isinstance(meta_ctx, list) and len(meta_ctx) == 2:
        universe = meta_ctx[0].get("universe", [])
        ctxs = meta_ctx[1]
        for u, c in zip(universe, ctxs, strict=False):
            name = u.get("name")
            if not name:
                continue
            try:
                funding[name] = float(c.get("funding", 0))
                open_interest[name] = float(c.get("openInterest", 0))
                day_ntl_vlm[name] = float(c.get("dayNtlVlm", 0))
            except (TypeError, ValueError):
                pass
    return MarketView(
        ts_ms=int(time.time() * 1000),
        mids=mids,
        funding=funding,
        open_interest=open_interest,
        extra={"day_ntl_vlm": day_ntl_vlm},
    )


def normalize_spot_mids(payload: object, perp_mids: dict[str, float],
                        band: float = SPOT_SANITY_BAND) -> dict[str, float]:
    """Extract BTC/ETH/SOL spot mids from a raw ``spotMetaAndAssetCtxs`` payload.

    REVIEW M5 hardening. The previous inline version had two silent-wrongness
    bugs, both pinned by tests here:

    * It zipped ``meta.universe`` with the ctx array positionally, but the two
      are NOT aligned (live API 2026-06-12: 305 universe rows vs 590 ctxs —
      delisted pairs leave holes), so UBTC/USDC was read off another pair's
      ctx. Ctxs join on their ``coin`` field == the universe row's ``name``.
    * It scaled midPx by ``10**(base_weiDecimals - quote_weiDecimals)``, but
      midPx is already quoted in USDC per token (live: @142 midPx 63668.5 vs
      perp 63682.5, a −2bps basis), so the scaling mangled a correct price.
      No scaling is applied.

    A candidate is adopted only within ``band`` of the perp mid — the sanity
    bound the old code documented as 5% but enforced at ±50% (the basis agent
    enters at 0.2% divergence, so a mis-parsed mid inside a loose band becomes
    max-size phantom entries). Wrapped (U-prefixed) pairs are preferred over
    plain-named ones; malformed payloads degrade to ``{}`` (spot is an
    enrichment, never tick-fatal).
    """
    out: dict[str, float] = {}
    if not (isinstance(payload, list) and len(payload) == 2):
        return out
    meta = payload[0] if isinstance(payload[0], dict) else {}
    ctxs = payload[1] if isinstance(payload[1], list) else []
    tokens = meta.get("tokens")
    universe = meta.get("universe")
    tokens = tokens if isinstance(tokens, list) else []
    universe = universe if isinstance(universe, list) else []
    name_by_token = {t.get("index"): t.get("name") for t in tokens if isinstance(t, dict)}
    ctx_by_pair = {c.get("coin"): c for c in ctxs if isinstance(c, dict)}
    for u in universe:
        if not isinstance(u, dict):
            continue
        pair_tokens = u.get("tokens")
        if not isinstance(pair_tokens, list) or len(pair_tokens) < 2:
            continue
        base_name = name_by_token.get(pair_tokens[0])
        if name_by_token.get(pair_tokens[1]) != "USDC":
            continue
        wrapped = (isinstance(base_name, str) and base_name.startswith("U")
                   and base_name[1:] in BASIS_COINS)
        coin = base_name[1:] if wrapped else base_name
        if coin not in BASIS_COINS:
            continue
        ctx = ctx_by_pair.get(u.get("name"))
        if not isinstance(ctx, dict):
            continue
        try:
            mid = float(ctx.get("midPx") or 0)
        except (TypeError, ValueError):
            continue
        perp_mid = perp_mids.get(coin)
        if mid <= 0 or not perp_mid or perp_mid <= 0:
            continue
        if not (1 - band) < mid / perp_mid < (1 + band):
            continue
        if wrapped or coin not in out:
            out[coin] = mid
    return out


def enrich_view(view: MarketView, api_url: str, vol: dict[str, float],
                vwap_window: int = DEFAULT_VWAP_WINDOW,
                closes_15m_bars: int = 0) -> None:
    """Augment a MarketView with rolling VWAP/σ (top-vol coins), spot mids, liquidations.

    ``vwap_window`` is the number of 1m candles in the rolling window (60 = the
    historical 1h live config). VWAP/σ math is the backtester's
    ``rolling_vwap_sigma`` so live and backtest agree bar-for-bar (B-WIN2);
    the ``candles_1h`` key name is kept for agent compatibility.

    ``closes_15m_bars`` > 0 additionally fetches that many 15m candles per top
    coin into ``view.extra["closes_15m"]`` (current in-progress bar last, like
    backtest frames) for channel agents whose horizon outruns the 1m window
    (B-EDGE2a: breakout's 96h channel = 385×15m bars, one API call per coin,
    well under the ~5000-row cap). 0 = skip, no extra API traffic.
    """
    from ..backtest.data import closes_vols, rolling_vwap_sigma

    # ---- top-20-by-volume universe ----
    top = sorted(vol.items(), key=lambda kv: kv[1], reverse=True)[:20]
    top_coins = [c for c, _ in top]

    candles_1h: dict[str, dict] = {}
    closes_by_coin: dict[str, list[float]] = {}
    closes_15m_by_coin: dict[str, list[float]] = {}
    spot_mids: dict[str, float] = {}

    with httpx.Client(timeout=15) as cli:
        # vwap_window × 1m candles -> vwap & sigma per top coin
        end_ms = int(time.time() * 1000)
        start_ms = end_ms - vwap_window * 60_000
        for coin in top_coins:
            try:
                cs = cli.post(api_url + "/info", json={
                    "type": "candleSnapshot",
                    "req": {"coin": coin, "interval": "1m",
                            "startTime": start_ms, "endTime": end_ms},
                }).json() or []
                if not isinstance(cs, list) or not cs:
                    continue
                pxs, vols, _ts = closes_vols(cs)
                vwap, sigma = rolling_vwap_sigma(pxs, vols, vwap_window)
                if vwap is None or sigma is None:
                    continue
                candles_1h[coin] = {"vwap": vwap, "sigma": sigma, "n": len(pxs)}
                closes_by_coin[coin] = pxs[-vwap_window:]
            except Exception:  # noqa: BLE001
                continue

        # 15m closes feed for long-horizon channel agents (per-coin error
        # isolation like the 1m loop; a short history is fine — channel_break
        # just won't fire until enough bars exist).
        if closes_15m_bars > 0:
            start_15m = end_ms - closes_15m_bars * 900_000
            for coin in top_coins:
                try:
                    cs = cli.post(api_url + "/info", json={
                        "type": "candleSnapshot",
                        "req": {"coin": coin, "interval": "15m",
                                "startTime": start_15m, "endTime": end_ms},
                    }).json() or []
                    if not isinstance(cs, list) or not cs:
                        continue
                    pxs, _vols, _ts = closes_vols(cs)
                    closes_15m_by_coin[coin] = pxs[-closes_15m_bars:]
                except Exception:  # noqa: BLE001
                    continue

        # Spot mids for BTC/ETH/SOL (basis agent input). All parsing, the
        # by-name ctx join, and the ±5%-of-perp sanity band live in
        # normalize_spot_mids (REVIEW M5); a fetch failure degrades to {}.
        try:
            spot = cli.post(api_url + "/info", json={"type": "spotMetaAndAssetCtxs"}).json()
            spot_mids = normalize_spot_mids(spot, view.mids)
        except Exception:  # noqa: BLE001
            pass

    # No liquidation source over REST: HL exposes no `{"type":"liquidations"}`
    # info endpoint (the old call was a phantom that always returned nothing).
    # The real feed is the WS `trades` liquidation flag, overlaid by
    # build_tick_view when a fresh snapshot exists. `liquidations_feed=False`
    # tells liq_cascade it has no real feed yet, so it keeps entries disabled
    # (REVIEW C6 / B11).
    view.extra["candles_1h"] = candles_1h
    view.extra["closes"] = closes_by_coin
    view.extra["closes_15m"] = closes_15m_by_coin
    view.extra["spot_mids"] = spot_mids
    view.extra["liquidations"] = []
    view.extra["liquidations_feed"] = False


@dataclass
class TickView:
    """A fully built tick MarketView plus the resolved feed parameters.

    ``vwap_window`` / ``bars_15m`` are returned so the CLI summary can print
    what was actually fetched; ``ws`` is the overlay result (None when no
    ``HLBOT_WS_SNAPSHOT`` is configured, ``applied=False`` when the snapshot
    file is missing or stale — REST stays the source of truth either way).
    """

    view: MarketView
    vwap_window: int
    bars_15m: int
    ws: WsOverlay | None


def build_tick_view(
    base_url: str,
    agents: list[Agent],
    *,
    vwap_window: int = 0,
    env: Mapping[str, str] | None = None,
) -> TickView:
    """Build the one tick MarketView both paper and live paths decide on.

    The single view pipeline (REVIEW M3 / B12): REST universe fetch
    (:func:`fetch_market_view`) → VWAP/σ + spot + 15m-feed enrichment
    (:func:`enrich_view`, window resolved CLI > ``HLBOT_VWAP_WINDOW`` env >
    default, 15m bars sized by what the roster consumes) → optional fresh-WS
    overlay (:func:`overlay_ws_snapshot`, opt-in via ``HLBOT_WS_SNAPSHOT``),
    which is also the only real liquidations feed. ``env`` defaults to
    ``os.environ``; tests inject a plain dict.
    """
    if env is None:
        env = os.environ
    view = fetch_market_view(base_url, [])
    w = resolve_vwap_window(vwap_window, env)
    bars_15m = closes_15m_bars(agents)
    enrich_view(view, base_url, view.extra.get("day_ntl_vlm", {}),
                vwap_window=w, closes_15m_bars=bars_15m)
    ws = None
    ws_path = env.get("HLBOT_WS_SNAPSHOT")
    if ws_path:
        from ..ingest.ws import load_fresh_snapshot
        snap = load_fresh_snapshot(ws_path, max_age_s=30.0)
        ws = overlay_ws_snapshot(view, snap)
    return TickView(view=view, vwap_window=w, bars_15m=bars_15m, ws=ws)


@dataclass
class AccountState:
    """Exchange-truth account snapshot for one tick (two HL ``/info`` calls).

    ``clearinghouse`` / ``spot_clearinghouse`` are the raw HL payloads — the
    position parse (:func:`positions_from_clearinghouse`) and guardrails read
    them downstream. The floats are derived once here so risk sizing and the
    CLI summary share one tested parse instead of re-reading the dicts.
    """

    clearinghouse: dict
    spot_clearinghouse: dict
    account_value: float  # perp marginSummary.accountValue
    spot_usdc: float  # USDC balance on spot
    portfolio_value: float  # unified perp + spot USDC (live risk-sizing input)
    withdrawable: float


def fetch_account_state(base_url: str, address: str) -> AccountState:
    """Fetch HL clearinghouse truth for ``address`` and derive sizing values.

    Extracted verbatim from the ``femr_tick`` preamble (REVIEW M3 / B12).
    Failure semantics are deliberate and preserved: the perp
    ``clearinghouseState`` call is the tick's ground truth, so an HTTP failure
    there propagates — a tick must never size risk blind. The spot call
    degrades to ``{}`` on HTTP errors (spot USDC then counts as 0), which only
    *shrinks* portfolio value and therefore the notional caps — a spot outage
    tightens risk rather than aborting the tick.
    """
    from ..risk.scaling import (
        perp_account_value_from_state,
        spot_usdc_from_state,
        unified_portfolio_value,
    )

    with httpx.Client(timeout=10) as cli:
        st = cli.post(
            base_url + "/info",
            json={"type": "clearinghouseState", "user": address},
        ).json() or {}
        try:
            spot_st = cli.post(
                base_url + "/info",
                json={"type": "spotClearinghouseState", "user": address},
            ).json() or {}
        except httpx.HTTPError:
            spot_st = {}
    try:
        withdrawable = float(st.get("withdrawable", 0) or 0)
    except (TypeError, ValueError):
        withdrawable = 0.0
    return AccountState(
        clearinghouse=st,
        spot_clearinghouse=spot_st,
        account_value=perp_account_value_from_state(st),
        spot_usdc=spot_usdc_from_state(spot_st),
        portfolio_value=unified_portfolio_value(st, spot_st),
        withdrawable=withdrawable,
    )


@dataclass
class WsOverlay:
    """Result of overlaying a WS snapshot onto the live REST view.

    ``applied`` is False when no fresh snapshot was available (REST stays the
    source of truth). ``n_mids`` / ``n_liqs`` are returned so the CLI can print a
    one-line summary without re-reading the snapshot.
    """

    applied: bool
    n_mids: int
    n_liqs: int


def overlay_ws_snapshot(view: MarketView, snap: MarketView | None) -> WsOverlay:
    """Merge a fresh WS snapshot onto the live REST ``view`` in place.

    Purely additive: sub-second mids/funding, the L2 ``book_top``, and a REAL
    liquidations feed for liq_cascade. REST stays the fallback — when ``snap`` is
    None nothing is touched. Extracted from the ``femr_tick`` preamble (REVIEW
    M3 / B12) so the overlay is importable and unit-tested without filesystem IO;
    the CLI keeps the env-read + ``load_fresh_snapshot`` and the console print.

    A non-None snapshot IS a real liquidation feed (it comes from the WS trades
    flag), even when no liquidations occurred this window — empty means a calm
    market, not a broken feed — so ``liquidations_feed`` is set True and
    liq_cascade entries are enabled.
    """
    if snap is None:
        return WsOverlay(applied=False, n_mids=0, n_liqs=0)
    view.mids.update(snap.mids)
    view.funding.update(snap.funding)
    if snap.book_top:
        view.book_top.update(snap.book_top)
    liqs = snap.extra.get("liquidations") or []
    view.extra["liquidations"] = liqs
    view.extra["liquidations_feed"] = True
    # Carry our own WS-captured fills through so the maker reconcile path can
    # detect a just-filled quote this tick (deduped against REST by (hash,tid)).
    view.extra["user_fills"] = snap.extra.get("user_fills") or []
    return WsOverlay(applied=True, n_mids=len(snap.mids), n_liqs=len(liqs))


def positions_from_clearinghouse(st: dict) -> list[dict]:
    """Normalize HL ``clearinghouseState`` into the bot's position-dict shape.

    Pure parse of ``st["assetPositions"][].position`` into the list of dicts the
    rest of the live path consumes (reconcile, allocator, view enrichment).
    Previously inlined and untested in ``femr_tick``; extracted as the first pure
    slice of the shared live/paper tick harness (REVIEW M3 / B12). Malformed
    entries are skipped rather than aborting the tick.
    """
    out: list[dict] = []
    for ap in st.get("assetPositions", []) or []:
        pos = (ap.get("position") or {}) if isinstance(ap, dict) else {}
        with contextlib.suppress(TypeError, ValueError):
            out.append({
                "coin": pos.get("coin"),
                "szi": float(pos.get("szi", 0) or 0),
                "entry_px": float(pos.get("entryPx", 0) or 0),
                "position_value": float(pos.get("positionValue", 0) or 0),
                "unrealized_pnl": float(pos.get("unrealizedPnl", 0) or 0),
                "liquidation_px": float(pos.get("liquidationPx", 0) or 0),
                "leverage": (pos.get("leverage") or {}).get("value"),
                "margin_used": float(pos.get("marginUsed", 0) or 0),
            })
    return out


def synthesize_paper_positions(
    conn: sqlite3.Connection,
    agent: str,
    mids: Mapping[str, float],
) -> list[dict]:
    """Replay an agent's paper book into the clearinghouse position-dict shape.

    femr evaluates exits only on ``view.extra["live_positions"]`` ("adopt"
    semantics), and a paper position has no exchange counterpart — so a paper
    femr position could never exit; it just held a capacity slot forever
    (B-PAPER2). Paper ticks pass this synthesized view instead, the same way
    the backtest engine synthesizes ``live_positions`` from its own book
    (``Backtester._view``), so one exit path runs in all three modes.

    Replay semantics match the agents' own book replays: a ``place`` opens (a
    re-place on a held coin overwrites), a ``flatten`` always closes. Rows that
    could never have filled (missing side/sz/px) are skipped like
    ``replay_paper_fills`` does — a zero entry px would also divide by zero in
    femr's return math. Approximations, mirroring the backtest view:
    ``position_value``/``unrealized_pnl`` are marked at the current mid (entry
    px fallback when the mid is missing), ``liquidation_px`` is 0.0 (femr skips
    liq-proximity checks at <= 0), and no funding accrues.
    """
    rows = conn.execute(
        """
        SELECT coin, action, side, sz, px FROM agent_decisions
        WHERE agent = ? AND coin IS NOT NULL AND action IN ('place', 'flatten')
          AND is_paper = 1
        ORDER BY ts_ms ASC
        """,
        (agent,),
    ).fetchall()
    book: dict[str, dict] = {}
    for r in rows:
        coin = r["coin"]
        if r["action"] == "flatten":
            book.pop(coin, None)
            continue
        side, sz, px = r["side"], float(r["sz"] or 0), float(r["px"] or 0)
        if side not in ("B", "A") or sz <= 0 or px <= 0:
            continue
        book[coin] = {"side": side, "sz": sz, "entry_px": px}
    out: list[dict] = []
    for coin, pos in book.items():
        signed = pos["sz"] if pos["side"] == "B" else -pos["sz"]
        mid = float(mids.get(coin) or 0) or pos["entry_px"]
        out.append({
            "coin": coin,
            "szi": signed,
            "entry_px": pos["entry_px"],
            "position_value": abs(pos["sz"] * mid),
            "unrealized_pnl": (mid - pos["entry_px"]) * signed,
            "liquidation_px": 0.0,
            "leverage": None,
            "margin_used": 0.0,
        })
    return out


def reconcile_agents(
    conn: sqlite3.Connection,
    all_positions: list[dict],
    agent_names: list[str],
) -> dict[str, list[str]]:
    """Clear stale DB ownership for each agent independently against HL truth.

    Runs ``reconcile_positions`` per agent (each agent owns coins by name match,
    so reconciling them together would cross-contaminate) and returns only the
    agents that had something reconciled. Extracted from the ``femr_tick``
    preamble as part of the shared tick harness (REVIEW M3 / B12).
    """
    from ..exec.orders import reconcile_positions

    reconciled: dict[str, list[str]] = {}
    for name in agent_names:
        r = reconcile_positions(conn, all_positions, agent=name)
        if r:
            reconciled[name] = r
    return reconciled


@dataclass
class PositionOwnership:
    """Bot-vs-manual classification of the live positions for one tick.

    ``owned_by_agent`` maps each agent to the coins it owns per its CONFIRMED
    decision log (via :func:`bot_owned_coins`); ``owned_all`` is their union;
    ``manual_coins`` is every live position NOT owned by any agent in the roster
    (i.e. opened by hand or by a filtered-out agent). ``manual_coins`` preserves
    ``all_positions`` order so the CLI's display is unchanged.
    """

    owned_by_agent: dict[str, set[str]]
    owned_all: set[str]
    manual_coins: list[str]


def classify_position_ownership(
    conn: sqlite3.Connection,
    all_positions: list[dict],
    agent_names: list[str],
    *,
    paper: bool = False,
) -> PositionOwnership:
    """Split live HL positions into bot-owned (per agent) vs manual.

    Extracted from the ``femr_tick`` preamble (REVIEW M3 / B12) so the live
    classification that drives the bot-owned/manual display — and tells the bot
    which coins it must NOT touch (manual) — is importable and unit-tested with
    an in-memory DB instead of an inlined loop. Behavior is preserved exactly:
    ownership keys off each agent's CONFIRMED place/flatten decision log, and a
    coin owned by an agent that was filtered out of ``agent_names`` (e.g. a
    not-promoted live agent) correctly falls into ``manual_coins``.

    ``paper`` selects which decision book defines ownership (matching the tick
    mode). The default is the live book, so paper rows can never reclassify a
    manual position as bot-owned — losing the don't-touch protection.
    """
    from ..exec.orders import bot_owned_coins

    owned_by_agent = {
        name: bot_owned_coins(conn, agent=name, paper=paper) for name in agent_names
    }
    owned_all: set[str] = set()
    for coins in owned_by_agent.values():
        owned_all |= coins
    manual_coins = [p["coin"] for p in all_positions if p["coin"] not in owned_all]
    return PositionOwnership(
        owned_by_agent=owned_by_agent,
        owned_all=owned_all,
        manual_coins=manual_coins,
    )


@dataclass
class AllocatorCaps:
    """Result of resolving + applying per-agent notional caps for one tick.

    ``allocs`` is the raw MetaAllocator split; ``effective_caps`` /
    ``effective_order_caps`` are the binding total / per-trade numbers actually
    written onto each agent's ``cfg`` (the function mutates the agents in place,
    preserving the prior inlined behavior). Returned so the CLI can print them.
    """

    allocs: dict[str, float]
    effective_caps: dict[str, float]
    effective_order_caps: dict[str, float]


def apply_allocator_caps(
    conn: sqlite3.Connection,
    agents: list[Agent],
    risk_cap,
) -> AllocatorCaps:
    """Allocate the 7d-performance split, resolve the layered risk rule, and
    write the binding caps onto each agent's ``cfg``.

    Extracted verbatim from the ``femr_tick`` preamble (REVIEW M3 / B12) so the
    live cap-application path is importable and unit-tested with fake agents
    instead of buried in the CLI. The layered rule is unchanged: the
    MetaAllocator suggests a split bounded by the 5x-total / 1x-per-position
    live caps, ``resolve_agent_caps`` applies "explicit configured cap wins,
    legacy blanket ceilings replaced by the dynamic 1x cap, configured per-trade
    sizes never raised", and the result is written onto each agent before its
    turn. Agents without a ``cfg`` keep their raw alloc and are left untouched.
    """
    from ..agents.meta_allocator import MetaAllocator, MetaAllocatorConfig
    from ..risk.allocation import resolve_agent_caps

    allocator = MetaAllocator(
        [a.name for a in agents],
        MetaAllocatorConfig(
            total_capital=risk_cap.max_total_notional,
            max_alloc=risk_cap.max_per_position_notional,
        ),
    )
    allocs = allocator.allocate(conn)
    configured_caps_in = {
        a.name: {
            "max_total_notional": float(getattr(a.cfg, "max_total_notional", float("inf"))),
            "max_notional_per_trade": float(getattr(a.cfg, "max_notional_per_trade", float("inf"))),
        }
        for a in agents if hasattr(a, "cfg")
    }
    resolved = resolve_agent_caps(allocs, risk_cap, configured_caps_in)
    effective_caps: dict[str, float] = {}
    effective_order_caps: dict[str, float] = {}
    for a in agents:
        cap = resolved.get(a.name)
        if cap is None:
            effective_caps[a.name] = allocs.get(a.name, 0.0)
            continue
        effective_caps[a.name] = cap.max_total_notional
        if hasattr(a, "cfg") and hasattr(a.cfg, "max_total_notional"):
            a.cfg.max_total_notional = cap.max_total_notional
            if hasattr(a.cfg, "max_notional_per_trade"):
                a.cfg.max_notional_per_trade = cap.max_notional_per_trade
                effective_order_caps[a.name] = cap.max_notional_per_trade
    return AllocatorCaps(
        allocs=allocs,
        effective_caps=effective_caps,
        effective_order_caps=effective_order_caps,
    )


def _agent_mode(conn: sqlite3.Connection, agent: str) -> tuple[str, bool]:
    row = conn.execute(
        "SELECT mode, enabled FROM agent_state WHERE agent=?", (agent,)
    ).fetchone()
    if not row:
        return "paper", True
    return row["mode"], bool(row["enabled"])


def gather_decisions(
    conn: sqlite3.Connection,
    agents: list[Agent],
    view: MarketView,
    *,
    is_paper: bool,
    defer_exec_logging: bool = False,
    log_holds: bool = True,
    honor_enabled: bool = True,
) -> list[Decision]:
    """Ask each agent to ``decide()``, isolating failures, and log per policy.

    The single decision-gathering path for every tick — ``femr_tick`` runs it
    for both paper (default) and live modes — so one tested function owns what
    gets logged and when (REVIEW M3 — the previously separate paper/live paths
    had diverged and only the paper one isolated agent crashes; the vestigial
    paper ``tick`` command is retired, B12j).

    Every returned decision has ``is_paper`` set to ``is_paper``. A ``decide()``
    that raises is caught, recorded as an ``error`` row, and skipped — one broken
    agent can no longer abort the whole tick, so risk-reducing flattens from
    healthy agents still run on the live path (this isolation was previously
    missing from ``femr_tick``).

    Logging policy:
    - ``honor_enabled``: skip agents marked ``enabled=0`` in ``agent_state``.
    - ``log_holds``: when False, ``hold`` rows are returned but not logged (noise).
    - ``defer_exec_logging``: when True (the live path), ``place``/``flatten`` are
      returned but NOT logged here — they're logged only after the exchange
      confirms, with the real fill px/sz (see :func:`execute_decisions`), so the
      cooldown check never sees our own intent rows.

    Each agent's ``paper_book`` flag is set to ``is_paper`` before ``decide()``
    so its position replay reads the book this tick will write: paper ticks see
    paper rows, live ticks see live rows, and the two books never mix even when
    they share one DB.
    """
    out: list[Decision] = []
    for agent in agents:
        if honor_enabled and not _agent_mode(conn, agent.name)[1]:
            log.info("agent %s disabled, skipping", agent.name)
            continue
        agent.paper_book = is_paper
        try:
            decisions = agent.decide(view)
        except Exception as e:  # noqa: BLE001
            log_decision(conn, Decision(
                agent=agent.name, action="error",
                reasoning="decide() raised", error=str(e),
                is_paper=True,
            ))
            log.exception("agent %s decide() failed", agent.name)
            continue
        for d in decisions:
            d.is_paper = is_paper
            defer = d.action == "hold" and not log_holds
            defer = defer or (defer_exec_logging and d.action in ("place", "flatten"))
            if not defer:
                log_decision(conn, d)
            out.append(d)
    return out


def record_tick_heartbeat(
    conn: sqlite3.Connection,
    *,
    mode: str,
    agents: int,
    decisions: int,
    now_ms: int | None = None,
) -> None:
    """Mark a tick loop as having run to completion (liveness ground truth).

    ``assess_health`` keys its "is the bot alive?" check on these rows.
    Decision rows cannot carry that check: ticks run with ``log_holds=False``,
    so ``agent_decisions`` grows only when an order/error happens and a healthy
    but quiet book is indistinguishable from a dead loop. Written at the END of
    a tick (after the execution loop on the live path), so a tick that aborts
    mid-way does not beat — which is the point of a dead-man switch.
    """
    conn.execute(
        "INSERT INTO tick_heartbeats(ts_ms, mode, agents, decisions) VALUES(?,?,?,?)",
        (now_ms or int(time.time() * 1000), mode, agents, decisions),
    )
    conn.commit()


@dataclass
class ExecEvent:
    """One outcome of routing a decision to the exchange (place/flatten).

    ``kind`` is the machine-readable outcome (asserted in tests); ``message`` is
    the human/console string (kept here so the CLI stays a thin printer and the
    live execution path is unit-testable with a fake exchange).
    """

    kind: str  # skip|resting|filled_maker|filled|reject|closed|close_failed
    agent: str
    coin: str
    message: str


def execute_decisions(
    conn: sqlite3.Connection,
    exchange,
    view: MarketView,
    decisions: list[Decision],
    *,
    agent_names: set[str],
    guardrails_ok: bool,
    execution: str = "taker",
) -> list[ExecEvent]:
    """Route place/flatten decisions to the exchange. Pure of presentation.

    This is the single live order-placement loop (previously inlined in
    ``cli.femr_tick``). Behavior is preserved exactly:

    - ``place`` is blocked when guardrails fail or the coin is in cooldown.
    - In ``maker`` mode, a coin with a resting quote is left alone; otherwise a
      post-only limit is placed at the near touch (book-aware, never crossing).
    - In ``taker`` mode, a market order is placed.
    - ``place``/``flatten`` are logged ONLY after exchange acceptance, with the
      REAL fill px/sz, so cooldown checks don't see our own intent rows and
      downstream stops/TPs key off truth.

    Returns an ordered list of :class:`ExecEvent` for the caller to display.
    """
    from ..exec.orders import (
        close_position,
        coin_in_cooldown,
        maker_limit_price,
        place_limit_order,
        place_market_order,
    )

    maker = execution == "maker"
    if maker:
        from ..exec.maker import log_rest, working_orders

    events: list[ExecEvent] = []
    for d in decisions:
        if d.agent not in agent_names or d.coin is None:
            continue

        if d.action == "place" and d.sz and d.side:
            if not guardrails_ok:
                events.append(ExecEvent(
                    "skip", d.agent, d.coin,
                    f"[dim]SKIP {d.agent} {d.coin}: guardrail blocks new entries[/dim]"))
                continue
            if coin_in_cooldown(conn, d.coin, agent=d.agent):
                events.append(ExecEvent(
                    "skip", d.agent, d.coin,
                    f"[dim]SKIP {d.agent} {d.coin}: in cooldown[/dim]"))
                continue
            is_buy = (d.side == "B")
            if maker:
                # Already have a working quote on this coin? leave it.
                if d.coin in working_orders(conn, d.agent):
                    events.append(ExecEvent(
                        "skip", d.agent, d.coin,
                        f"[dim]SKIP {d.agent} {d.coin}: maker quote already resting[/dim]"))
                    continue
                bt = (view.book_top or {}).get(d.coin)
                # Passive fallback when no fresh L2 book: step ~5bps inside from the
                # (possibly stale) mid so a post-only order rests instead of crossing.
                passive = (d.px or 0.0) * (0.9995 if is_buy else 1.0005)
                limit_px = maker_limit_price(
                    bt[0] if bt else None, bt[1] if bt else None, is_buy, passive)
                res = place_limit_order(exchange, d.coin, is_buy, d.sz, limit_px,
                                        post_only=True, cloid=d.cloid)
                if res.status == "resting":
                    events.append(ExecEvent(
                        "resting", d.agent, d.coin,
                        f"[cyan]RESTING[/cyan] {d.coin} {'BUY' if is_buy else 'SELL'} "
                        f"{d.sz} @ ${limit_px} oid={res.oid}"))
                    log_rest(conn, d.agent, d.coin, d.side, d.sz, limit_px, d.cloid, res.oid)
                elif res.ok:  # filled immediately (rare for post-only)
                    if res.avg_px:
                        d.px = res.avg_px
                    log_decision(conn, d)
                    events.append(ExecEvent(
                        "filled_maker", d.agent, d.coin,
                        f"[bold green]FILLED(maker)[/bold green] {d.coin} @ ${res.avg_px}"))
                else:
                    events.append(ExecEvent(
                        "reject", d.agent, d.coin,
                        f"[red]MAKER REJECT[/red] {d.coin}: {res.status} — {res.error}"))
                conn.commit()
                continue
            res = place_market_order(exchange, d.coin, is_buy, d.sz,
                                     slippage_pct=0.01, cloid=d.cloid)
            if res.ok:
                # Log place ONLY after fill confirmed, with the REAL fill px/sz
                # (not the pre-trade mid) so downstream stops/TPs key off truth.
                if res.avg_px:
                    d.px = res.avg_px
                if res.filled_sz:
                    d.sz = res.filled_sz
                log_decision(conn, d)
                events.append(ExecEvent(
                    "filled", d.agent, d.coin,
                    f"[bold green]FILLED[/bold green] {d.coin} {'BUY' if is_buy else 'SELL'} "
                    f"{res.filled_sz} @ ${res.avg_px}"))
            else:
                log_decision(conn, Decision(
                    agent=d.agent, action="rejected", coin=d.coin,
                    reasoning=f"HL rejected: {res.error}", is_paper=False,
                ))
                conn.commit()
                events.append(ExecEvent(
                    "reject", d.agent, d.coin,
                    f"[red]REJECT[/red] {d.coin}: {res.status} — {res.error}"))

        elif d.action == "flatten":
            res = close_position(exchange, d.coin, cloid=d.cloid)
            if res.ok:
                # Log the flatten immediately so ownership clears this tick rather
                # than waiting for next-tick reconciliation. Record the real exit px.
                if res.avg_px:
                    d.px = res.avg_px
                log_decision(conn, d)
                conn.commit()
                events.append(ExecEvent(
                    "closed", d.agent, d.coin,
                    f"[bold]CLOSED[/bold] {d.coin} @ ${res.avg_px}"))
            else:
                events.append(ExecEvent(
                    "close_failed", d.agent, d.coin,
                    f"[red]CLOSE FAILED[/red] {d.coin}: {res.error}"))

    return events
