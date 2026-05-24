"""Configuration loaded from env variables."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Settings:
    bot_token: str
    kp_api_key: str
    kp_api_base: str
    db_path: Path
    trailer_tmp_dir: Path
    max_trailer_mb: int

    @classmethod
    def from_env(cls) -> "Settings":
        bot_token = os.getenv("BOT_TOKEN", "").strip()
        kp_api_key = os.getenv("KP_API_KEY", "").strip()
        if not bot_token:
            raise RuntimeError("BOT_TOKEN not set. Create .env from .env.example")
        if not kp_api_key:
            raise RuntimeError("KP_API_KEY not set (Kinopoisk.dev / PoiskKino)")

        kp_api_base = os.getenv("KP_API_BASE", "https://api.kinopoisk.dev/v1.4").rstrip("/")

        db_path = Path(os.getenv("DB_PATH", "./data/bot.sqlite")).resolve()
        trailer_dir = Path(os.getenv("TRAILER_TMP_DIR", "./data/trailers")).resolve()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        trailer_dir.mkdir(parents=True, exist_ok=True)

        return cls(
            bot_token=bot_token,
            kp_api_key=kp_api_key,
            kp_api_base=kp_api_base,
            db_path=db_path,
            trailer_tmp_dir=trailer_dir,
            max_trailer_mb=int(os.getenv("MAX_TRAILER_MB", "48")),
        )
