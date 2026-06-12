"""WebSocket market view — sub-second mids, L2 depth, funding/OI, live trades.

REST polling only sees mids every few minutes; the WS feed
(``wss://api.hyperliquid.xyz/ws``) gives sub-second mids, real order-book depth
(spread/imbalance for maker pricing), funding/OI via ``activeAssetCtx``, and the
live ``trades`` stream. This is the highest-ROI *free* signal upgrade
(docs/INFRA.md) and the foundation for book-aware maker pricing and a real
liquidation feed.

Design: a pure ``MarketState`` updated by ``apply_message`` (unit-tested with
synthetic HL frames), serialized to a snapshot file by a long-running ``hlbot ws``
service. The cron tick can then prefer a fresh snapshot over REST (opt-in via
``load_fresh_snapshot``), so the WS service and the tick stay decoupled and the
tick keeps a safe REST fallback. The network connect loop is a thin wrapper.
"""

from __future__ import annotations

import json
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..agents.base import MarketView


@dataclass
class MarketState:
    mids: dict[str, float] = field(default_factory=dict)
    funding: dict[str, float] = field(default_factory=dict)
    open_interest: dict[str, float] = field(default_factory=dict)
    day_ntl_vlm: dict[str, float] = field(default_factory=dict)
    book_top: dict[str, tuple[float, float]] = field(default_factory=dict)  # coin -> (bid, ask)
    trades: deque = field(default_factory=lambda: deque(maxlen=2000))
    user_fills: deque = field(default_factory=lambda: deque(maxlen=2000))
    updated_ms: int = 0

    # -- message ingest (pure) -------------------------------------------
    def apply_message(self, msg: dict[str, Any]) -> None:
        """Update state from one HL WS message. Unknown channels are ignored."""
        if not isinstance(msg, dict):
            return
        channel = msg.get("channel")
        data = msg.get("data")
        if channel == "allMids":
            mids = (data or {}).get("mids", {}) if isinstance(data, dict) else {}
            for k, v in mids.items():
                try:
                    self.mids[k] = float(v)
                except (TypeError, ValueError):
                    continue
        elif channel == "l2Book" and isinstance(data, dict):
            coin = data.get("coin")
            levels = data.get("levels") or []
            if coin and len(levels) == 2:
                bid = _lvl_px(levels[0])
                ask = _lvl_px(levels[1])
                if bid and ask:
                    self.book_top[coin] = (bid, ask)
                    self.mids[coin] = (bid + ask) / 2.0
        elif channel == "activeAssetCtx" and isinstance(data, dict):
            coin = data.get("coin")
            ctx = data.get("ctx") or {}
            if coin:
                _set_float(self.funding, coin, ctx.get("funding"))
                _set_float(self.open_interest, coin, ctx.get("openInterest"))
                _set_float(self.day_ntl_vlm, coin, ctx.get("dayNtlVlm"))
        elif channel == "userFills" and isinstance(data, dict):
            # {"isSnapshot": bool, "user": "0x..", "fills": [<raw HL fill>, ...]}.
            # We keep the raw fill dicts verbatim so they upsert through the same
            # path as REST userFills (``upsert_fill``); cloid → instant maker-fill
            # attribution without waiting for the next REST ingest.
            for f in data.get("fills") or []:
                if isinstance(f, dict) and f.get("hash") is not None and f.get("tid") is not None:
                    self.user_fills.append(f)
        elif channel == "trades" and isinstance(data, list):
            for t in data:
                try:
                    self.trades.append({
                        "coin": t.get("coin"),
                        "side": t.get("side"),
                        "px": float(t.get("px", 0) or 0),
                        "sz": float(t.get("sz", 0) or 0),
                        "ts_ms": int(t.get("time", 0) or 0),
                        "notional_usd": float(t.get("px", 0) or 0) * float(t.get("sz", 0) or 0),
                        # HL flags liquidation trades when present; best-effort.
                        "liquidation": bool(t.get("liquidation", False)),
                    })
                except (TypeError, ValueError):
                    continue
        self.updated_ms = int(time.time() * 1000)

    # -- derived views ----------------------------------------------------
    def recent_liquidations(self, window_s: int = 300) -> list[dict]:
        """Liquidation-flagged trades in the last ``window_s`` (for liq_cascade)."""
        cutoff = int(time.time() * 1000) - window_s * 1000
        return [t for t in self.trades if t.get("liquidation") and t["ts_ms"] >= cutoff]

    def recent_user_fills(self, window_s: int = 1800) -> list[dict]:
        """Our own fills in the last ``window_s`` (for instant maker-fill detect).

        Raw HL fill dicts, newest-eligible kept; ``window_s`` defaults to the
        maker max-rest horizon so a quote that fills late is still caught.
        """
        cutoff = int(time.time() * 1000) - window_s * 1000
        return [f for f in self.user_fills if int(f.get("time", 0) or 0) >= cutoff]

    def to_snapshot(self) -> dict[str, Any]:
        return {
            "updated_ms": self.updated_ms,
            "mids": self.mids,
            "funding": self.funding,
            "open_interest": self.open_interest,
            "day_ntl_vlm": self.day_ntl_vlm,
            "book_top": {k: list(v) for k, v in self.book_top.items()},
            "recent_liquidations": self.recent_liquidations(),
            "user_fills": self.recent_user_fills(),
        }

    def to_market_view(self) -> MarketView:
        return MarketView(
            ts_ms=self.updated_ms or int(time.time() * 1000),
            mids=dict(self.mids),
            funding=dict(self.funding),
            open_interest=dict(self.open_interest),
            book_top=dict(self.book_top),
            extra={
                "day_ntl_vlm": dict(self.day_ntl_vlm),
                "liquidations": self.recent_liquidations(),
            },
        )


def _lvl_px(side_levels: list) -> float | None:
    if not side_levels:
        return None
    try:
        return float(side_levels[0]["px"])
    except (TypeError, ValueError, KeyError, IndexError):
        return None


def _set_float(d: dict[str, float], k: str, v: Any) -> None:
    try:
        if v is not None:
            d[k] = float(v)
    except (TypeError, ValueError):
        pass


# ---------------------------------------------------------------------------
# Snapshot persistence + tick integration
# ---------------------------------------------------------------------------


def write_snapshot(state: MarketState, path: str | Path) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(state.to_snapshot(), separators=(",", ":")))
    tmp.replace(p)  # atomic


def load_fresh_snapshot(path: str | Path, *, max_age_s: float = 30.0) -> MarketView | None:
    """Load a WS snapshot into a MarketView if it's fresh; else None (use REST)."""
    p = Path(path)
    if not p.exists():
        return None
    try:
        snap = json.loads(p.read_text())
    except (ValueError, OSError):
        return None
    age_s = (time.time() * 1000 - snap.get("updated_ms", 0)) / 1000
    if age_s > max_age_s:
        return None
    return MarketView(
        ts_ms=int(snap.get("updated_ms", 0)),
        mids={k: float(v) for k, v in snap.get("mids", {}).items()},
        funding={k: float(v) for k, v in snap.get("funding", {}).items()},
        open_interest={k: float(v) for k, v in snap.get("open_interest", {}).items()},
        book_top={k: (float(v[0]), float(v[1])) for k, v in snap.get("book_top", {}).items()},
        extra={
            "day_ntl_vlm": {k: float(v) for k, v in snap.get("day_ntl_vlm", {}).items()},
            "liquidations": snap.get("recent_liquidations", []),
            "user_fills": snap.get("user_fills", []),
        },
    )


# ---------------------------------------------------------------------------
# Connect loop (thin; unit-tested with a fake Info)
# ---------------------------------------------------------------------------


def run_ws(
    coins: list[str],
    snapshot_path: str | Path,
    *,
    base_url: str = "https://api.hyperliquid.xyz",
    write_interval_s: float = 1.0,
    duration_s: float | None = None,
    user_address: str | None = None,
) -> None:
    """Subscribe to HL WS for ``coins`` and persist a snapshot every interval.

    Uses the hyperliquid SDK's Info subscriptions. Runs until ``duration_s``
    (None = forever). Intended to be supervised by ``hlbot-ws`` / systemd. When
    ``user_address`` is set, also subscribes to our own ``userFills`` so the tick
    can detect maker fills instantly instead of waiting for the next REST ingest.
    """
    from hyperliquid.info import Info

    state = MarketState()
    info = Info(base_url, skip_ws=False)
    try:
        info.subscribe({"type": "allMids"}, lambda m: state.apply_message(m))
        if user_address:
            info.subscribe(
                {"type": "userFills", "user": user_address}, lambda m: state.apply_message(m)
            )
        for coin in coins:
            info.subscribe({"type": "l2Book", "coin": coin}, lambda m: state.apply_message(m))
            info.subscribe({"type": "trades", "coin": coin}, lambda m: state.apply_message(m))
            info.subscribe(
                {"type": "activeAssetCtx", "coin": coin}, lambda m: state.apply_message(m)
            )

        start = time.time()
        while duration_s is None or time.time() - start < duration_s:
            time.sleep(write_interval_s)
            if state.updated_ms:
                write_snapshot(state, snapshot_path)
    finally:
        # The SDK's ws thread is non-daemon and outlives this loop; without an
        # explicit disconnect a bounded run (--seconds N) hangs forever.
        info.disconnect_websocket()
