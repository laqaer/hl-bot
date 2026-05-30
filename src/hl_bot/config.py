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


@dataclass(frozen=True)
class Settings:
    # Hyperliquid
    hl_address: str               # public wallet address (read-only ops only need this)
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
            hl_address=os.getenv("HL_ADDRESS", ""),
            hl_secret_key=os.getenv("HL_SECRET_KEY") or None,
            hl_api_url=os.getenv("HL_API_URL", "https://api.hyperliquid.xyz"),
            tg_bot_token=os.getenv("TG_BOT_TOKEN") or None,
            tg_chat_id=os.getenv("TG_CHAT_ID") or None,
            db_path=Path(os.getenv("HLBOT_DB", str(DB_PATH))),
            paper_mode_default=os.getenv("HLBOT_PAPER", "1") == "1",
        )
