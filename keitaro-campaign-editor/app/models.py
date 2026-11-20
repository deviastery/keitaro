"""ORM-модели локального кэша офферов, потоков и состояния офферов в потоках."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Offer(Base):
    """Кэш офферов из Keitaro для быстрого автокомплита."""

    __tablename__ = "offers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(512), nullable=False)
    country: Mapped[str] = mapped_column(String(256), default="", nullable=False)
    state: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    search_text: Mapped[str] = mapped_column(String(600), index=True, nullable=False)


class StreamRecord(Base):
    """Локальный снимок потока кампании из Keitaro."""

    __tablename__ = "streams"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    campaign_id: Mapped[int] = mapped_column(Integer, index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    position: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    schema: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    state: Mapped[str] = mapped_column(String(64), default="active", nullable=False)
    filter_summary: Mapped[str] = mapped_column(String(512), default="None", nullable=False)
    has_offers: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    raw_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)

    offers: Mapped[list["StreamOfferRecord"]] = relationship(
        back_populates="stream",
        cascade="all, delete-orphan",
        order_by="StreamOfferRecord.sort_order",
    )


class StreamOfferRecord(Base):
    """Состояние оффера внутри потока (активный / удалённый / закреплённый)."""

    __tablename__ = "stream_offers"
    __table_args__ = (UniqueConstraint("stream_id", "offer_id", name="uq_stream_offer"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    stream_id: Mapped[int] = mapped_column(ForeignKey("streams.id", ondelete="CASCADE"), index=True, nullable=False)
    offer_id: Mapped[int] = mapped_column(Integer, nullable=False)
    offer_name: Mapped[str] = mapped_column(String(512), default="", nullable=False)
    share: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    is_pinned: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_removed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    stream: Mapped[StreamRecord] = relationship(back_populates="offers")
