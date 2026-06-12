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
    # Profile isolation (e.g. the ring-fenced moonshot sleeve): a profile gets
    # its own data dir (=> own DB, own KILL file), its own configs/<profile>/
    # contract set, and may sign with a different API wallet against a
    # different trader address (HL sub-account). Hard walls, not accounting.
    profile: str | None = None
    api_wallet_env: Path | None = None

    @property
    def configs_dir(self) -> Path:
        if self.profile and (CONFIG_DIR / self.profile).is_dir():
            return CONFIG_DIR / self.profile
        return CONFIG_DIR

    @classmethod
    def from_env(cls) -> Settings:
        profile = os.getenv("HLBOT_PROFILE") or None
        default_db = DATA_DIR / profile / "hlbot.sqlite" if profile else DB_PATH
        wallet_env = os.getenv("HL_BOT_API_WALLET_ENV")
        return cls(
            hl_address=os.getenv("HL_ADDRESS", ""),
            hl_secret_key=os.getenv("HL_SECRET_KEY") or None,
            hl_api_url=os.getenv("HL_API_URL", "https://api.hyperliquid.xyz"),
            tg_bot_token=os.getenv("TG_BOT_TOKEN") or None,
            tg_chat_id=os.getenv("TG_CHAT_ID") or None,
            db_path=Path(os.getenv("HLBOT_DB", str(default_db))),
            paper_mode_default=os.getenv("HLBOT_PAPER", "1") == "1",
            profile=profile,
            api_wallet_env=Path(wallet_env) if wallet_env else None,
        )
