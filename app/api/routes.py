import uuid
import logging
import re
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from starlette.concurrency import run_in_threadpool
from app.core.config import settings
from app.models.common import APIResponse, ResponseMeta, Pagination, APIError
from app.models.manga import (MangaDetails, BrowseData, HomeData, ChapterListData,
    RecommendationData)
from app.models.reader import ReaderData
from app.scraper.client import source_client, SourceFetchError
from app.scraper.parsers import (parse_home, parse_browse, parse_manga_detail, parse_reader,
    normalize_source_card, normalize_source_detail, normalize_source_reader)
from app.services.cache import cache
from app.services.database import get_database
from app.services.rate_limiter import rate_limiter
from app.auth.dependencies import optional_user
from app.users.repository import record_chapter_read

router = APIRouter()
logger = logging.getLogger(__name__)

def meta(request: Request, url: str, cached: bool = False) -> ResponseMeta:
    return ResponseMeta(
        request_id=getattr(request.state, "request_id", str(uuid.uuid4())),
        source="manhwahq.com",
        cached=cached,
        source_url=url,
    )


async def _apply_card_overrides(items: list[dict]) -> list[dict]:
    """Apply locally managed title/visibility overrides without mutating source data."""
    slugs = [str(item.get("slug")) for item in items if item.get("slug")]
    if not slugs:
        return items
    try:
        docs = await get_database().manga_overrides.find({"slug": {"$in": slugs}}).to_list(length=min(len(slugs), 500))
    except Exception:
        logger.warning("Could not load manga overrides; returning upstream cards unchanged.", exc_info=True)
        return items
    overrides = {doc["slug"]: doc for doc in docs}
    result = []
    for original in items:
        item = dict(original)
        override = overrides.get(item.get("slug"), {})
        if override.get("hidden"):
            continue
        if override.get("title"):
            item["title"] = override["title"]
        if override.get("manga_type"):
            item["type"] = override["manga_type"]
        result.append(item)
    return result


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


async def _ensure_manga_visible(slug: str) -> None:
    try:
        hidden = await get_database().manga_overrides.find_one({"slug": slug, "hidden": True}, {"_id": 1})
    except Exception:
        logger.warning("Could not verify manga visibility override for %s.", slug, exc_info=True)
        return
    if hidden:
        raise HTTPException(status_code=404, detail="Manga not found.")


async def _apply_detail_overrides(slug: str, data: dict) -> dict:
    try:
        db = get_database()
        override = await db.manga_overrides.find_one({"slug": slug})
        chapter_overrides = await db.chapter_overrides.find({"manga_slug": slug, "hidden": True}).to_list(length=1000)
    except Exception:
        logger.warning("Could not load manga overrides; returning upstream detail unchanged.", exc_info=True)
        return data
    if override and override.get("hidden"):
        raise HTTPException(status_code=404, detail="Manga not found.")
    if override:
        if override.get("title"):
            data.setdefault("title", {})["primary"] = override["title"]
        if override.get("description") is not None:
            data["description"] = override["description"]
        metadata = data.setdefault("metadata", {})
        if override.get("status"):
            metadata["status"] = override["status"]
        if override.get("manga_type"):
            metadata["type"] = override["manga_type"]
        if override.get("genres") is not None:
            metadata["genres"] = override["genres"]
    hidden_numbers = {_chapter_key(item.get("chapter_number")) for item in chapter_overrides}
    if hidden_numbers:
        data["chapters"] = [c for c in data.get("chapters", [])
                            if _chapter_key(c.get("chapter_number") if c.get("chapter_number") is not None else c.get("title", "")) not in hidden_numbers]
    return data


async def _apply_homepage_settings(data: dict) -> dict:
    keys = ["hero", "trending", "latest_updates", "popular", "recommended"]
    defaults = {"visible_sections": keys, "section_titles": {}, "section_order": keys}
    try:
        doc = await get_database().homepage_settings.find_one({"_id": "global"}) or {}
    except Exception:
        logger.warning("Could not load homepage settings; using source defaults.", exc_info=True)
        doc = {}
    visible = [key for key in doc.get("visible_sections", defaults["visible_sections"]) if key in keys]
    order = [key for key in doc.get("section_order", defaults["section_order"]) if key in keys]
    titles = doc.get("section_titles", {})
    for key in keys:
        if key not in visible:
            data[key] = []
        else:
            data[key] = await _apply_card_overrides(data.get(key, []))
    data["sections"] = [
        {"key": key, "title": titles.get(key) or key.replace("_", " ").title(), "items": data.get(key, [])}
        for key in order if key in visible
    ]
    data["admin_settings"] = {"visible_sections": visible, "section_titles": titles, "section_order": order}
    return data



async def fetch_or_502(
    path: str,
    use_cache: bool = True,
    request: Request | None = None,
):
    request_id = (
        getattr(request.state, "request_id", "unknown")
        if request else "unknown"
    )

    try:
        return await source_client.get_text(
            path,
            use_cache=use_cache,
        )

    except SourceFetchError as exc:
        logger.error(
            "UPSTREAM_SOURCE_FAILURE | request_id=%s | "
            "path=%s | status=%s | error=%s | cause=%r",
            request_id,
            path,
            exc.status_code,
            str(exc),
            exc.__cause__,
        )

        if exc.status_code == 404:
            raise HTTPException(
                status_code=404,
                detail="Source page not found."
            )

        if exc.status_code == 429:
            raise HTTPException(
                status_code=503,
                detail="The manga source is temporarily rate limiting requests."
            )

        raise HTTPException(
            status_code=502,
            detail="The manga source is temporarily unavailable."
        )

    except ValueError as exc:
        logger.exception(
            "UPSTREAM_URL_ERROR | request_id=%s | path=%s",
            request_id,
            path,
        )
        raise HTTPException(status_code=400, detail=str(exc))


async def fetch_source_data(params: dict):
    request_params = {**params, "site": settings.source_site_key}
    try:
        payload, url, cached = await source_client.get_json("/api/local-manga", request_params)
    except SourceFetchError as exc:
        if exc.status_code == 429:
            raise HTTPException(status_code=503, detail="The manga source is temporarily rate limiting requests.")
        raise HTTPException(status_code=502, detail="The manga source is temporarily unavailable.")
    if isinstance(payload, dict) and payload.get("success"):
        return payload.get("data"), payload, url, cached
    return None, payload, url, cached


@router.get("/site-config", tags=["System"], summary="Get public website configuration")
async def public_site_config(request: Request):
    try:
        doc = await get_database().site_settings.find_one({"_id": "global"}) or {}
    except Exception:
        logger.warning("Could not load public site settings; using defaults.", exc_info=True)
        doc = {}
    return APIResponse(data={
        "maintenance_mode": bool(doc.get("maintenance_mode", False)),
        "registration_enabled": bool(doc.get("registration_enabled", True)),
        "announcement_enabled": bool(doc.get("announcement_enabled", False)),
        "announcement_title": doc.get("announcement_title", ""),
        "announcement_body": doc.get("announcement_body", ""),
        "announcement_link": doc.get("announcement_link", ""),
    }, meta=meta(request, settings.source_base_url))


@router.head("/health", tags=["System"], include_in_schema=False)
async def health_head():
    """Lightweight HEAD probe for uptime monitors (UptimeRobot etc.). No body, no database or source calls."""
    return Response(status_code=200)


@router.get("/health", tags=["System"], summary="API and optional source connectivity check")
async def health(request: Request, check_source: bool = Query(False, description="Perform a live request to the source homepage")):
    source_check = "not_checked"
    if check_source:
        try:
            await source_client.get_text("/", use_cache=False)
            source_check = "reachable"
        except Exception:
            source_check = "unreachable"
    return APIResponse(
        data={
            "status": "ok",
            "service": settings.app_name,
            "version": settings.app_version,
            "source": settings.source_base_url,
            "data_api": settings.source_data_api_base_url,
            "source_check": source_check,
            "rate_limiter": (
                "disabled" if not settings.rate_limit_enabled
                else "available" if rate_limiter.available
                else "degraded"
            ),
            "rate_limiter_backend": "mongodb",
        },
        meta=meta(request, settings.source_base_url),
    )


@router.get("/home", tags=["Discovery"], response_model=APIResponse[HomeData], summary="Get homepage sections")
async def home(request: Request, refresh: bool = Query(False, description="Bypass cached source HTML")):
    html, url, cached = await fetch_or_502("/", use_cache=not refresh, request=request)
    data = await run_in_threadpool(parse_home, html, url)
    data = await _apply_homepage_settings(data)
    return APIResponse(data=data, meta=meta(request, url, cached))


@router.get("/browse", tags=["Discovery"], response_model=APIResponse[BrowseData], summary="Browse the manga catalogue")
async def browse(
    request: Request,
    page: int = Query(1, ge=1),
    per_page: int = Query(24, ge=1, le=100),
    sort: str | None = None,
    search: str | None = None,
    genre: str | None = None,
    status: str | None = None,
    manga_type: str | None = Query(None, alias="type"),
):
    params = {"page": page, "per_page": per_page}
    for key, value in {"sort": sort, "search": search, "genre": genre, "status": status, "type": manga_type}.items():
        if value:
            params[key] = value
    source_items, payload, data_url, cached = await fetch_source_data(params)
    if isinstance(source_items, list):
        items = [normalize_source_card(item) for item in source_items if isinstance(item, dict)]
        items = await _apply_card_overrides(items)
        total = payload.get("total") if isinstance(payload, dict) else None
        total_pages = payload.get("pages") if isinstance(payload, dict) else None
        available = payload.get("availableGenres", []) if isinstance(payload, dict) else []
        data = {"items": items, "available_genres": available, "sort_options": [],
                "applied_filters": {k: v for k, v in params.items() if k not in {"page", "per_page"}}}
        pagination = Pagination(page=page, per_page=per_page, total=total, total_pages=total_pages,
                                has_next=page < total_pages if isinstance(total_pages, int) else len(items) >= per_page,
                                has_previous=page > 1)
        return APIResponse(data=data, meta=meta(request, data_url, cached), pagination=pagination)
    from urllib.parse import urlencode
    path = "/browse?" + urlencode(params)
    html, url, html_cached = await fetch_or_502(path)
    data = await run_in_threadpool(parse_browse, html, url)
    data["items"] = await _apply_card_overrides(data.get("items", []))
    total = len(data["items"])
    data["applied_filters"] = {k: v for k, v in params.items() if k not in {"page", "per_page"}}
    pagination = Pagination(page=page, per_page=per_page, total=None, total_pages=None,
                            has_next=total >= per_page, has_previous=page > 1)
    return APIResponse(data=data, meta=meta(request, url, html_cached), pagination=pagination)


@router.get("/search", tags=["Discovery"], response_model=APIResponse[BrowseData], summary="Search manga")
async def search(
    request: Request,
    q: str = Query(..., min_length=1, max_length=120),
    page: int = Query(1, ge=1),
    per_page: int = Query(24, ge=1, le=100),
):
    params = {"search": q, "page": page, "per_page": per_page}
    source_items, payload, data_url, cached = await fetch_source_data(params)
    if isinstance(source_items, list):
        items = [normalize_source_card(item) for item in source_items if isinstance(item, dict)]
        items = await _apply_card_overrides(items)
        total = payload.get("total") if isinstance(payload, dict) else None
        total_pages = payload.get("pages") if isinstance(payload, dict) else None
        data = {"items": items, "available_genres": payload.get("availableGenres", []) if isinstance(payload, dict) else [],
                "sort_options": [], "applied_filters": {"search": q}}
        pagination = Pagination(page=page, per_page=per_page, total=total, total_pages=total_pages,
                                has_next=page < total_pages if isinstance(total_pages, int) else len(items) >= per_page,
                                has_previous=page > 1)
        return APIResponse(data=data, meta=meta(request, data_url, cached), pagination=pagination)
    from urllib.parse import urlencode
    path = "/browse?" + urlencode(params)
    html, url, html_cached = await fetch_or_502(path)
    data = await run_in_threadpool(parse_browse, html, url)
    data["items"] = await _apply_card_overrides(data.get("items", []))
    total = len(data["items"])
    data["applied_filters"] = {"search": q}
    pagination = Pagination(page=page, per_page=per_page, total=None, total_pages=None,
                            has_next=total >= per_page, has_previous=page > 1)
    return APIResponse(data=data, meta=meta(request, url, html_cached), pagination=pagination)


@router.get("/genres", tags=["Discovery"], summary="List genres available in the browse catalogue")
async def genres(request: Request):
    source_items, payload, data_url, cached = await fetch_source_data({"genres": "true"})
    if isinstance(source_items, list):
        items = sorted({str(item).strip() for item in source_items if str(item).strip()}, key=str.casefold)
        return APIResponse(data={"items": items}, meta=meta(request, data_url, cached))
    html, url, html_cached = await fetch_or_502("/browse")
    data = await run_in_threadpool(parse_browse, html, url)
    return APIResponse(data={"items": data["available_genres"]}, meta=meta(request, url, html_cached))


@router.get("/genres/{genre}", tags=["Discovery"], response_model=APIResponse[BrowseData], summary="Browse manga by genre")
async def genre_manga(
    request: Request,
    genre: str,
    page: int = Query(1, ge=1),
    per_page: int = Query(24, ge=1, le=100),
):
    params = {"genre": genre, "page": page, "per_page": per_page}
    source_items, payload, data_url, cached = await fetch_source_data(params)
    if isinstance(source_items, list):
        items = [normalize_source_card(item) for item in source_items if isinstance(item, dict)]
        items = await _apply_card_overrides(items)
        total_pages = payload.get("pages") if isinstance(payload, dict) else None
        data = {"items": items, "available_genres": payload.get("availableGenres", []) if isinstance(payload, dict) else [],
                "sort_options": [], "applied_filters": {"genre": genre}}
        pagination = Pagination(page=page, per_page=per_page, total=payload.get("total"), total_pages=total_pages,
                                has_next=page < total_pages if isinstance(total_pages, int) else len(items) >= per_page,
                                has_previous=page > 1)
        return APIResponse(data=data, meta=meta(request, data_url, cached), pagination=pagination)
    from urllib.parse import urlencode
    path = "/browse?" + urlencode(params)
    html, url, html_cached = await fetch_or_502(path)
    data = await run_in_threadpool(parse_browse, html, url)
    data["items"] = await _apply_card_overrides(data.get("items", []))
    data["applied_filters"] = {"genre": genre}
    total = len(data["items"])
    pagination = Pagination(page=page, per_page=per_page, total=None, total_pages=None,
                            has_next=total >= per_page, has_previous=page > 1)
    return APIResponse(data=data, meta=meta(request, url, html_cached), pagination=pagination)


@router.get("/manga/{slug}", tags=["Manga"], response_model=APIResponse[MangaDetails], summary="Get manga details")
async def manga_details(request: Request, slug: str):
    if not slug or "/" in slug or slug in {".", ".."}:
        raise HTTPException(status_code=400, detail="Invalid manga slug.")
    path = f"/title/{slug}"
    raw, payload, data_url, cached = await fetch_source_data({"id": slug})
    if isinstance(raw, dict):
        data = normalize_source_detail(raw, path_url := f"{settings.source_base_url.rstrip('/')}{path}")
        # Recommendations are page-specific UI content and may not be included in the
        # catalogue JSON record. Enrich them from the rendered detail page when available.
        try:
            html, page_url, html_cached = await fetch_or_502(path)
            html_data = await run_in_threadpool(parse_manga_detail, html, page_url)
            data["recommendations"] = html_data.get("recommendations", [])
            if not data["images"].get("banner"):
                data["images"]["banner"] = html_data.get("images", {}).get("banner")
        except HTTPException:
            pass
        data["recommendations"] = await _apply_card_overrides(data.get("recommendations", []))
        data = await _apply_detail_overrides(slug, data)
        return APIResponse(data=data, meta=meta(request, data_url, cached))
    html, url, html_cached = await fetch_or_502(path)
    data = await run_in_threadpool(parse_manga_detail, html, url)
    if not data.get("slug"):
        data["slug"] = slug
    data["recommendations"] = await _apply_card_overrides(data.get("recommendations", []))
    data = await _apply_detail_overrides(slug, data)
    return APIResponse(data=data, meta=meta(request, url, html_cached))


@router.get("/manga/{slug}/chapters", tags=["Manga"], response_model=APIResponse[ChapterListData], summary="Get manga chapter list")
async def manga_chapters(
    request: Request,
    slug: str,
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
):
    await _ensure_manga_visible(slug)
    raw, payload, data_url, cached = await fetch_source_data({"id": slug})
    if isinstance(raw, dict):
        detail = normalize_source_detail(raw, f"{settings.source_base_url.rstrip('/')}/title/{slug}")
        items = detail["chapters"]
        url = data_url
        if not items:
            html, url, cached = await fetch_or_502(f"/title/{slug}")
            detail = await run_in_threadpool(parse_manga_detail, html, url)
            items = detail["chapters"]
    else:
        html, url, cached = await fetch_or_502(f"/title/{slug}")
        detail = await run_in_threadpool(parse_manga_detail, html, url)
        items = detail["chapters"]
    try:
        hidden_docs = await get_database().chapter_overrides.find({"manga_slug": slug, "hidden": True}).to_list(length=1000)
        hidden = {_chapter_key(item.get("chapter_number")) for item in hidden_docs}
        items = [item for item in items if _chapter_key(item.get("chapter_number") if item.get("chapter_number") is not None else item.get("title", "")) not in hidden]
    except Exception:
        logger.warning("Could not load chapter visibility overrides; returning source chapters.", exc_info=True)
    total = len(items)
    start = (page - 1) * per_page
    page_items = items[start:start + per_page]
    pagination = Pagination(
        page=page, per_page=per_page, total=total,
        total_pages=max(1, (total + per_page - 1) // per_page),
        has_next=start + per_page < total, has_previous=page > 1,
    )
    return APIResponse(data={"manga_slug": slug, "items": page_items, "total": total}, meta=meta(request, url, cached), pagination=pagination)


@router.get("/manga/{slug}/recommendations", tags=["Manga"], response_model=APIResponse[RecommendationData], summary="Get related manga recommendations")
async def manga_recommendations(request: Request, slug: str):
    await _ensure_manga_visible(slug)
    html, url, cached = await fetch_or_502(f"/title/{slug}")
    detail = await run_in_threadpool(parse_manga_detail, html, url)
    items = await _apply_card_overrides(detail.get("recommendations", []))
    return APIResponse(data={"manga_slug": slug, "items": items}, meta=meta(request, url, cached))


@router.get("/manga/{slug}/chapters/{chapter_number}", tags=["Reader"], response_model=APIResponse[ReaderData], summary="Get reader pages for a chapter")
async def chapter_reader(request: Request, slug: str, chapter_number: str, user=Depends(optional_user)):
    await _ensure_manga_visible(slug)
    if not chapter_number.replace(".", "", 1).isdigit():
        raise HTTPException(status_code=400, detail="Chapter number must be numeric.")
    try:
        hidden = await get_database().chapter_overrides.find_one({"manga_slug": slug, "chapter_number": _chapter_key(chapter_number), "hidden": True})
        if hidden:
            raise HTTPException(status_code=404, detail="Chapter not found.")
    except HTTPException:
        raise
    except Exception:
        logger.warning("Could not verify chapter visibility override.", exc_info=True)
    path = f"/title/{slug}/chapter/{chapter_number}"
    raw, payload, data_url, cached = await fetch_source_data({"id": slug, "chapter": chapter_number})
    if isinstance(raw, dict):
        data = normalize_source_reader(raw, f"{settings.source_base_url.rstrip('/')}{path}", slug, chapter_number)
        if data["pages"]:
            if user:
                await record_chapter_read(str(user["_id"]), slug, data.get("manga_title") or slug,
                                          chapter_number, data.get("chapter_title"))
            return APIResponse(data=data, meta=meta(request, data_url, cached))
    html, url, html_cached = await fetch_or_502(path)
    data = await run_in_threadpool(parse_reader, html, url, slug, chapter_number)
    if not data["pages"]:
        raise HTTPException(status_code=502, detail="No reader images were found on the source page.")
    if user:
        await record_chapter_read(str(user["_id"]), slug, data.get("manga_title") or slug,
                                  chapter_number, data.get("chapter_title"))
    return APIResponse(data=data, meta=meta(request, url, html_cached))
