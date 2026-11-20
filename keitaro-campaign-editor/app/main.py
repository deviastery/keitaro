"""FastAPI-приложение: REST API для редактора потоков и раздача UI."""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy.orm import Session

from .database import get_db, init_db
from .keitaro import KeitaroError
from .schemas import (
    CampaignStreamsResponse,
    CampaignSummaryOut,
    OfferOut,
    PinUpdateIn,
    PushStreamIn,
    StreamOut,
)
from .services import (
    fetch_campaign_streams,
    get_cached_campaign_streams,
    list_campaigns,
    push_stream_offers,
    search_offers,
    update_offer_pin,
)

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


app = FastAPI(title="Keitaro Campaign Editor", lifespan=lifespan)


@app.exception_handler(KeitaroError)
async def keitaro_error_handler(_, exc: KeitaroError):
    return JSONResponse(status_code=exc.http_status, content={"detail": str(exc)})


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/campaigns", response_model=list[CampaignSummaryOut])
def api_list_campaigns(q: str = Query(default=""), limit: int = Query(default=50, ge=1, le=200)):
    return list_campaigns(query=q, limit=limit)


@app.get("/api/campaigns/{campaign_id}/streams", response_model=CampaignStreamsResponse)
def api_get_streams(campaign_id: int, db: Session = Depends(get_db)):
    return get_cached_campaign_streams(db, campaign_id)


@app.post("/api/campaigns/{campaign_id}/fetch", response_model=CampaignStreamsResponse)
def api_fetch_streams(campaign_id: int, db: Session = Depends(get_db)):
    return fetch_campaign_streams(db, campaign_id)


@app.patch("/api/streams/{stream_id}/offers/{offer_id}/pin")
def api_update_pin(stream_id: int, offer_id: int, body: PinUpdateIn, db: Session = Depends(get_db)):
    updated = update_offer_pin(db, stream_id, offer_id, body.is_pinned)
    return {"ok": updated, "is_pinned": body.is_pinned}


@app.post("/api/streams/{stream_id}/push", response_model=StreamOut)
def api_push_stream(stream_id: int, body: PushStreamIn, db: Session = Depends(get_db)):
    try:
        return push_stream_offers(db, stream_id, body)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/api/offers", response_model=list[OfferOut])
def api_search_offers(
    q: str = Query(default=""),
    limit: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
):
    return search_offers(db, query=q, limit=limit)
