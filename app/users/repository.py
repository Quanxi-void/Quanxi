from datetime import datetime, timezone
from bson import ObjectId
from cachetools import TTLCache
from threading import RLock
from app.services.database import get_database
from app.core.config import settings


def utcnow():
    return datetime.now(timezone.utc)


def public_user(doc: dict) -> dict:
    return {
        "id": str(doc["_id"]), "username": doc["username"],
        "display_name": doc.get("display_name") or doc["username"],
        "bio": doc.get("bio"), "avatar": doc.get("avatar") or {"kind": "preset", "value": "Avatar-20(1).jpg", "url": "/api/v1/static/avatars/Avatar-20(1).jpg"},
        "joined_at": doc["created_at"], "is_public": doc.get("is_public", True),
        "email": doc["email"], "last_activity_at": doc.get("last_activity_at"),
        "last_read_at": doc.get("last_read_at"),
    }


async def log_activity(user_id: str, action: str, description: str):
    await get_database().activity_logs.insert_one({
        "user_id": user_id, "action": action, "description": description, "created_at": utcnow()
    })


_touch_cache = None
_touch_lock = RLock()


def _should_touch(user_id: str) -> bool:
    # Per-worker throttle: authenticated page views do not write last_activity_at
    # to MongoDB on every request. The cache is intentionally an optimization;
    # correctness does not depend on it.
    global _touch_cache
    if _touch_cache is None:
        _touch_cache = TTLCache(
            maxsize=20000,
            ttl=settings.user_activity_touch_interval_seconds,
        )
    with _touch_lock:
        if user_id in _touch_cache:
            return False
        _touch_cache[user_id] = True
        return True


async def touch_user(user_id: str, *, read_at: bool = False):
    now = utcnow()
    update = {}
    if read_at:
        update["last_read_at"] = now
    if read_at or _should_touch(user_id):
        update["last_activity_at"] = now
    if update:
        await get_database().users.update_one(
            {"_id": ObjectId(user_id)},
            {"$set": update},
        )


async def account_summary(user_id: str) -> dict:
    db = get_database()
    bookmarks = await db.bookmarks.count_documents({"user_id": user_id})
    chapters = await db.reading_history.count_documents({"user_id": user_id})
    user = await db.users.find_one({"_id": ObjectId(user_id)}, {"last_activity_at": 1, "last_read_at": 1}) or {}
    return {"bookmarked": bookmarks, "total_chapters_read": chapters,
            "last_read_at": user.get("last_read_at"), "last_activity_at": user.get("last_activity_at")}


async def record_chapter_read(user_id: str, manga_slug: str, manga_title: str,
                              chapter_number: str, chapter_title: str | None = None,
                              cover: str | None = None):
    db = get_database()
    now = utcnow()
    # One current reading-position document per user + manga. Reading a newer
    # chapter updates the existing manga row instead of creating duplicates.
    await db.reading_history.update_one(
        {"user_id": user_id, "manga_slug": manga_slug},
        {"$set": {"manga_title": manga_title, "chapter_number": str(chapter_number),
                  "chapter_title": chapter_title, "cover": cover,
                  "progress": 0, "read_at": now},
         "$setOnInsert": {"created_at": now}},
        upsert=True,
    )
    await touch_user(user_id, read_at=True)
    await log_activity(user_id, "chapter_read", f"Read {manga_title} — Chapter {chapter_number}")
