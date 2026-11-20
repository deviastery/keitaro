"""Pydantic-схемы запросов и ответов API."""
from __future__ import annotations

from pydantic import BaseModel, Field


class CampaignSummaryOut(BaseModel):
    id: int
    name: str
    alias: str = ""
    state: str = ""
    keitaro_admin_url: str = ""


class OfferOut(BaseModel):
    id: int
    name: str
    country: str = ""
    state: str = ""


class StreamOfferOut(BaseModel):
    offer_id: int
    offer_name: str
    share: int
    is_pinned: bool
    is_removed: bool


class StreamOut(BaseModel):
    id: int
    campaign_id: int
    name: str
    position: int
    schema_name: str = Field(alias="schema")
    state: str
    filter_summary: str
    has_offers: bool
    offers: list[StreamOfferOut]

    model_config = {"populate_by_name": True}


class CampaignStreamsResponse(BaseModel):
    campaign: CampaignSummaryOut
    streams: list[StreamOut]


class PushOfferIn(BaseModel):
    offer_id: int = Field(..., gt=0)
    offer_name: str = ""
    share: int = Field(..., ge=0, le=100)
    is_pinned: bool = False
    is_removed: bool = False


class PushStreamIn(BaseModel):
    offers: list[PushOfferIn]


class PinUpdateIn(BaseModel):
    is_pinned: bool
