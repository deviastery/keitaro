"""SQLAlchemy-модели: локальный кэш офферов и журнал созданных кампаний."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Offer(Base):
    """Кэш офферов Keitaro — нужен для быстрого автокомплита."""

    __tablename__ = "offers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)  # ID оффера в Keitaro
    name: Mapped[str] = mapped_column(String(512))
    country: Mapped[str] = mapped_column(String(255), default="")
    state: Mapped[str] = mapped_column(String(32), default="")
    search_text: Mapped[str] = mapped_column(Text, index=True)


class CampaignLog(Base):
    """Журнал кампаний, созданных через это приложение."""

    __tablename__ = "campaign_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    keitaro_id: Mapped[int] = mapped_column(Integer, index=True)
    alias: Mapped[str] = mapped_column(String(64), default="")
    name: Mapped[str] = mapped_column(String(255))
    geo: Mapped[str] = mapped_column(String(255))  # "MX,AU,RO"
    offer_id: Mapped[int] = mapped_column(Integer)
    offer_name: Mapped[str] = mapped_column(String(512), default="")
    url: Mapped[str] = mapped_column(String(512), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
