"""Pydantic-схемы запросов и ответов."""
from __future__ import annotations

from datetime import datetime, timezone

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .geo import parse_geo


class CampaignCreate(BaseModel):
    name: str
    geo: list[str]
    offer_id: int = Field(gt=0)

    @field_validator("name")
    @classmethod
    def _check_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Укажите название кампании")
        if len(value) > 255:
            raise ValueError("Название слишком длинное (максимум 255 символов)")
        return value

    @field_validator("geo", mode="before")
    @classmethod
    def _check_geo(cls, value):
        return parse_geo(value, strict=True)


class OfferOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    country: str = ""
    state: str = ""


class CampaignOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    keitaro_id: int
    alias: str
    name: str
    geo: str
    offer_id: int
    offer_name: str
    url: str
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        # SQLite возвращает «наивное» время — помечаем его как UTC, чтобы браузер показал верно
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value
