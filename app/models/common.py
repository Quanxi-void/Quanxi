from datetime import datetime, timezone
from typing import Any, Generic, TypeVar
from pydantic import BaseModel, Field, ConfigDict

T = TypeVar("T")


class APIError(BaseModel):
    code: str
    message: str
    details: dict[str, Any] | None = None


class ResponseMeta(BaseModel):
    request_id: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    source: str = "manhwahq.com"
    cached: bool = False
    source_url: str | None = None


class Pagination(BaseModel):
    page: int = 1
    per_page: int = 24
    total: int | None = None
    total_pages: int | None = None
    has_next: bool | None = None
    has_previous: bool | None = None


class APIResponse(BaseModel, Generic[T]):
    success: bool = True
    data: T | None = None
    meta: ResponseMeta
    pagination: Pagination | None = None
    error: APIError | None = None


class FlexibleModel(BaseModel):
    model_config = ConfigDict(extra="allow")


class ImageAsset(FlexibleModel):
    url: str | None = None
    source_path: str | None = None
    alt: str | None = None
    width: int | None = None
    height: int | None = None
    kind: str | None = None


class MangaCard(FlexibleModel):
    id: str | None = None
    slug: str
    title: str
    cover: ImageAsset | None = None
    latest_chapter: str | None = None
    updated_at: str | None = None
    rating: float | None = None
    status: str | None = None
    href: str | None = None
