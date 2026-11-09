from datetime import datetime
from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator
import re

class RegisterInput(BaseModel):
    email: EmailStr
    username: str = Field(min_length=3, max_length=24)
    display_name: str = Field(min_length=1, max_length=40)
    password: str = Field(min_length=8, max_length=128)

    @field_validator("username")
    @classmethod
    def valid_username(cls, value: str) -> str:
        value = value.strip().lower()
        if not re.fullmatch(r"[a-z0-9_]{3,24}", value):
            raise ValueError("Username may contain only lowercase letters, numbers, and underscores.")
        return value

    @field_validator("display_name")
    @classmethod
    def clean_display_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Display name cannot be empty.")
        return value

class LoginInput(BaseModel):
    email_or_username: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=128)

class RefreshInput(BaseModel):
    refresh_token: str

class ProfileUpdateInput(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=40)
    bio: str | None = Field(default=None, max_length=300)
    is_public: bool | None = None

class UsernameChangeInput(BaseModel):
    new_username: str = Field(min_length=3, max_length=24)
    current_password: str

    @field_validator("new_username")
    @classmethod
    def valid_new_username(cls, value: str) -> str:
        value = value.strip().lower()
        if not re.fullmatch(r"[a-z0-9_]{3,24}", value):
            raise ValueError("Username may contain only lowercase letters, numbers, and underscores.")
        return value

class EmailChangeInput(BaseModel):
    new_email: EmailStr
    current_password: str

class PasswordChangeInput(BaseModel):
    current_password: str
    new_password: str = Field(min_length=8, max_length=128)

class AvatarURLInput(BaseModel):
    url: str = Field(min_length=8, max_length=2048)

class AvatarPresetInput(BaseModel):
    avatar_id: str = Field(min_length=1, max_length=80)

class PublicProfile(BaseModel):
    id: str
    username: str
    display_name: str
    bio: str | None = None
    avatar: dict
    joined_at: datetime
    is_public: bool = True

class UserProfile(PublicProfile):
    email: EmailStr
    last_activity_at: datetime | None = None
    last_read_at: datetime | None = None

class AccountSummary(BaseModel):
    bookmarked: int = 0
    total_chapters_read: int = 0
    last_read_at: datetime | None = None
    last_activity_at: datetime | None = None

class TokenPair(BaseModel):
    token_type: str = "bearer"
    access_token: str
    refresh_token: str
    expires_in: int
    user: UserProfile

class BookmarkInput(BaseModel):
    manga_slug: str = Field(min_length=1, max_length=180)
    manga_title: str = Field(min_length=1, max_length=240)
    cover: str | None = None
    manga_type: str | None = None

class HistoryItem(BaseModel):
    manga_slug: str
    manga_title: str
    chapter_number: str
    chapter_title: str | None = None
    cover: str | None = None
    read_at: datetime
    progress: float = 0

class ActivityItem(BaseModel):
    action: str
    description: str
    created_at: datetime

class NotificationPreferences(BaseModel):
    email_updates: bool = False
    reading_reminders: bool = False
    product_announcements: bool = False




class ReadingHistoryInput(BaseModel):
    manga_slug: str = Field(min_length=1, max_length=180)
    manga_title: str = Field(min_length=1, max_length=240)
    chapter_number: str = Field(min_length=1, max_length=40)
    chapter_title: str | None = Field(default=None, max_length=240)
    cover: str | None = Field(default=None, max_length=2048)
    progress: float = Field(default=0, ge=0, le=100)
