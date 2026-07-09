from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Config:
    token: str
    main_admins: frozenset[str]
    log_chat_id: int | None
    database_path: Path
    whitelist_csv: Path
    max_active_tasks: int


def load_config() -> Config:
    token = os.getenv("BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("BOT_TOKEN не задан. Скопируйте .env.example в .env.")
    admins = frozenset(
        value.strip().lstrip("@").lower()
        for value in os.getenv("MAIN_ADMIN_USERNAMES", "").split(",")
        if value.strip()
    )
    raw_chat = os.getenv("STAFF_LOG_CHAT_ID", "").strip()
    return Config(
        token=token,
        main_admins=admins,
        log_chat_id=int(raw_chat) if raw_chat else None,
        database_path=Path(os.getenv("DATABASE_PATH", "data/bank.sqlite3")),
        whitelist_csv=Path(os.getenv("WHITELIST_CSV", "data/members.csv")),
        max_active_tasks=max(1, int(os.getenv("MAX_ACTIVE_TASKS", "2"))),
    )

