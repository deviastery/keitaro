"""FastAPI-приложение: API + раздача единственной HTML-страницы."""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session

from . import services
from .config import BASE_DIR, get_settings
from .database import engine, get_db
from .keitaro import KeitaroError
from .models import Base, Offer
from .schemas import CampaignCreate, CampaignOut, OfferOut

STATIC_DIR = BASE_DIR / "static"


@asynccontextmanager
async def lifespan(_: FastAPI):
    # Таблица офферов — всего лишь кэш: пересоздаём её при старте, так что
    # изменения схемы не требуют миграций, а данные подтягиваются из Keitaro заново.
    Offer.__table__.drop(engine, checkfirst=True)
    Base.metadata.create_all(engine)
    yield


app = FastAPI(title="Keitaro campaign creator", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.exception_handler(KeitaroError)
async def keitaro_error_handler(_, exc: KeitaroError):
    return JSONResponse(status_code=exc.http_status, content={"detail": str(exc)})


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/health")
def health():
    s = get_settings()
    return {
        "keitaro_configured": bool(s.keitaro_url and s.keitaro_api_key),
        "domain": s.keitaro_domain,
        "group": s.keitaro_group,
        "source": s.keitaro_source,
    }


@app.get("/api/offers", response_model=list[OfferOut])
def search_offers(
    q: str = Query("", max_length=100),
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
):
    """Автокомплит офферов по названию или ID."""
    return services.search_offers(db, q, limit)


@app.post("/api/offers/refresh")
def refresh_offers(db: Session = Depends(get_db)):
    """Принудительно перечитать офферы из Keitaro."""
    stored, received = services.refresh_offers(db)
    return {"count": stored, "received": received}


@app.post("/api/campaigns", response_model=CampaignOut, status_code=201)
def create_campaign(payload: CampaignCreate, db: Session = Depends(get_db)):
    return services.create_campaign(db, payload)


@app.get("/api/history", response_model=list[CampaignOut])
def history(limit: int = Query(10, ge=1, le=100), db: Session = Depends(get_db)):
    """Последние кампании, созданные через это приложение."""
    return services.recent_campaigns(db, limit)
