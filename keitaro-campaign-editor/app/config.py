"""Конфигурация приложения из .env и переменных окружения."""
from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


_load_dotenv(BASE_DIR / ".env")


@dataclass(frozen=True)
class Settings:
    keitaro_url: str
    keitaro_api_key: str
    database_url: str
    request_timeout: float
    offers_ttl_seconds: int


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings(
        keitaro_url=os.getenv("KEITARO_URL", "").strip(),
        keitaro_api_key=os.getenv("KEITARO_API_KEY", "").strip(),
        database_url=os.getenv("DATABASE_URL", "sqlite:///./app.db").strip(),
        request_timeout=float(os.getenv("REQUEST_TIMEOUT", "20")),
        offers_ttl_seconds=int(os.getenv("OFFERS_TTL_SECONDS", "300")),
    )
