"""Configuration loaded from env variables."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

load_dotenv()


def _build_db_url() -> str:
    """If DATABASE_URL is set (e.g. Neon/Supabase) — use Postgres.
    Otherwise fall back to local SQLite at DB_PATH."""
    db_url = os.getenv("DATABASE_URL", "").strip()
    if db_url:
        # Neon/Heroku style: postgres:// → SQLAlchemy expects postgresql+asyncpg://
        if db_url.startswith("postgres://"):
            db_url = "postgresql+asyncpg://" + db_url[len("postgres://"):]
        elif db_url.startswith("postgresql://"):
            db_url = "postgresql+asyncpg://" + db_url[len("postgresql://"):]
        # Strip query string params that asyncpg doesn't recognize (sslmode, channel_binding)
        if "?" in db_url:
            db_url = db_url.split("?", 1)[0]
        return db_url
    db_path = Path(os.getenv("DB_PATH", "./data/bot.sqlite")).resolve()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return f"sqlite+aiosqlite:///{db_path}"


@dataclass(frozen=True)
class Settings:
    bot_token: str
    kp_api_key: str
    kp_api_base: str
    groq_api_key: Optional[str]
    groq_model: str
    db_url: str
    is_postgres: bool
    trailer_tmp_dir: Path
    max_trailer_mb: int

    @classmethod
    def from_env(cls) -> "Settings":
        bot_token = os.getenv("BOT_TOKEN", "").strip()
        kp_api_key = os.getenv("KP_API_KEY", "").strip()
        if not bot_token:
            raise RuntimeError("BOT_TOKEN not set")
        if not kp_api_key:
            raise RuntimeError("KP_API_KEY not set")

        kp_api_base = os.getenv("KP_API_BASE", "https://api.kinopoisk.dev/v1.4").rstrip("/")
        groq_api_key = os.getenv("GROQ_API_KEY", "").strip() or None
        groq_model = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile").strip()

        db_url = _build_db_url()
        is_pg = db_url.startswith("postgresql")

        trailer_dir = Path(os.getenv("TRAILER_TMP_DIR", "./data/trailers")).resolve()
        trailer_dir.mkdir(parents=True, exist_ok=True)

        return cls(
            bot_token=bot_token,
            kp_api_key=kp_api_key,
            kp_api_base=kp_api_base,
            groq_api_key=groq_api_key,
            groq_model=groq_model,
            db_url=db_url,
            is_postgres=is_pg,
            trailer_tmp_dir=trailer_dir,
            max_trailer_mb=int(os.getenv("MAX_TRAILER_MB", "48")),
        )
