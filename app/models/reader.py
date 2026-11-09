from pydantic import Field
from .common import FlexibleModel, ImageAsset


class ReaderPage(FlexibleModel):
    index: int
    page_number: int
    image: ImageAsset
    loading: str | None = None


class ReaderData(FlexibleModel):
    manga_slug: str
    manga_title: str | None = None
    chapter_title: str
    chapter_number: float | None = None
    chapter_url: str
    pages: list[ReaderPage] = Field(default_factory=list)
    page_count: int = 0
    navigation: dict[str, str | None] = Field(default_factory=dict)
    published_at: str | None = None
