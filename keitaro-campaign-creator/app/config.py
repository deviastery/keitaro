"""Настройки приложения: переменные окружения + необязательный файл .env."""
from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    """Минимальный загрузчик .env (без внешних зависимостей).

    Уже заданные переменные окружения не перезаписываются.
    """
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv(BASE_DIR / ".env")


@dataclass(frozen=True)
class Settings:
    keitaro_url: str          # корень трекера
    keitaro_api_key: str
    keitaro_domain: str       # домен кампании, напр. in.posdk.xyz
    keitaro_group: str        # имя группы кампаний
    keitaro_source: str       # имя источника трафика
    redirect_url: str         # куда уводит первый поток (гео-поток)
    database_url: str
    offers_ttl_seconds: int   # как долго кэш офферов считается свежим
    request_timeout: float    # таймаут запросов к Keitaro, сек


@lru_cache
def get_settings() -> Settings:
    return Settings(
        keitaro_url=os.getenv("KEITARO_URL", "").strip(),
        keitaro_api_key=os.getenv("KEITARO_API_KEY", "").strip(),
        keitaro_domain=os.getenv("KEITARO_DOMAIN", "in.posdk.xyz").strip(),
        keitaro_group=os.getenv("KEITARO_GROUP", "FORTESTS").strip(),
        keitaro_source=os.getenv("KEITARO_SOURCE", "FORTESTS").strip(),
        redirect_url=os.getenv("REDIRECT_URL", "https://google.com").strip(),
        database_url=os.getenv("DATABASE_URL", f"sqlite:///{BASE_DIR / 'app.db'}").strip(),
        offers_ttl_seconds=int(os.getenv("OFFERS_TTL_SECONDS", "600")),
        request_timeout=float(os.getenv("KEITARO_TIMEOUT", "20")),
    )
