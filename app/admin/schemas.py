from typing import Literal
from pydantic import BaseModel, Field, ConfigDict, field_validator

AdminRole = Literal["owner", "admin", "content_manager", "moderator"]

class UserStatusInput(BaseModel):
    is_active: bool
    reason: str | None = Field(default=None, max_length=300)

class UserRoleInput(BaseModel):
    role: AdminRole | Literal["user"]
    reason: str | None = Field(default=None, max_length=300)

class SiteSettingsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    maintenance_mode: bool = False
    registration_enabled: bool = True
    announcement_enabled: bool = False
    announcement_title: str = Field(default="", max_length=100)
    announcement_body: str = Field(default="", max_length=500)
    announcement_link: str = Field(default="", max_length=500)

    @field_validator("announcement_link")
    @classmethod
    def secure_announcement_link(cls, value: str) -> str:
        value = value.strip()
        if value and not value.startswith("https://"):
            raise ValueError("Announcement links must use HTTPS.")
        return value

class HomepageSettingsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    visible_sections: list[str] = Field(default_factory=lambda: ["hero", "trending", "latest_updates", "popular", "recommended"], max_length=10)
    section_titles: dict[str, str] = Field(default_factory=dict)
    section_order: list[str] = Field(default_factory=lambda: ["hero", "trending", "latest_updates", "popular", "recommended"], max_length=10)

class MangaOverrideInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str | None = Field(default=None, max_length=240)
    description: str | None = Field(default=None, max_length=5000)
    status: str | None = Field(default=None, max_length=80)
    manga_type: str | None = Field(default=None, max_length=40)
    genres: list[str] | None = Field(default=None, max_length=40)
    hidden: bool | None = None

class ChapterVisibilityInput(BaseModel):
    hidden: bool
    reason: str | None = Field(default=None, max_length=300)
