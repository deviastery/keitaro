"""Бизнес-логика редактора: синхронизация потоков с Keitaro, кэш офферов, push."""
from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session, selectinload

from .config import get_settings
from .keitaro import (
    KeitaroClient,
    KeitaroConfigError,
    KeitaroError,
    KeitaroNotFound,
    admin_campaign_url,
    build_stream_update_body,
    format_filters_summary,
)
from .models import Offer, StreamOfferRecord, StreamRecord
from .schemas import (
    CampaignStreamsResponse,
    CampaignSummaryOut,
    OfferOut,
    PushStreamIn,
    StreamOfferOut,
    StreamOut,
)
from .shares import OfferShareItem, validate_active_shares

logger = logging.getLogger("uvicorn.error")

_refresh_lock = threading.Lock()
_last_refresh: float | None = None


def get_client() -> KeitaroClient:
    s = get_settings()
    if not s.keitaro_url or not s.keitaro_api_key:
        raise KeitaroConfigError("Не заданы KEITARO_URL и KEITARO_API_KEY (см. .env.example)")
    return KeitaroClient(s.keitaro_url, s.keitaro_api_key, s.request_timeout)


# --------------------------------------------------------------------------- #
# Офферы (кэш и автокомплит)
# --------------------------------------------------------------------------- #
def refresh_offers(db: Session, client: KeitaroClient | None = None) -> int:
    global _last_refresh
    cli = client or get_client()
    with _refresh_lock:
        raw = cli.list_offers()
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
        return len(rows)


def ensure_offers_fresh(db: Session, client: KeitaroClient | None = None) -> None:
    ttl = get_settings().offers_ttl_seconds
    is_empty = (db.scalar(select(func.count()).select_from(Offer)) or 0) == 0
    is_stale = _last_refresh is None or (time.monotonic() - _last_refresh) > ttl
    if not (is_empty or is_stale):
        return
    try:
        refresh_offers(db, client=client)
    except KeitaroError:
        if is_empty:
            raise
        logger.warning("Не удалось обновить кэш офферов, используем локальные данные", exc_info=True)


def search_offers(db: Session, query: str = "", limit: int = 50) -> list[OfferOut]:
    ensure_offers_fresh(db)
    stmt = select(Offer)
    needle = query.strip().rstrip(":").strip().casefold()
    if needle:
        escaped = needle.replace("\\", "\\\\").replace("%", "\%").replace("_", "\_")
        stmt = stmt.where(Offer.search_text.like(f"%{escaped}%", escape="\\"))
    rows = list(db.scalars(stmt.order_by(Offer.name).limit(limit)))
    return [OfferOut(id=o.id, name=o.name, country=o.country, state=o.state) for o in rows]


def _offer_name_map(db: Session) -> dict[int, str]:
    rows = db.execute(select(Offer.id, Offer.name)).all()
    return {int(r[0]): str(r[1]) for r in rows}


# --------------------------------------------------------------------------- #
# Кампании
# --------------------------------------------------------------------------- #
def list_campaigns(query: str = "", limit: int = 50, client: KeitaroClient | None = None) -> list[CampaignSummaryOut]:
    cli = client or get_client()
    raw = cli.list_campaigns()
    needle = query.strip().casefold()
    out: list[CampaignSummaryOut] = []
    for item in raw:
        if item.get("state") == "deleted":
            continue
        try:
            cid = int(item["id"])
        except (KeyError, TypeError, ValueError):
            continue
        name = str(item.get("name") or f"Campaign {cid}")
        if needle and needle not in f"{cid} {name}".casefold():
            continue
        out.append(
            CampaignSummaryOut(
                id=cid,
                name=name,
                alias=str(item.get("alias") or ""),
                state=str(item.get("state") or ""),
                keitaro_admin_url=admin_campaign_url(cli.base_url, cid),
            )
        )
    out.sort(key=lambda c: c.id, reverse=True)
    return out[:limit]


# --------------------------------------------------------------------------- #
# Потоки (fetch / local read / pin / push)
# --------------------------------------------------------------------------- #
def _serialize_stream(record: StreamRecord) -> StreamOut:
    # Активные офферы сверху, удалённые (серые) — внизу
    ordered = sorted(record.offers, key=lambda o: (o.is_removed, o.sort_order, o.offer_id))
    return StreamOut(
        id=record.id,
        campaign_id=record.campaign_id,
        name=record.name,
        position=record.position,
        schema=record.schema,
        state=record.state,
        filter_summary=record.filter_summary,
        has_offers=record.has_offers,
        offers=[
            StreamOfferOut(
                offer_id=o.offer_id,
                offer_name=o.offer_name or f"Offer #{o.offer_id}",
                share=o.share,
                is_pinned=o.is_pinned,
                is_removed=o.is_removed,
            )
            for o in ordered
        ],
    )


def _sort_streams_for_ui(streams: list[StreamRecord]) -> list[StreamOut]:
    """Потоки с офферами показываем сверху, потоки без офферов — внизу страницы."""
    ordered = sorted(streams, key=lambda s: (not s.has_offers, s.position, s.id))
    return [_serialize_stream(s) for s in ordered]


def _extract_kt_offers(raw_stream: dict[str, Any]) -> tuple[list[dict[str, Any]], list[int]]:
    """Разделяет офферы из ответа Keitaro на активные и удалённые/отключённые."""
    raw_offers = raw_stream.get("offers")
    if not isinstance(raw_offers, list):
        return [], []
    active: list[dict[str, Any]] = []
    removed_ids: list[int] = []
    for item in raw_offers:
        if not isinstance(item, dict):
            continue
        try:
            oid = int(item["offer_id"])
            share = int(round(float(item.get("share", 0))))
        except (KeyError, TypeError, ValueError):
            continue
        state = str(item.get("state") or "active").lower()
        if state in ("deleted", "disabled"):
            removed_ids.append(oid)
        else:
            active.append({"offer_id": oid, "share": share})
    return active, removed_ids


def fetch_campaign_streams(
    db: Session, campaign_id: int, client: KeitaroClient | None = None
) -> CampaignStreamsResponse:
    """Подтягивает актуальные потоки из Keitaro и синхронизирует с локальной БД:
    - активные офферы обновляются из Keitaro (сохраняя локальный флаг pin);
    - офферы, удалённые в Keitaro (пропавшие из списка или пришедшие со state=deleted/disabled),
      отображаются серыми с share=0 и кнопкой Bring back.
    """
    cli = client or get_client()
    ensure_offers_fresh(db, client=cli)
    offer_names = _offer_name_map(db)

    kt_campaign = cli.get_campaign(campaign_id)
    kt_streams = cli.list_campaign_streams(campaign_id)

    existing_streams = {
        s.id: s
        for s in db.scalars(
            select(StreamRecord)
            .where(StreamRecord.campaign_id == campaign_id)
            .options(selectinload(StreamRecord.offers))
        ).all()
    }

    seen_stream_ids: set[int] = set()
    for raw_s in kt_streams:
        if raw_s.get("state") == "deleted":
            continue
        sid = int(raw_s["id"])
        seen_stream_ids.add(sid)

        kt_active_offers, kt_removed_ids = _extract_kt_offers(raw_s)
        kt_active_ids = {o["offer_id"] for o in kt_active_offers}
        schema_name = str(raw_s.get("schema") or "")

        record = existing_streams.get(sid)
        if record is None:
            record = StreamRecord(id=sid, campaign_id=campaign_id)
            db.add(record)
            existing_offers_by_id: dict[int, StreamOfferRecord] = {}
        else:
            existing_offers_by_id = {o.offer_id: o for o in record.offers}

        record.name = str(raw_s.get("name") or f"Stream #{sid}")
        record.position = int(raw_s.get("position") or 0)
        record.schema = schema_name
        record.state = str(raw_s.get("state") or "active")
        record.filter_summary = format_filters_summary(raw_s.get("filters"))
        record.raw_json = json.dumps(raw_s, ensure_ascii=False)

        record.has_offers = bool(
            kt_active_offers or kt_removed_ids or schema_name == "landings" or existing_offers_by_id
        )

        # 1. Обновляем/добавляем активные офферы из Keitaro
        for idx, ko in enumerate(kt_active_offers):
            oid = ko["offer_id"]
            name = offer_names.get(oid) or f"Offer #{oid}"
            existing = existing_offers_by_id.get(oid)
            if existing is None:
                rec_offer = StreamOfferRecord(
                    offer_id=oid,
                    offer_name=name,
                    share=ko["share"],
                    is_pinned=False,
                    is_removed=False,
                    sort_order=idx,
                )
                record.offers.append(rec_offer)
                existing_offers_by_id[oid] = rec_offer
            else:
                existing.offer_name = name
                existing.share = ko["share"]
                existing.is_removed = False
                existing.sort_order = idx

        # 2. Офферы, пришедшие из Keitaro как deleted/disabled, добавляем/обновляем как removed
        next_order = len(kt_active_offers)
        for oid in kt_removed_ids:
            if oid in kt_active_ids:
                continue
            name = offer_names.get(oid) or f"Offer #{oid}"
            existing = existing_offers_by_id.get(oid)
            if existing is None:
                rec_offer = StreamOfferRecord(
                    offer_id=oid,
                    offer_name=name,
                    share=0,
                    is_pinned=False,
                    is_removed=True,
                    sort_order=next_order,
                )
                record.offers.append(rec_offer)
                existing_offers_by_id[oid] = rec_offer
            else:
                existing.offer_name = name
                existing.is_removed = True
                existing.is_pinned = False
                existing.share = 0
                existing.sort_order = next_order
            next_order += 1

        # 3. Офферы, которые были у нас раньше, но исчезли из ответа Keitaro -> помечаем removed
        for oid, existing in existing_offers_by_id.items():
            if oid not in kt_active_ids and oid not in kt_removed_ids:
                existing.is_removed = True
                existing.is_pinned = False
                existing.share = 0
                existing.sort_order = next_order
                next_order += 1

    # Удаляем из локальной БД потоки, которых больше нет в кампании
    for sid, old_stream in existing_streams.items():
        if sid not in seen_stream_ids:
            db.delete(old_stream)

    db.commit()

    refreshed = list(
        db.scalars(
            select(StreamRecord)
            .where(StreamRecord.campaign_id == campaign_id)
            .options(selectinload(StreamRecord.offers))
        ).all()
    )

    campaign_out = CampaignSummaryOut(
        id=int(kt_campaign["id"]),
        name=str(kt_campaign.get("name") or f"Campaign {campaign_id}"),
        alias=str(kt_campaign.get("alias") or ""),
        state=str(kt_campaign.get("state") or ""),
        keitaro_admin_url=admin_campaign_url(cli.base_url, campaign_id),
    )
    return CampaignStreamsResponse(campaign=campaign_out, streams=_sort_streams_for_ui(refreshed))


def get_cached_campaign_streams(
    db: Session, campaign_id: int, client: KeitaroClient | None = None
) -> CampaignStreamsResponse:
    """Возвращает локальное состояние потоков из БД; если потоков в БД ещё нет — делает fetch."""
    existing = list(
        db.scalars(
            select(StreamRecord)
            .where(StreamRecord.campaign_id == campaign_id)
            .options(selectinload(StreamRecord.offers))
        ).all()
    )
    if not existing:
        return fetch_campaign_streams(db, campaign_id, client=client)

    cli = client or get_client()
    kt_campaign = cli.get_campaign(campaign_id)
    campaign_out = CampaignSummaryOut(
        id=int(kt_campaign["id"]),
        name=str(kt_campaign.get("name") or f"Campaign {campaign_id}"),
        alias=str(kt_campaign.get("alias") or ""),
        state=str(kt_campaign.get("state") or ""),
        keitaro_admin_url=admin_campaign_url(cli.base_url, campaign_id),
    )
    return CampaignStreamsResponse(campaign=campaign_out, streams=_sort_streams_for_ui(existing))


def update_offer_pin(db: Session, stream_id: int, offer_id: int, is_pinned: bool) -> bool:
    """Локально сохраняет состояние булавки (pin) без отправки в Keitaro."""
    row = db.scalar(
        select(StreamOfferRecord).where(
            StreamOfferRecord.stream_id == stream_id,
            StreamOfferRecord.offer_id == offer_id,
        )
    )
    if row is None or row.is_removed:
        return False
    row.is_pinned = bool(is_pinned)
    db.commit()
    return True


def push_stream_offers(
    db: Session, stream_id: int, payload: PushStreamIn, client: KeitaroClient | None = None
) -> StreamOut:
    """Отправляет изменения потока в Keitaro (PUT /streams/{id}) и сохраняет состояние в БД."""
    record = db.scalar(
        select(StreamRecord)
        .where(StreamRecord.id == stream_id)
        .options(selectinload(StreamRecord.offers))
    )
    if record is None:
        raise KeitaroNotFound(f"Поток #{stream_id} не найден в локальной базе. Сначала нажмите Fetch streams.")

    share_items = [
        OfferShareItem(
            offer_id=o.offer_id,
            share=0 if o.is_removed else o.share,
            is_pinned=False if o.is_removed else o.is_pinned,
            is_removed=o.is_removed,
        )
        for o in payload.offers
    ]
    validate_active_shares(share_items)

    active_payload = [
        {"offer_id": it.offer_id, "share": it.share}
        for it in share_items
        if not it.is_removed
    ]

    try:
        raw_stream = json.loads(record.raw_json)
    except ValueError:
        raw_stream = {"id": record.id, "campaign_id": record.campaign_id, "name": record.name}

    body = build_stream_update_body(raw_stream, active_payload)

    cli = client or get_client()
    updated_raw = cli.update_stream(stream_id, body)
    if updated_raw:
        record.raw_json = json.dumps(updated_raw, ensure_ascii=False)

    offer_names = _offer_name_map(db)
    existing_by_id = {o.offer_id: o for o in record.offers}
    incoming_ids: set[int] = set()

    for idx, item in enumerate(payload.offers):
        incoming_ids.add(item.offer_id)
        resolved_name = item.offer_name or offer_names.get(item.offer_id) or f"Offer #{item.offer_id}"
        existing = existing_by_id.get(item.offer_id)
        if existing is None:
            record.offers.append(
                StreamOfferRecord(
                    offer_id=item.offer_id,
                    offer_name=resolved_name,
                    share=0 if item.is_removed else item.share,
                    is_pinned=False if item.is_removed else item.is_pinned,
                    is_removed=item.is_removed,
                    sort_order=idx,
                )
            )
        else:
            existing.offer_name = resolved_name
            existing.share = 0 if item.is_removed else item.share
            existing.is_pinned = False if item.is_removed else item.is_pinned
            existing.is_removed = item.is_removed
            existing.sort_order = idx

    for oid, existing in list(existing_by_id.items()):
        if oid not in incoming_ids:
            record.offers.remove(existing)

    record.has_offers = True
    db.commit()
    db.refresh(record)
    return _serialize_stream(record)
