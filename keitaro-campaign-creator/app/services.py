"""Бизнес-логика: кэш офферов, поиск, создание кампании."""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone

from sqlalchemy import delete, func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from .config import get_settings
from .keitaro import KeitaroClient, KeitaroConfigError, KeitaroError
from .models import CampaignLog, Offer
from .schemas import CampaignCreate, CampaignOut

logger = logging.getLogger("uvicorn.error")  # чтобы сообщения были видны в консоли uvicorn

_refresh_lock = threading.Lock()
_last_refresh: float | None = None  # time.monotonic() последнего успешного обновления кэша


def get_client() -> KeitaroClient:
    s = get_settings()
    if not s.keitaro_url or not s.keitaro_api_key:
        raise KeitaroConfigError("Не заданы KEITARO_URL и KEITARO_API_KEY (см. .env.example)")
    return KeitaroClient(s.keitaro_url, s.keitaro_api_key, s.request_timeout)


# --------------------------------------------------------------------------- #
# Офферы
# --------------------------------------------------------------------------- #
def refresh_offers(db: Session) -> tuple[int, int]:
    """Полностью перезаписывает кэш офферов данными из Keitaro.
    Возвращает (сколько сохранено, сколько пришло из Keitaro)."""
    global _last_refresh
    with _refresh_lock:
        raw = get_client().list_offers()

        rows: dict[int, Offer] = {}
        for item in raw:
            if item.get("state") == "deleted":
                continue
            try:
                offer_id = int(item["id"])
            except (KeyError, TypeError, ValueError):
                continue
            name = str(item.get("name") or f"Offer {offer_id}")
            country = item.get("country")
            rows[offer_id] = Offer(
                id=offer_id,
                name=name,
                country=",".join(str(c) for c in country) if isinstance(country, list) else str(country or ""),
                state=str(item.get("state") or ""),
                search_text=f"{offer_id} {name}".casefold(),
            )

        db.execute(delete(Offer))
        db.add_all(rows.values())
        db.commit()
        _last_refresh = time.monotonic()
        logger.info("Офферы обновлены: получено из Keitaro %d, сохранено %d", len(raw), len(rows))
        return len(rows), len(raw)


def ensure_offers_fresh(db: Session) -> None:
    """Обновляет кэш, если он пуст или устарел. Если Keitaro недоступен, а кэш
    есть, спокойно отдаём старые данные."""
    ttl = get_settings().offers_ttl_seconds
    is_empty = (db.scalar(select(func.count()).select_from(Offer)) or 0) == 0
    is_stale = _last_refresh is None or (time.monotonic() - _last_refresh) > ttl
    if not (is_empty or is_stale):
        return
    try:
        refresh_offers(db)
    except KeitaroError:
        if is_empty:
            raise
        logger.warning("Не удалось обновить офферы, используем кэш", exc_info=True)


def search_offers(db: Session, query: str = "", limit: int = 50) -> list[Offer]:
    """Офферы для автокомплита: подстрока названия или ID, регистр не важен.
    Гео кампании на список офферов не влияет."""
    ensure_offers_fresh(db)
    stmt = select(Offer)

    needle = query.strip().rstrip(":").strip().casefold()
    if needle:
        escaped = needle.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        stmt = stmt.where(Offer.search_text.like(f"%{escaped}%", escape="\\"))

    return list(db.scalars(stmt.order_by(Offer.name).limit(limit)))


# --------------------------------------------------------------------------- #
# Кампании
# --------------------------------------------------------------------------- #
def create_campaign(db: Session, data: CampaignCreate) -> CampaignOut:
    s = get_settings()
    result = get_client().provision_campaign(
        name=data.name,
        geo=data.geo,
        offer_id=data.offer_id,
        domain=s.keitaro_domain,
        group=s.keitaro_group,
        source=s.keitaro_source,
        redirect_url=s.redirect_url,
    )

    out = CampaignOut(
        keitaro_id=result["id"],
        alias=result["alias"],
        name=data.name,
        geo=",".join(data.geo),
        offer_id=data.offer_id,
        offer_name=result["offer_name"],
        url=result["url"],
        created_at=datetime.now(timezone.utc),
    )

    # Кампания в Keitaro уже создана — сбой локального журнала не должен
    # превращать успех в ошибку.
    try:
        db.add(CampaignLog(**out.model_dump()))
        db.commit()
    except SQLAlchemyError:
        db.rollback()
        logger.exception("Кампания %s создана, но не записана в локальный журнал", out.keitaro_id)
    return out


def recent_campaigns(db: Session, limit: int = 10) -> list[CampaignLog]:
    return list(db.scalars(select(CampaignLog).order_by(CampaignLog.id.desc()).limit(limit)))
