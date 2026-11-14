from __future__ import annotations

import re
import time
from datetime import timedelta
from typing import Any

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pymongo import DESCENDING

from app.admin.dependencies import admin_user, owner_user, require_permission, ROLE_PERMISSIONS
from app.admin.schemas import (
    ChapterVisibilityInput, HomepageSettingsInput, MangaOverrideInput,
    SiteSettingsInput, UserRoleInput, UserStatusInput,
)
from app.core.config import settings
from app.models.common import APIResponse, Pagination, ResponseMeta
from app.scraper.client import SourceFetchError, source_client
from app.scraper.parsers import normalize_source_card, parse_browse, parse_manga_detail
from app.services.cache import cache
from app.services.database import get_database
from app.services.rate_limiter import rate_limiter
from app.users.repository import utcnow

# Intentionally excluded from the public API schema / developer portal.
router = APIRouter(prefix="/_control", include_in_schema=False)


def _meta(request: Request):
    return ResponseMeta(
        request_id=getattr(request.state, "request_id", "unknown"),
        source="manhwavoid-admin",
    )


def _page(page: int, per_page: int, total: int):
    return Pagination(
        page=page, per_page=per_page, total=total,
        total_pages=max(1, (total + per_page - 1) // per_page),
        has_next=page * per_page < total, has_previous=page > 1,
    )


def _chapter_key(value) -> str:
    if value is None:
        return ""
    try:
        number = float(value)
        return str(int(number)) if number.is_integer() else str(number)
    except (TypeError, ValueError):
        text = str(value).strip()
        match = re.search(r"(-?\d+(?:\.\d+)?)", text)
        if match:
            return _chapter_key(match.group(1))
        return text


def _safe_user(doc: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(doc["_id"]),
        "username": doc.get("username", ""),
        "display_name": doc.get("display_name") or doc.get("username", ""),
        "email": doc.get("email", ""),
        "role": doc.get("role", "user"),
        "is_active": bool(doc.get("is_active", True)),
        "joined_at": doc.get("created_at"),
        "last_activity_at": doc.get("last_activity_at"),
        "last_read_at": doc.get("last_read_at"),
        "avatar": doc.get("avatar"),
    }


async def _audit(actor: dict, action: str, target_type: str, target_id: str | None = None,
                 details: dict | None = None):
    await get_database().admin_audit_logs.insert_one({
        "actor_id": str(actor["_id"]),
        "actor_username": actor.get("username", "unknown"),
        "actor_role": actor.get("role", "user"),
        "action": action,
        "target_type": target_type,
        "target_id": target_id,
        "details": details or {},
        "created_at": utcnow(),
    })


@router.get("/me")
async def admin_me(request: Request, user=Depends(admin_user)):
    role = user.get("role", "user")
    permissions = sorted(ROLE_PERMISSIONS.get(role, set()))
    return APIResponse(data={
        "id": str(user["_id"]), "username": user.get("username"),
        "display_name": user.get("display_name"), "email": user.get("email"),
        "role": role, "permissions": permissions,
    }, meta=_meta(request))


@router.get("/dashboard")
async def dashboard(request: Request, user=Depends(require_permission("dashboard.read"))):
    db = get_database()
    now = utcnow()
    since = now - timedelta(hours=24)
    users_total = await db.users.count_documents({})
    users_active = await db.users.count_documents({"is_active": True})
    active_24h = await db.users.count_documents({"last_activity_at": {"$gte": since}})
    bookmarks = await db.bookmarks.count_documents({})
    history = await db.reading_history.count_documents({})
    sessions = await db.sessions.count_documents({"expires_at": {"$gt": now}})
    return APIResponse(data={
        "users_total": users_total,
        "users_active": users_active,
        "users_active_24h": active_24h,
        "bookmarks_total": bookmarks,
        "reading_positions": history,
        "active_sessions": sessions,
        "cache": cache.stats(),
        "rate_limiter": "available" if rate_limiter.available else ("disabled" if not settings.rate_limit_enabled else "degraded"),
        "generated_at": now,
        "metrics_note": "Request-level analytics are not currently persisted by this API; no request totals are fabricated.",
    }, meta=_meta(request))


@router.get("/users")
async def list_users(
    request: Request,
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=1, le=100),
    q: str | None = Query(None, max_length=100),
    role: str | None = Query(None),
    status: str | None = Query(None),
    user=Depends(require_permission("users.read")),
):
    db = get_database()
    query: dict[str, Any] = {}
    if q and q.strip():
        pattern = re.escape(q.strip())
        query["$or"] = [
            {"username": {"$regex": pattern, "$options": "i"}},
            {"display_name": {"$regex": pattern, "$options": "i"}},
            {"email": {"$regex": pattern, "$options": "i"}},
        ]
    if role and role in {"owner", "admin", "content_manager", "moderator", "user"}:
        query["role"] = role
    if status == "active":
        query["is_active"] = True
    elif status == "suspended":
        query["is_active"] = False
    total = await db.users.count_documents(query)
    cursor = db.users.find(query, {
        "password_hash": 0,
    }).sort("created_at", DESCENDING).skip((page - 1) * per_page).limit(per_page)
    items = [_safe_user(item) async for item in cursor]
    return APIResponse(data={"items": items, "total": total}, meta=_meta(request), pagination=_page(page, per_page, total))


@router.patch("/users/{user_id}/status")
async def set_user_status(
    user_id: str, payload: UserStatusInput, request: Request,
    actor=Depends(require_permission("users.manage")),
):
    if not ObjectId.is_valid(user_id):
        raise HTTPException(status_code=404, detail="User not found.")
    db = get_database()
    target = await db.users.find_one({"_id": ObjectId(user_id)})
    if not target:
        raise HTTPException(status_code=404, detail="User not found.")
    if str(target["_id"]) == str(actor["_id"]) and not payload.is_active:
        raise HTTPException(status_code=400, detail="You cannot suspend your own account.")
    if target.get("role") == "owner" and actor.get("role") != "owner":
        raise HTTPException(status_code=403, detail="Only the owner can change an owner account.")
    if target.get("role") == "owner" and not payload.is_active:
        owners = await db.users.count_documents({"role": "owner", "is_active": True})
        if owners <= 1:
            raise HTTPException(status_code=400, detail="The last active owner cannot be suspended.")
    await db.users.update_one({"_id": target["_id"]}, {"$set": {"is_active": payload.is_active, "updated_at": utcnow()}})
    if not payload.is_active:
        await db.sessions.delete_many({"user_id": user_id})
    await _audit(actor, "user.activated" if payload.is_active else "user.suspended", "user", user_id, {"reason": payload.reason})
    updated = await db.users.find_one({"_id": target["_id"]})
    return APIResponse(data={"user": _safe_user(updated)}, meta=_meta(request))


@router.patch("/users/{user_id}/role")
async def set_user_role(
    user_id: str, payload: UserRoleInput, request: Request,
    actor=Depends(owner_user),
):
    if not ObjectId.is_valid(user_id):
        raise HTTPException(status_code=404, detail="User not found.")
    db = get_database()
    target = await db.users.find_one({"_id": ObjectId(user_id)})
    if not target:
        raise HTTPException(status_code=404, detail="User not found.")
    if str(target["_id"]) == str(actor["_id"]) and payload.role != "owner":
        raise HTTPException(status_code=400, detail="You cannot remove your own owner role.")
    if target.get("role") == "owner" and payload.role != "owner":
        owners = await db.users.count_documents({"role": "owner", "is_active": True})
        if owners <= 1:
            raise HTTPException(status_code=400, detail="The last owner role cannot be removed.")
    await db.users.update_one({"_id": target["_id"]}, {"$set": {"role": payload.role, "updated_at": utcnow()}})
    await _audit(actor, "user.role_changed", "user", user_id, {"from": target.get("role", "user"), "to": payload.role, "reason": payload.reason})
    updated = await db.users.find_one({"_id": target["_id"]})
    return APIResponse(data={"user": _safe_user(updated)}, meta=_meta(request))


@router.get("/homepage")
async def get_homepage_settings(request: Request, user=Depends(require_permission("homepage.read"))):
    db = get_database()
    doc = await db.homepage_settings.find_one({"_id": "global"}) or {}
    data = {
        "visible_sections": doc.get("visible_sections", ["hero", "trending", "latest_updates", "popular", "recommended"]),
        "section_titles": doc.get("section_titles", {}),
        "section_order": doc.get("section_order", ["hero", "trending", "latest_updates", "popular", "recommended"]),
    }
    return APIResponse(data=data, meta=_meta(request))


@router.put("/homepage")
async def update_homepage_settings(payload: HomepageSettingsInput, request: Request,
                                   actor=Depends(require_permission("homepage.write"))):
    allowed = {"hero", "trending", "latest_updates", "popular", "recommended"}
    if set(payload.visible_sections) - allowed or set(payload.section_order) - allowed:
        raise HTTPException(status_code=422, detail="Unknown homepage section.")
    if len(set(payload.section_order)) != len(payload.section_order):
        raise HTTPException(status_code=422, detail="Homepage section order contains duplicates.")
    titles = {k: v.strip()[:80] for k, v in payload.section_titles.items() if k in allowed and v.strip()}
    doc = {**payload.model_dump(), "section_titles": titles, "updated_at": utcnow(), "updated_by": str(actor["_id"])}
    await get_database().homepage_settings.update_one({"_id": "global"}, {"$set": doc}, upsert=True)
    await _audit(actor, "homepage.updated", "homepage", "global", {"visible_sections": payload.visible_sections})
    return APIResponse(data={k: doc[k] for k in ("visible_sections", "section_titles", "section_order")}, meta=_meta(request))


@router.get("/settings")
async def get_site_settings(request: Request, user=Depends(require_permission("settings.read"))):
    doc = await get_database().site_settings.find_one({"_id": "global"}) or {}
    defaults = {
        "maintenance_mode": False, "registration_enabled": True,
        "announcement_enabled": False, "announcement_title": "",
        "announcement_body": "", "announcement_link": "",
    }
    defaults.update({k: doc[k] for k in defaults if k in doc})
    return APIResponse(data=defaults, meta=_meta(request))


@router.put("/settings")
async def update_site_settings(payload: SiteSettingsInput, request: Request,
                               actor=Depends(require_permission("settings.write"))):
    data = payload.model_dump()
    data.update({"updated_at": utcnow(), "updated_by": str(actor["_id"])})
    await get_database().site_settings.update_one({"_id": "global"}, {"$set": data}, upsert=True)
    await _audit(actor, "settings.updated", "settings", "global", {"keys": list(payload.model_fields_set)})
    return APIResponse(data=payload.model_dump(), meta=_meta(request))


@router.get("/system/health")
async def system_health(request: Request, user=Depends(require_permission("system.read"))):
    db_status = "unavailable"
    try:
        await get_database().command("ping")
        db_status = "connected"
    except Exception:
        pass
    return APIResponse(data={
        "api": "operational", "database": db_status,
        "rate_limiter": "available" if rate_limiter.available else ("disabled" if not settings.rate_limit_enabled else "degraded"),
        "cache": cache.stats(), "checked_at": utcnow(),
    }, meta=_meta(request))


@router.get("/scraper/status")
async def scraper_status(request: Request, user=Depends(require_permission("system.read"))):
    started = time.perf_counter()
    try:
        _, url, cached = await source_client.get_text("/", use_cache=False)
        return APIResponse(data={
            "status": "reachable", "http_status": 200,
            "duration_ms": round((time.perf_counter() - started) * 1000),
            "source_host": settings.source_base_url,
            "checked_at": utcnow(), "cached": cached,
        }, meta=_meta(request))
    except SourceFetchError as exc:
        return APIResponse(data={
            "status": "blocked" if exc.status_code == 403 else "unavailable",
            "http_status": exc.status_code,
            "error": str(exc),
            "duration_ms": round((time.perf_counter() - started) * 1000),
            "checked_at": utcnow(),
        }, meta=_meta(request))
    except Exception as exc:
        return APIResponse(data={
            "status": "error", "error_type": type(exc).__name__,
            "duration_ms": round((time.perf_counter() - started) * 1000),
            "checked_at": utcnow(),
        }, meta=_meta(request))


@router.get("/cache")
async def cache_status(request: Request, user=Depends(require_permission("system.read"))):
    return APIResponse(data=cache.stats(), meta=_meta(request))


@router.post("/cache/clear")
async def clear_cache(request: Request, actor=Depends(require_permission("cache.manage"))):
    before = cache.stats()
    cache.clear()
    await _audit(actor, "cache.cleared", "cache", "all", {"previous": before})
    return APIResponse(data={"cleared": True, "previous": before, "current": cache.stats()}, meta=_meta(request))


@router.get("/audit")
async def audit_logs(request: Request, page: int = Query(1, ge=1), per_page: int = Query(30, ge=1, le=100),
                     user=Depends(require_permission("audit.read"))):
    db = get_database()
    total = await db.admin_audit_logs.count_documents({})
    cursor = db.admin_audit_logs.find({}).sort("created_at", DESCENDING).skip((page - 1) * per_page).limit(per_page)
    items = []
    async for item in cursor:
        items.append({
            "id": str(item["_id"]), "actor_username": item.get("actor_username"),
            "actor_role": item.get("actor_role"), "action": item.get("action"),
            "target_type": item.get("target_type"), "target_id": item.get("target_id"),
            "details": item.get("details", {}), "created_at": item.get("created_at"),
        })
    return APIResponse(data={"items": items, "total": total}, meta=_meta(request), pagination=_page(page, per_page, total))


@router.get("/manga/search")
async def search_manga(request: Request, q: str = Query("", max_length=120),
                       page: int = Query(1, ge=1), per_page: int = Query(24, ge=1, le=60),
                       user=Depends(require_permission("content.read"))):
    params = {"site": settings.source_site_key, "page": page, "per_page": per_page}
    if q.strip():
        params["search"] = q.strip()
    try:
        payload, source_url, cached = await source_client.get_json("/api/local-manga", params, use_cache=True)
        raw = payload.get("data") if isinstance(payload, dict) and payload.get("success") else None
        if isinstance(raw, list):
            items = [normalize_source_card(x) for x in raw if isinstance(x, dict)]
            total = payload.get("total") if isinstance(payload.get("total"), int) else len(items)
            return APIResponse(data={"items": items, "total": total, "source": "upstream"}, meta=_meta(request), pagination=_page(page, per_page, total))
    except SourceFetchError:
        pass
    from urllib.parse import urlencode
    path = "/browse?" + urlencode({"search": q, "page": page, "per_page": per_page})
    try:
        html, source_url, cached = await source_client.get_text(path, use_cache=True)
    except SourceFetchError as exc:
        raise HTTPException(status_code=502, detail=f"Catalogue source is unavailable (upstream status: {exc.status_code or 'unknown'}).")
    parsed = parse_browse(html, source_url)
    items = parsed.get("items", [])
    return APIResponse(data={"items": items, "total": len(items), "source": "html"}, meta=_meta(request), pagination=_page(page, per_page, len(items)))


@router.get("/manga/{slug}/override")
async def get_manga_override(slug: str, request: Request, user=Depends(require_permission("content.read"))):
    if not re.fullmatch(r"[a-zA-Z0-9._-]{1,180}", slug):
        raise HTTPException(status_code=422, detail="Invalid manga slug.")
    doc = await get_database().manga_overrides.find_one({"slug": slug}, {"_id": 0, "updated_at": 0, "updated_by": 0})
    return APIResponse(data=doc or {"slug": slug, "title": None, "description": None, "status": None, "manga_type": None, "genres": None, "hidden": False}, meta=_meta(request))


@router.put("/manga/{slug}/override")
async def save_manga_override(slug: str, payload: MangaOverrideInput, request: Request,
                              actor=Depends(require_permission("content.write"))):
    if not re.fullmatch(r"[a-zA-Z0-9._-]{1,180}", slug):
        raise HTTPException(status_code=422, detail="Invalid manga slug.")
    submitted = payload.model_dump()
    fields = set(payload.model_fields_set)
    data = {k: submitted[k] for k in fields if submitted[k] is not None}
    unset = {k: "" for k in fields if submitted[k] is None}
    data.update({"slug": slug, "updated_at": utcnow(), "updated_by": str(actor["_id"])})
    update: dict[str, Any] = {"$set": data}
    if unset:
        update["$unset"] = unset
    await get_database().manga_overrides.update_one({"slug": slug}, update, upsert=True)
    await _audit(actor, "manga.override_saved", "manga", slug, {"fields": sorted(fields)})
    saved = await get_database().manga_overrides.find_one({"slug": slug}, {"_id": 0, "updated_at": 0, "updated_by": 0})
    return APIResponse(data=saved or {"slug": slug}, meta=_meta(request))


@router.get("/manga/{slug}/chapters")
async def admin_chapters(slug: str, request: Request, user=Depends(require_permission("content.read"))):
    if not re.fullmatch(r"[a-zA-Z0-9._-]{1,180}", slug):
        raise HTTPException(status_code=422, detail="Invalid manga slug.")
    try:
        payload, source_url, cached = await source_client.get_json("/api/local-manga", {"site": settings.source_site_key, "id": slug}, use_cache=True)
        raw = payload.get("data") if isinstance(payload, dict) and payload.get("success") else None
        if isinstance(raw, dict):
            from app.scraper.parsers import normalize_source_detail
            detail = normalize_source_detail(raw, f"{settings.source_base_url.rstrip('/')}/title/{slug}")
            chapters = detail.get("chapters", [])
        else:
            raise SourceFetchError("No chapter data returned.")
    except Exception as exc:
        from app.scraper.parsers import parse_manga_detail
        try:
            html, source_url, cached = await source_client.get_text(f"/title/{slug}", use_cache=True)
        except SourceFetchError as source_exc:
            raise HTTPException(status_code=502, detail=f"Chapter source is unavailable (upstream status: {source_exc.status_code or 'unknown'}).") from exc
        chapters = parse_manga_detail(html, source_url).get("chapters", [])
    overrides = await get_database().chapter_overrides.find({"manga_slug": slug}).to_list(length=1000)
    hidden = {_chapter_key(item.get("chapter_number")) for item in overrides if item.get("hidden")}
    items = []
    for chapter in chapters:
        number = _chapter_key(chapter.get("chapter_number") if chapter.get("chapter_number") is not None else chapter.get("title", ""))
        items.append({**chapter, "hidden": number in hidden})
    return APIResponse(data={"manga_slug": slug, "items": items, "total": len(items)}, meta=_meta(request))


@router.patch("/manga/{slug}/chapters/{chapter_number}/visibility")
async def set_chapter_visibility(slug: str, chapter_number: str, payload: ChapterVisibilityInput,
                                 request: Request, actor=Depends(require_permission("content.write"))):
    if not re.fullmatch(r"[a-zA-Z0-9._-]{1,180}", slug):
        raise HTTPException(status_code=422, detail="Invalid manga slug.")
    await get_database().chapter_overrides.update_one(
        {"manga_slug": slug, "chapter_number": _chapter_key(chapter_number)},
        {"$set": {"hidden": payload.hidden, "reason": payload.reason, "updated_at": utcnow(), "updated_by": str(actor["_id"])}},
        upsert=True,
    )
    normalized_number = _chapter_key(chapter_number)
    await _audit(actor, "chapter.visibility_changed", "chapter", f"{slug}:{normalized_number}", {"hidden": payload.hidden, "reason": payload.reason})
    return APIResponse(data={"manga_slug": slug, "chapter_number": normalized_number, "hidden": payload.hidden}, meta=_meta(request))
