"""Live order placement adapter for Hyperliquid — PRODUCTION HARDENED.

KEY CHANGES vs initial version:
  1. Order result inspects `statuses[].filled` (real fill) vs `error` /
     `resting` (rejected or queued) — no more phantom "ok" from SDK acceptance.
  2. Retry with exponential backoff on connection / 5xx errors.
  3. Per-coin cooldown: bot will not re-place same coin within COOLDOWN_S
     of its last attempt, regardless of outcome.
  4. Position reconciliation: at tick start, sync DB ownership truth with
     live HL positions. If bot thinks it owns X but exchange shows none,
     auto-write a flatten decision to clear stale state.
  5. Telegram alerts on guardrail trips, repeated rejections, big PnL moves.
"""

from __future__ import annotations

import logging
import math
import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from eth_account import Account
from eth_account.signers.local import LocalAccount
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils import constants
from hyperliquid.utils.signing import Cloid

log = logging.getLogger(__name__)

def _resolve_trader_address() -> str:
    """The funded account the bot trades on.

    Prefer ``HL_TRADER_ADDRESS``, then ``HL_ADDRESS``, then the legacy default so
    existing deployments keep working. Set HL_TRADER_ADDRESS in /etc/hl-bot/env to
    point the bot at your own account.
    """
    return (
        os.environ.get("HL_TRADER_ADDRESS")
        or os.environ.get("HL_ADDRESS")
        or "0x5C3a67932Ca4026A6ABC18822Dc601BeD44f45a3"
    )


HL_TRADER_ADDRESS = _resolve_trader_address()
DEFAULT_API_WALLET_ENV = Path.home() / ".config" / "hermes" / "hl-bot-api-wallet.env"
COOLDOWN_S = 3600  # 1h cooldown per coin between attempts


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass
class OrderResult:
    ok: bool                        # truly filled
    status: str                     # "filled" | "rejected" | "resting" | "error"
    avg_px: float | None = None
    filled_sz: float | None = None
    oid: int | None = None
    cloid: str | None = None
    detail: dict[str, Any] | None = None
    error: str | None = None


# ---------------------------------------------------------------------------
# Key loading & exchange construction
# ---------------------------------------------------------------------------


def _load_api_key(env_path: Path | None = None) -> tuple[str, str]:
    p = env_path or DEFAULT_API_WALLET_ENV
    if not p.exists():
        raise FileNotFoundError(f"API wallet env not found at {p}")
    if p.stat().st_mode & 0o077:
        raise PermissionError(f"{p} has loose permissions; must be 600")
    env: dict[str, str] = {}
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        k, _, v = line.partition("=")
        env[k.strip()] = v.strip()
    priv = env.get("HL_BOT_API_PRIVATE_KEY", "")
    addr = env.get("HL_BOT_API_WALLET_ADDRESS", "")
    if not priv.startswith("0x") or len(priv) != 66:
        raise ValueError("HL_BOT_API_PRIVATE_KEY missing or malformed")
    if not addr.startswith("0x") or len(addr) != 42:
        raise ValueError("HL_BOT_API_WALLET_ADDRESS missing or malformed")
    return priv, addr


def build_exchange(env_path: Path | None = None) -> tuple[Exchange, Info, LocalAccount]:
    priv, expected_addr = _load_api_key(env_path)
    wallet: LocalAccount = Account.from_key(priv)
    if wallet.address.lower() != expected_addr.lower():
        raise ValueError(f"derived API wallet {wallet.address} != env {expected_addr}")
    info = Info(constants.MAINNET_API_URL, skip_ws=True)
    exchange = Exchange(
        wallet=wallet, base_url=constants.MAINNET_API_URL,
        account_address=HL_TRADER_ADDRESS,
    )
    log.info("HL exchange ready: signer=%s trader=%s", wallet.address, exchange.account_address)
    return exchange, info, wallet


# ---------------------------------------------------------------------------
# Ownership & reconciliation
# ---------------------------------------------------------------------------


def bot_owned_coins(conn: sqlite3.Connection, agent: str = "femr_v1") -> set[str]:
    """Coins bot believes it owns per its own decision audit log.

    NOTE: only counts decisions that were CONFIRMED filled. The new logger
    writes action='place' only after fill verification; rejected attempts
    write action='rejected' which is excluded here.
    """
    rows = conn.execute(
        """
        SELECT coin, action FROM agent_decisions
        WHERE agent = ? AND coin IS NOT NULL AND action IN ('place', 'flatten')
        ORDER BY ts_ms ASC
        """, (agent,),
    ).fetchall()
    owned: set[str] = set()
    for r in rows:
        coin = r["coin"] if hasattr(r, "keys") else r[0]
        action = r["action"] if hasattr(r, "keys") else r[1]
        if action == "place":
            owned.add(coin)
        elif action == "flatten":
            owned.discard(coin)
    return owned


def reconcile_positions(
    conn: sqlite3.Connection,
    live_positions: list[dict],
    agent: str = "femr_v1",
) -> list[str]:
    """Compare bot's owned-set vs live HL positions. Write synthetic
    'flatten' decisions for any coin we think we own but isn't on the
    exchange anymore. Returns list of coins reconciled.

    This protects against: (a) the user manually closed a bot position,
    (b) we logged a place but the fill never happened, (c) a liquidation.
    """
    live_coins = {p["coin"] for p in live_positions if abs(float(p.get("szi", 0) or 0)) > 0}
    owned = bot_owned_coins(conn, agent)
    stale = owned - live_coins
    if not stale:
        return []
    now_ms = int(time.time() * 1000)
    for coin in stale:
        conn.execute(
            """
            INSERT INTO agent_decisions(ts_ms, agent, action, coin, reasoning, is_paper)
            VALUES (?, ?, 'flatten', ?, ?, 0)
            """,
            (now_ms, agent, coin,
             "RECONCILE: bot owned but not present on exchange — clearing stale state"),
        )
    conn.commit()
    return list(stale)


def coin_in_cooldown(
    conn: sqlite3.Connection,
    coin: str,
    agent: str = "femr_v1",
    cooldown_s: int = COOLDOWN_S,
) -> bool:
    """True if bot attempted (placed OR rejected) this coin within cooldown window."""
    cutoff_ms = int((time.time() - cooldown_s) * 1000)
    row = conn.execute(
        """
        SELECT 1 FROM agent_decisions
        WHERE agent = ? AND coin = ?
          AND action IN ('place', 'rejected', 'flatten')
          AND ts_ms >= ?
        LIMIT 1
        """, (agent, coin, cutoff_ms),
    ).fetchone()
    return row is not None


# ---------------------------------------------------------------------------
# Guardrails
# ---------------------------------------------------------------------------


@dataclass
class GuardrailConfig:
    min_bot_capital: float = 40.0
    max_daily_loss: float = 5.0
    max_total_notional: float = 40.0
    max_concurrent_positions: int = 2
    max_per_order_notional: float = 20.0


def dynamic_daily_loss_limit(
    portfolio_value: float | None,
    floor: float = 10.0,
    pct: float = 0.03,
) -> float:
    """Daily realized-loss limit that scales with portfolio size.

    Returns a positive dollar figure: the most the bot may lose (realized,
    net of fees) across all active agents in a rolling 24h window before new
    entries are halted. Small/unknown accounts fall back to ``floor`` so a
    momentarily-empty portfolio reading can never widen the limit.
    """
    if portfolio_value is None or portfolio_value <= 0:
        return float(floor)
    return max(float(floor), float(pct) * float(portfolio_value))


def _spot_usdc(info: Info, address: str) -> float:
    try:
        st = info.post("/info", {"type": "spotClearinghouseState", "user": address}) or {}
        for b in st.get("balances", []) or []:
            if b.get("coin") == "USDC":
                return float(b.get("total", 0) or 0)
    except Exception:  # noqa: BLE001
        pass
    return 0.0


def check_guardrails(
    conn: sqlite3.Connection,
    info: Info,
    cfg: GuardrailConfig,
    agents: list[str] | None = None,
) -> tuple[bool, str]:
    state = _retry(lambda: info.user_state(HL_TRADER_ADDRESS))
    try:
        perp_val = float((state or {}).get("marginSummary", {}).get("accountValue", 0) or 0)
    except (TypeError, ValueError):
        perp_val = 0.0
    spot_usdc = _retry(lambda: _spot_usdc(info, HL_TRADER_ADDRESS))
    capital = perp_val + spot_usdc

    if capital < cfg.min_bot_capital:
        return False, f"capital ${capital:.2f} (spot ${spot_usdc:.2f} + perp ${perp_val:.2f}) < ${cfg.min_bot_capital:.2f}"

    bot_agents = agents or ["femr_v1"]
    since_ms = int((time.time() - 86400) * 1000)
    placeholders = ",".join("?" for _ in bot_agents)
    row = conn.execute(
        f"""SELECT COALESCE(SUM(closed_pnl), 0) - COALESCE(SUM(fee), 0)
           FROM fills WHERE time_ms >= ? AND agent IN ({placeholders})""",
        (since_ms, *bot_agents),
    ).fetchone()
    daily_pnl = float(row[0] or 0.0)
    if daily_pnl < -abs(cfg.max_daily_loss):
        return False, (
            f"24h bot PnL ${daily_pnl:.2f} < -${cfg.max_daily_loss:.2f} "
            f"(agents={','.join(bot_agents)})"
        )

    asset_pos = (state or {}).get("assetPositions", []) or []
    owned: set[str] = set()
    for agent in bot_agents:
        owned |= bot_owned_coins(conn, agent=agent)
    bot_ntl = sum(
        abs(float(ap.get("position", {}).get("positionValue", 0) or 0))
        for ap in asset_pos
        if ap.get("position", {}).get("coin") in owned
    )
    if bot_ntl >= cfg.max_total_notional:
        return False, f"bot open notional ${bot_ntl:.2f} >= cap ${cfg.max_total_notional:.2f}"

    return True, f"OK capital ${capital:.2f} 24h-pnl ${daily_pnl:+.2f} bot-open ${bot_ntl:.2f}"


# ---------------------------------------------------------------------------
# Retry helper for HL calls (handles "Remote end closed" + 5xx)
# ---------------------------------------------------------------------------


def _retry(fn, attempts: int = 3, base_delay: float = 1.0):
    last_exc = None
    for i in range(attempts):
        try:
            return fn()
        except (httpx.HTTPError, ConnectionError, OSError) as e:
            last_exc = e
            if i < attempts - 1:
                time.sleep(base_delay * (2 ** i))
    if last_exc:
        raise last_exc
    return None


# ---------------------------------------------------------------------------
# Order placement with strict fill verification
# ---------------------------------------------------------------------------


def _as_cloid(cloid: str | None) -> Cloid | None:
    return Cloid.from_str(cloid) if cloid else None


def _parse_response(res: dict) -> OrderResult:
    """Extract real fill state from HL response.

    Possible shapes:
      filled: {"totalSz": "...", "avgPx": "...", "oid": int, "cloid": "..."}
      error:  {"error": "reason string"}
      resting: {"oid": int, "cloid": "..."}  (limit order placed, not filled)
    """
    try:
        statuses = res.get("response", {}).get("data", {}).get("statuses", []) or []
    except (AttributeError, TypeError):
        return OrderResult(ok=False, status="error", detail=res, error="malformed response")

    if not statuses:
        return OrderResult(ok=False, status="error", detail=res, error="no statuses in response")

    s = statuses[0]
    if "filled" in s:
        f = s["filled"]
        return OrderResult(
            ok=True, status="filled",
            avg_px=float(f.get("avgPx", 0) or 0),
            filled_sz=float(f.get("totalSz", 0) or 0),
            oid=f.get("oid"),
            cloid=f.get("cloid"),
            detail=res,
        )
    if "error" in s:
        return OrderResult(ok=False, status="rejected", detail=res, error=str(s["error"]))
    if "resting" in s:
        return OrderResult(
            ok=False, status="resting",
            oid=s["resting"].get("oid"),
            cloid=s["resting"].get("cloid"),
            detail=res,
            error="order resting (not immediately filled)",
        )
    return OrderResult(ok=False, status="unknown", detail=res, error=f"unknown status: {list(s.keys())}")


_SZ_DECIMALS_CACHE: dict[str, int] = {}


def _round_order_size(exchange: Exchange, coin: str, sz: float) -> float:
    """Floor size to Hyperliquid's per-asset szDecimals.

    HL rejects otherwise-valid market orders with "Order has invalid size" when
    the float has too many decimals for the asset. Floor instead of round so we
    never exceed the agent's requested notional cap.
    """
    if coin not in _SZ_DECIMALS_CACHE:
        meta = _retry(lambda: exchange.info.meta()) or {}
        for asset in meta.get("universe", []) or []:
            name = asset.get("name")
            if name:
                _SZ_DECIMALS_CACHE[str(name)] = int(asset.get("szDecimals", 5) or 0)
    decimals = _SZ_DECIMALS_CACHE.get(coin, 5)
    factor = 10 ** decimals
    return math.floor(float(sz) * factor) / factor


def place_market_order(
    exchange: Exchange, coin: str, is_buy: bool, sz: float,
    slippage_pct: float = 0.01, cloid: str | None = None,
) -> OrderResult:
    if not cloid:
        return OrderResult(ok=False, status="error", error="SAFETY: cloid required")
    rounded_sz = _round_order_size(exchange, coin, sz)
    if rounded_sz <= 0:
        return OrderResult(ok=False, status="error", error=f"rounded size is zero: requested {sz}")
    try:
        res = _retry(lambda: exchange.market_open(
            name=coin, is_buy=is_buy, sz=rounded_sz,
            slippage=slippage_pct, cloid=_as_cloid(cloid),
        ))
    except Exception as e:  # noqa: BLE001
        log.exception("place_market_order failed")
        return OrderResult(ok=False, status="error", error=str(e))
    return _parse_response(res or {})


def close_position(exchange: Exchange, coin: str, cloid: str | None = None) -> OrderResult:
    try:
        res = _retry(lambda: exchange.market_close(coin=coin, cloid=_as_cloid(cloid)))
    except Exception as e:  # noqa: BLE001
        log.exception("close_position failed")
        return OrderResult(ok=False, status="error", error=str(e))
    return _parse_response(res or {})


# ---------------------------------------------------------------------------
# Maker (post-only) execution
# ---------------------------------------------------------------------------
#
# The book bleeds because every entry crosses the spread as a taker. Passive
# (post-only / "Alo") limit orders earn the spread instead of paying it. A
# post-only order that would cross is rejected rather than filled, so it can
# never accidentally become a taker. It also won't fill immediately — callers
# must track resting orders across ticks (see has_resting_order); the synchronous
# market path stays the default until that async handling lands.


def round_price_to(px: float, sz_decimals: int, max_decimals: int = 6) -> float:
    """Round a price to Hyperliquid's tick rules.

    HL accepts prices with at most 5 significant figures AND at most
    ``max_decimals - sz_decimals`` decimal places (max_decimals is 6 for perps,
    8 for spot); integer prices are always valid. Returns ``px`` unchanged for
    non-positive input.
    """
    if px <= 0:
        return px
    sig = 5
    if px >= 1:
        int_digits = math.floor(math.log10(px)) + 1
        dec_for_sig = max(0, sig - int_digits)
    else:
        dec_for_sig = sig + (-math.floor(math.log10(px)) - 1)
    decimals = max(0, min(dec_for_sig, max_decimals - sz_decimals))
    return round(px, decimals)


def _round_price(exchange: Exchange, coin: str, px: float) -> float:
    if coin not in _SZ_DECIMALS_CACHE:
        meta = _retry(lambda: exchange.info.meta()) or {}
        for asset in meta.get("universe", []) or []:
            name = asset.get("name")
            if name:
                _SZ_DECIMALS_CACHE[str(name)] = int(asset.get("szDecimals", 5) or 0)
    return round_price_to(px, _SZ_DECIMALS_CACHE.get(coin, 5))


def place_limit_order(
    exchange: Exchange, coin: str, is_buy: bool, sz: float, limit_px: float,
    *, post_only: bool = True, reduce_only: bool = False, cloid: str | None = None,
) -> OrderResult:
    """Place a resting limit order (post-only by default -> always maker).

    A post-only order returns status ``resting`` when it rests as a maker (the
    expected path) and ``rejected`` if it would have crossed. It does NOT fill
    immediately; track it via has_resting_order and reconcile fills on a later
    tick.
    """
    if not cloid:
        return OrderResult(ok=False, status="error", error="SAFETY: cloid required")
    rounded_sz = _round_order_size(exchange, coin, sz)
    if rounded_sz <= 0:
        return OrderResult(ok=False, status="error", error=f"rounded size is zero: requested {sz}")
    rounded_px = _round_price(exchange, coin, limit_px)
    if rounded_px <= 0:
        return OrderResult(ok=False, status="error", error=f"bad limit px: {limit_px}")
    tif = "Alo" if post_only else "Gtc"
    try:
        res = _retry(lambda: exchange.order(
            name=coin, is_buy=is_buy, sz=rounded_sz, limit_px=rounded_px,
            order_type={"limit": {"tif": tif}},
            reduce_only=reduce_only, cloid=_as_cloid(cloid),
        ))
    except Exception as e:  # noqa: BLE001
        log.exception("place_limit_order failed")
        return OrderResult(ok=False, status="error", error=str(e))
    return _parse_response(res or {})


def has_resting_order(info: Info, coin: str, address: str = HL_TRADER_ADDRESS) -> bool:
    """True if the account already has an open (resting) order on ``coin``.

    Used to avoid stacking duplicate maker orders while one is still working.
    """
    try:
        orders = _retry(lambda: info.open_orders(address)) or []
    except Exception:  # noqa: BLE001
        return False
    return any((o or {}).get("coin") == coin for o in orders)


def cancel_order(exchange: Exchange, coin: str, oid: int) -> OrderResult:
    """Cancel a resting order by oid. Best-effort; returns an OrderResult."""
    try:
        res = _retry(lambda: exchange.cancel(coin, oid))
    except Exception as e:  # noqa: BLE001
        log.exception("cancel_order failed")
        return OrderResult(ok=False, status="error", error=str(e))
    status = ((res or {}).get("response", {}).get("data", {}).get("statuses", []) or [None])[0]
    ok = status == "success"
    return OrderResult(ok=ok, status="cancelled" if ok else "error", oid=oid, detail=res)


# ---------------------------------------------------------------------------
# Telegram alerts (best-effort; failures don't break the bot)
# ---------------------------------------------------------------------------


def telegram_alert(message: str) -> None:
    """Send a Telegram message via HOME channel (set in Hermes config).
    Uses bot token from env. Silently fails if not configured.
    """
    token = os.environ.get("TG_BOT_TOKEN") or _load_tg_token()
    chat_id = os.environ.get("TG_CHAT_ID") or "8588356687"  # Guda home
    if not token:
        log.warning("no TG_BOT_TOKEN; skipping alert: %s", message[:80])
        return
    try:
        httpx.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": message, "parse_mode": "Markdown"},
            timeout=10,
        )
    except Exception as e:  # noqa: BLE001
        log.warning("telegram_alert failed: %s", e)


def _load_tg_token() -> str | None:
    """Read TG bot token from Hermes config if available."""
    p = Path.home() / ".hermes" / "config.yaml"
    if not p.exists():
        return None
    try:
        import yaml
        cfg = yaml.safe_load(p.read_text()) or {}
        tg = (cfg.get("platforms") or {}).get("telegram") or {}
        return tg.get("bot_token")
    except Exception:  # noqa: BLE001
        return None
