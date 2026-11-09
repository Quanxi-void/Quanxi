import re
import uuid
from pathlib import Path
from urllib.parse import urlparse
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile, File, Query
from starlette.concurrency import run_in_threadpool
from bson import ObjectId
from pymongo.errors import DuplicateKeyError
from PIL import Image, UnidentifiedImageError
from app.auth.dependencies import current_user, optional_user
from app.auth.security import verify_password, hash_password
from app.core.config import settings
from app.models.common import APIResponse, ResponseMeta, Pagination
from app.services.database import get_database
from app.users.repository import public_user, account_summary, log_activity, touch_user, utcnow, record_chapter_read
from app.users.schemas import (ProfileUpdateInput, EmailChangeInput, PasswordChangeInput,
    AvatarURLInput, AvatarPresetInput, BookmarkInput, UsernameChangeInput, NotificationPreferences, ReadingHistoryInput)

router = APIRouter(prefix="/users", tags=["User Profiles", "Library"])
AVATAR_DIR = Path(settings.avatar_upload_dir)
PRESET_DIR = Path(__file__).resolve().parents[1] / "static" / "avatars"
ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
MAX_AVATAR_BYTES = 2 * 1024 * 1024

def _meta(request: Request):
    return ResponseMeta(request_id=getattr(request.state, "request_id", "unknown"), source="manhwahq-users")

def _profile(doc: dict):
    return public_user(doc)


def _process_avatar_image(raw: bytes, destination: Path) -> None:
    """Decode and resize an uploaded avatar outside the async event loop."""
    from io import BytesIO
    image = Image.open(BytesIO(raw))
    image.verify()
    image = Image.open(BytesIO(raw))
    if image.width > 5000 or image.height > 5000:
        raise ValueError("Image dimensions are too large.")
    AVATAR_DIR.mkdir(parents=True, exist_ok=True)
    if image.mode not in ("RGB", "RGBA"):
        image = image.convert("RGBA")
    image.thumbnail((1024, 1024))
    image.save(destination, "WEBP", quality=88, method=6)

@router.get("/avatars")
async def list_avatars(request: Request):
    items = [{"id": p.name, "url": f"{settings.api_prefix}/static/avatars/{p.name}"}
             for p in sorted(PRESET_DIR.iterdir()) if p.is_file() and p.suffix.lower() in ALLOWED_EXTENSIONS]
    return APIResponse(data={"items": items}, meta=_meta(request))

@router.get("/me")
async def get_my_profile(request: Request, user=Depends(current_user)):
    user = await get_database().users.find_one({"_id": user["_id"]})
    return APIResponse(data=_profile(user), meta=_meta(request))

@router.patch("/me")
async def update_my_profile(payload: ProfileUpdateInput, request: Request, user=Depends(current_user)):
    changes = payload.model_dump(exclude_unset=True)
    if "display_name" in changes and changes["display_name"] is not None:
        changes["display_name"] = changes["display_name"].strip()
        if not changes["display_name"]:
            raise HTTPException(status_code=422, detail="Display name cannot be empty.")
    if changes.get("is_public", True) is None:
        changes.pop("is_public", None)
    changes["updated_at"] = utcnow()
    await get_database().users.update_one({"_id": user["_id"]}, {"$set": changes})
    await log_activity(str(user["_id"]), "profile_updated", "Updated profile settings")
    updated = await get_database().users.find_one({"_id": user["_id"]})
    return APIResponse(data=_profile(updated), meta=_meta(request))

@router.get("/{username}")
async def public_profile(username: str, request: Request, viewer=Depends(optional_user)):
    doc = await get_database().users.find_one({"username": username.lower(), "is_active": True})
    if not doc:
        raise HTTPException(status_code=404, detail="User profile not found.")
    if not doc.get("is_public", True) and (viewer is None or viewer["_id"] != doc["_id"]):
        raise HTTPException(status_code=404, detail="User profile not found.")
    data = {k: v for k, v in _profile(doc).items() if k not in {"email", "last_activity_at", "last_read_at"}}
    return APIResponse(data=data, meta=_meta(request))

@router.get("/me/summary")
async def my_summary(request: Request, user=Depends(current_user)):
    return APIResponse(data=await account_summary(str(user["_id"])), meta=_meta(request))

@router.put("/me/avatar/preset")
async def choose_avatar(payload: AvatarPresetInput, request: Request, user=Depends(current_user)):
    candidate = (PRESET_DIR / payload.avatar_id).resolve()
    if candidate.parent != PRESET_DIR.resolve() or not candidate.is_file() or candidate.suffix.lower() not in ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=404, detail="Avatar preset not found.")
    avatar = {"kind": "preset", "value": candidate.name,
              "url": f"{settings.api_prefix}/static/avatars/{candidate.name}"}
    await get_database().users.update_one({"_id": user["_id"]}, {"$set": {"avatar": avatar}})
    await log_activity(str(user["_id"]), "avatar_changed", "Changed profile avatar")
    return APIResponse(data={"avatar": avatar}, meta=_meta(request))

@router.put("/me/avatar/url")
async def set_avatar_url(payload: AvatarURLInput, request: Request, user=Depends(current_user)):
    parsed = urlparse(payload.url)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
        raise HTTPException(status_code=422, detail="Avatar URL must be a valid HTTPS URL.")
    avatar = {"kind": "url", "value": payload.url, "url": payload.url}
    await get_database().users.update_one({"_id": user["_id"]}, {"$set": {"avatar": avatar}})
    await log_activity(str(user["_id"]), "avatar_changed", "Changed profile avatar")
    return APIResponse(data={"avatar": avatar}, meta=_meta(request))

@router.post("/me/avatar/upload")
async def upload_avatar(request: Request, file: UploadFile = File(...), user=Depends(current_user)):
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=415, detail="Use PNG, JPG, GIF, or WebP images.")
    raw = await file.read(MAX_AVATAR_BYTES + 1)
    if len(raw) > MAX_AVATAR_BYTES:
        raise HTTPException(status_code=413, detail="Avatar must be 2 MB or smaller.")
    filename = f"{user['_id']}_{uuid.uuid4().hex}.webp"
    destination = AVATAR_DIR / filename
    try:
        await run_in_threadpool(_process_avatar_image, raw, destination)
    except ValueError as exc:
        if str(exc) == "Image dimensions are too large.":
            raise HTTPException(status_code=422, detail=str(exc))
        raise HTTPException(status_code=415, detail="The uploaded file is not a supported image.")
    except (UnidentifiedImageError, OSError):
        raise HTTPException(status_code=415, detail="The uploaded file is not a supported image.")
    avatar = {"kind": "upload", "value": filename, "url": f"{settings.api_prefix}/uploads/avatars/{filename}"}
    await get_database().users.update_one({"_id": user["_id"]}, {"$set": {"avatar": avatar}})
    await log_activity(str(user["_id"]), "avatar_changed", "Uploaded a profile avatar")
    return APIResponse(data={"avatar": avatar}, meta=_meta(request))

@router.post("/me/username")
async def change_username(payload: UsernameChangeInput, request: Request, user=Depends(current_user)):
    if not verify_password(payload.current_password, user["password_hash"]):
        raise HTTPException(status_code=400, detail="Current password is incorrect.")
    try:
        await get_database().users.update_one({"_id": user["_id"]}, {"$set": {"username": payload.new_username, "updated_at": utcnow()}})
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="That username is already in use.")
    await log_activity(str(user["_id"]), "username_changed", "Changed account username")
    return APIResponse(data={"username": payload.new_username}, meta=_meta(request))

@router.post("/me/email")
async def change_email(payload: EmailChangeInput, request: Request, user=Depends(current_user)):
    if not verify_password(payload.current_password, user["password_hash"]):
        raise HTTPException(status_code=400, detail="Current password is incorrect.")
    try:
        await get_database().users.update_one({"_id": user["_id"]}, {"$set": {"email": str(payload.new_email).lower(), "updated_at": utcnow()}})
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="That email address is already in use.")
    await log_activity(str(user["_id"]), "email_changed", "Changed account email")
    return APIResponse(data={"email": str(payload.new_email).lower()}, meta=_meta(request))

@router.post("/me/password")
async def change_password(payload: PasswordChangeInput, request: Request, user=Depends(current_user)):
    if not verify_password(payload.current_password, user["password_hash"]):
        raise HTTPException(status_code=400, detail="Current password is incorrect.")
    await get_database().users.update_one({"_id": user["_id"]}, {"$set": {"password_hash": hash_password(payload.new_password), "updated_at": utcnow()}})
    await get_database().sessions.delete_many({"user_id": str(user["_id"])})
    await log_activity(str(user["_id"]), "password_changed", "Changed account password")
    return APIResponse(data={"password_changed": True, "sessions_revoked": True}, meta=_meta(request))

@router.get("/me/preferences")
async def get_preferences(request: Request, user=Depends(current_user)):
    preferences = user.get("preferences") or {"email_updates": False, "reading_reminders": False, "product_announcements": False}
    return APIResponse(data={"notifications": preferences}, meta=_meta(request))

@router.patch("/me/preferences")
async def update_preferences(payload: NotificationPreferences, request: Request, user=Depends(current_user)):
    preferences = payload.model_dump()
    await get_database().users.update_one({"_id": user["_id"]}, {"$set": {"preferences": preferences}})
    await log_activity(str(user["_id"]), "preferences_updated", "Updated notification preferences")
    return APIResponse(data={"notifications": preferences}, meta=_meta(request))

@router.get("/me/export")
async def export_account_data(request: Request, user=Depends(current_user)):
    db = get_database(); user_id = str(user["_id"])
    bookmarks = await db.bookmarks.find({"user_id": user_id}, {"_id": 0, "user_id": 0}).to_list(length=10000)
    history = await db.reading_history.find({"user_id": user_id}, {"_id": 0, "user_id": 0}).to_list(length=10000)
    profile = {"username": user["username"], "display_name": user.get("display_name"),
               "email": user["email"], "bio": user.get("bio"), "avatar": user.get("avatar"),
               "joined_at": user["created_at"]}
    return APIResponse(data={"profile": profile, "bookmarks": bookmarks,
                             "reading_history": history, "exported_at": utcnow()}, meta=_meta(request))

@router.get("/me/bookmarks")
async def list_bookmarks(request: Request, page: int = Query(1, ge=1), per_page: int = Query(24, ge=1, le=100), user=Depends(current_user)):
    db = get_database(); query = {"user_id": str(user["_id"])}
    total = await db.bookmarks.count_documents(query)
    cursor = db.bookmarks.find(query).sort("created_at", -1).skip((page - 1) * per_page).limit(per_page)
    items = await cursor.to_list(length=per_page)
    for item in items: item["id"] = str(item.pop("_id"))
    return APIResponse(data={"items": items, "total": total}, meta=_meta(request),
        pagination=Pagination(page=page, per_page=per_page, total=total, total_pages=max(1, (total + per_page - 1)//per_page), has_next=page*per_page < total, has_previous=page > 1))

@router.post("/me/bookmarks", status_code=201)
async def add_bookmark(payload: BookmarkInput, request: Request, user=Depends(current_user)):
    db = get_database(); user_id = str(user["_id"])
    try:
        await db.bookmarks.insert_one({**payload.model_dump(), "user_id": user_id, "created_at": utcnow()})
    except DuplicateKeyError:
        return APIResponse(data={"bookmarked": True, "already_exists": True}, meta=_meta(request))
    await log_activity(user_id, "bookmark_added", f"Bookmarked {payload.manga_title}")
    return APIResponse(data={"bookmarked": True}, meta=_meta(request))

@router.delete("/me/bookmarks/{manga_slug}")
async def remove_bookmark(manga_slug: str, request: Request, user=Depends(current_user)):
    result = await get_database().bookmarks.delete_one({"user_id": str(user["_id"]), "manga_slug": manga_slug})
    if not result.deleted_count: raise HTTPException(status_code=404, detail="Bookmark not found.")
    await log_activity(str(user["_id"]), "bookmark_removed", f"Removed bookmark {manga_slug}")
    return APIResponse(data={"bookmarked": False}, meta=_meta(request))

@router.get("/me/history")
async def reading_history(request: Request, page: int = Query(1, ge=1), per_page: int = Query(30, ge=1, le=100), user=Depends(current_user)):
    db = get_database(); query = {"user_id": str(user["_id"])}
    total = await db.reading_history.count_documents(query)
    cursor = db.reading_history.find(query).sort("read_at", -1).skip((page - 1) * per_page).limit(per_page)
    items = await cursor.to_list(length=per_page)
    for item in items: item["id"] = str(item.pop("_id"))
    return APIResponse(data={"items": items, "total": total}, meta=_meta(request),
        pagination=Pagination(page=page, per_page=per_page, total=total, total_pages=max(1, (total + per_page - 1)//per_page), has_next=page*per_page < total, has_previous=page > 1))

@router.post("/me/history", status_code=201)
async def record_reading_history(payload: ReadingHistoryInput, request: Request, user=Depends(current_user)):
    await record_chapter_read(
        str(user["_id"]), payload.manga_slug, payload.manga_title,
        payload.chapter_number, payload.chapter_title, payload.cover
    )
    # Keep progress supplied by the reader when it is non-zero.
    if payload.progress:
        await get_database().reading_history.update_one(
            {"user_id": str(user["_id"]), "manga_slug": payload.manga_slug},
            {"$set": {"progress": payload.progress}}
        )
    return APIResponse(data={"recorded": True, "manga_slug": payload.manga_slug,
                             "chapter_number": payload.chapter_number}, meta=_meta(request))


@router.delete("/me/history")
async def clear_history(request: Request, user=Depends(current_user)):
    result = await get_database().reading_history.delete_many({"user_id": str(user["_id"])})
    await get_database().users.update_one({"_id": user["_id"]}, {"$set": {"last_read_at": None}})
    await log_activity(str(user["_id"]), "history_cleared", "Cleared reading history")
    return APIResponse(data={"deleted": result.deleted_count}, meta=_meta(request))

@router.get("/me/activity")
async def recent_activity(request: Request, limit: int = Query(20, ge=1, le=100), user=Depends(current_user)):
    cursor = get_database().activity_logs.find({"user_id": str(user["_id"])}).sort("created_at", -1).limit(limit)
    items = await cursor.to_list(length=limit)
    for item in items: item["id"] = str(item.pop("_id"))
    return APIResponse(data={"items": items}, meta=_meta(request))
