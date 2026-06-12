"""Centralized config: env vars + paths."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = PROJECT_ROOT / "data"
CONFIG_DIR = PROJECT_ROOT / "configs"
DB_PATH = DATA_DIR / "hlbot.sqlite"

_HEX = set("0123456789abcdefABCDEF")


def resolve_vault_address() -> str | None:
    """The Hyperliquid vault the bot trades on behalf of (CAPITAL.md Track A).

    Unset (the default) means the bot trades the personal/master account and
    nothing changes. When set, BOTH sides of the bot follow it: exchange
    actions are signed with ``vaultAddress`` so orders execute on the vault
    (``exec.orders.build_exchange``), and every account read — fills/funding/
    equity ingest, guardrail capital, open orders — targets the vault, so
    accounting ground truth matches the book being traded.

    A malformed value raises instead of falling back: the failure mode this
    guards against is the operator believing the vault is live while orders
    quietly execute on the personal account. Human-gated; see
    docs/GO_LIVE.md "Vault retargeting".
    """
    addr = (os.getenv("HL_VAULT_ADDRESS") or "").strip()
    if not addr:
        return None
    if not (addr.startswith("0x") and len(addr) == 42 and set(addr[2:]) <= _HEX):
        raise ValueError(
            f"HL_VAULT_ADDRESS malformed: {addr!r} (want 0x + 40 hex chars); "
            "refusing to fall back to the personal account"
        )
    return addr


@dataclass(frozen=True)
class Settings:
    # Hyperliquid
    hl_address: str               # account whose state is read (vault when HL_VAULT_ADDRESS set)
    hl_secret_key: str | None     # required for live trading; None = read-only
    hl_api_url: str               # mainnet or testnet
    # Telegram (optional, for reports)
    tg_bot_token: str | None
    tg_chat_id: str | None
    # Runtime
    db_path: Path
    paper_mode_default: bool      # global override; agents can opt-in to live

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            hl_address=resolve_vault_address() or os.getenv("HL_ADDRESS", ""),
            hl_secret_key=os.getenv("HL_SECRET_KEY") or None,
            hl_api_url=os.getenv("HL_API_URL", "https://api.hyperliquid.xyz"),
            tg_bot_token=os.getenv("TG_BOT_TOKEN") or None,
            tg_chat_id=os.getenv("TG_CHAT_ID") or None,
            db_path=Path(os.getenv("HLBOT_DB", str(DB_PATH))),
            paper_mode_default=os.getenv("HLBOT_PAPER", "1") == "1",
        )
