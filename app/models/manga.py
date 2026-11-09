from pydantic import Field, ConfigDict
from .common import FlexibleModel, MangaCard, ImageAsset


class MangaTitles(FlexibleModel):
    primary: str
    alternative: list[str] = Field(default_factory=list)
    native: str | None = None


class MangaMetadata(FlexibleModel):
    type: str | None = None
    status: str | None = None
    release_year: int | None = None
    authors: list[str] = Field(default_factory=list)
    artists: list[str] = Field(default_factory=list)
    genres: list[str] = Field(default_factory=list)
    rating: float | None = None
    rating_count: int | None = None


class MangaStatistics(FlexibleModel):
    views: int | None = None
    follows: int | None = None
    chapter_count: int | None = None


class ChapterSummary(FlexibleModel):
    id: str | None = None
    slug: str | None = None
    title: str
    chapter_number: float | None = None
    released_at: str | None = None
    href: str | None = None


class RecommendationCard(FlexibleModel):
    """Compact card used by the manga detail page's Related section."""
    id: str | None = None
    slug: str
    title: str
    cover: ImageAsset | None = None
    latest_chapter: str | None = None
    updated_at: str | None = None
    type: str | None = None
    href: str | None = None


class RecommendationData(FlexibleModel):
    manga_slug: str
    items: list[RecommendationCard] = Field(default_factory=list)


class MangaDetails(FlexibleModel):
    id: str | None = None
    slug: str
    url: str
    title: MangaTitles
    description: str | None = None
    images: dict[str, ImageAsset | None] = Field(default_factory=dict)
    metadata: MangaMetadata = Field(default_factory=MangaMetadata)
    statistics: MangaStatistics = Field(default_factory=MangaStatistics)
    chapters: list[ChapterSummary] = Field(default_factory=list)
    recommendations: list[RecommendationCard] = Field(default_factory=list)


class BrowseData(FlexibleModel):
    items: list[MangaCard] = Field(default_factory=list)
    available_genres: list[str] = Field(default_factory=list)
    sort_options: list[str] = Field(default_factory=list)
    applied_filters: dict = Field(default_factory=dict)


class HomeMangaCard(FlexibleModel):
    """Lean homepage card; does not expose rating or status fields."""
    model_config = ConfigDict(extra="ignore")

    id: str | None = None
    slug: str
    title: str
    cover: ImageAsset | None = None
    latest_chapter: str | None = None
    updated_at: str | None = None
    type: str | None = None
    href: str | None = None


class HomeSection(FlexibleModel):
    key: str
    title: str
    items: list[HomeMangaCard] = Field(default_factory=list)


class HomeData(FlexibleModel):
    hero: list[HomeMangaCard] = Field(default_factory=list)
    trending: list[HomeMangaCard] = Field(default_factory=list)
    latest_updates: list[HomeMangaCard] = Field(default_factory=list)
    popular: list[HomeMangaCard] = Field(default_factory=list)
    recommended: list[HomeMangaCard] = Field(default_factory=list)
    sections: list[HomeSection] = Field(default_factory=list)


class ChapterListData(FlexibleModel):
    manga_slug: str
    items: list[ChapterSummary] = Field(default_factory=list)
    total: int = 0
