import hmac
import time
import uuid
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse, FileResponse, Response
from fastapi.staticfiles import StaticFiles
from app.core.config import settings
from app.models.common import APIError
from app.api.routes import router
from app.scraper.client import source_client
from app.services.database import connect_database, close_database
from app.services.rate_limiter import rate_limiter
from app.auth.routes import router as auth_router
from app.users.routes import router as users_router
from pathlib import Path

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await source_client.start()
    try:
        # MongoDB must be connected before the shared rate limiter initializes.
        if settings.mongodb_uri:
            try:
                await connect_database()
            except Exception:
                logger.exception("MongoDB startup connection failed.")
                await close_database()
                if not settings.rate_limit_fail_open:
                    raise
                logger.warning(
                    "Continuing in fail-open mode: account endpoints and MongoDB-backed "
                    "rate limiting will be unavailable until MongoDB reconnects."
                )
        await rate_limiter.connect()
        yield
    finally:
        await rate_limiter.close()
        await source_client.close()
        await close_database()


app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    description=(
        "ManhwaHQ data and account API with MongoDB-backed shared token-bucket "
        "rate limiting, profiles, bookmarks and reading history."
    ),
    # Built-in interactive docs and machine-readable OpenAPI routes are disabled.
    # The public developer portal below documents only the manga-data API.
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=lifespan,
    contact={"name": "ManhwaHQ API"},
    license_info={"name": "Private project"},
)


app.add_middleware(GZipMiddleware, minimum_size=1000, compresslevel=5)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Retry-After", "RateLimit-Limit", "RateLimit-Remaining", "RateLimit-Reset", "X-RateLimit-Limit", "X-RateLimit-Remaining", "X-RateLimit-Reset", "X-RateLimit-Policy", "X-RateLimit-Status", "X-Request-ID"],
)


@app.middleware("http")
async def request_context(request: Request, call_next):
    request.state.request_id = request.headers.get("X-Request-ID", f"req_{uuid.uuid4().hex[:16]}")
    started = time.perf_counter()
    decision = await rate_limiter.check(request)

    if decision.unavailable:
        response = JSONResponse(
            status_code=503,
            content={
                "success": False,
                "data": None,
                "meta": {"request_id": request.state.request_id, "source": "manhwahq.com"},
                "pagination": None,
                "error": {"code": "RATE_LIMITER_UNAVAILABLE", "message": "Request protection is temporarily unavailable. Please retry shortly."},
            },
            headers={"Retry-After": "5"},
        )
    elif not decision.allowed:
        retry_after = max(1, decision.retry_after or 1)
        response = JSONResponse(
            status_code=429,
            content={
                "success": False,
                "data": None,
                "meta": {"request_id": request.state.request_id, "source": "manhwahq.com"},
                "pagination": None,
                "error": {
                    "code": "RATE_LIMIT_EXCEEDED",
                    "message": "Too many requests. Please wait before trying again.",
                    "details": {"policy": decision.policy, "retry_after": retry_after},
                },
            },
            headers={"Retry-After": str(retry_after)},
        )
    else:
        response = await call_next(request)

    if decision.degraded:
        response.headers["X-RateLimit-Status"] = "degraded"
    elif decision.limit is not None:
        response.headers["X-RateLimit-Status"] = "active"
    else:
        response.headers["X-RateLimit-Status"] = "disabled-or-exempt"

    if decision.limit is not None:
        response.headers["RateLimit-Limit"] = str(decision.limit)
        response.headers["RateLimit-Remaining"] = str(decision.remaining or 0)
        response.headers["RateLimit-Reset"] = str(decision.reset_seconds or 1)
        response.headers["X-RateLimit-Limit"] = str(decision.limit)
        response.headers["X-RateLimit-Remaining"] = str(decision.remaining or 0)
        response.headers["X-RateLimit-Reset"] = str(decision.reset_seconds or 1)
        if decision.policy:
            response.headers["X-RateLimit-Policy"] = decision.policy
    response.headers["X-Request-ID"] = request.state.request_id
    response.headers["X-Process-Time"] = f"{time.perf_counter() - started:.4f}"
    return response


_PROXY_OPEN_PATHS = {f"{settings.api_prefix.rstrip('/')}/health", "/"}


@app.middleware("http")
async def require_proxy_secret(request: Request, call_next):
    """Only accept /api/ traffic that carries the Vercel proxy's secret (when PROXY_SECRET is set).

    Registered after request_context, so it runs first and rejected requests never consume rate-limit budget.
    The health endpoint and the root path stay open so uptime monitors can reach them directly.
    """
    secret = settings.proxy_secret
    path = request.url.path
    if (
        secret
        and path.startswith("/api/")
        and path not in _PROXY_OPEN_PATHS
        and request.method.upper() != "OPTIONS"
        and not hmac.compare_digest(request.headers.get("x-proxy-secret", "").encode(), secret.encode())
    ):
        return JSONResponse(
            status_code=403,
            content={
                "success": False,
                "data": None,
                "meta": {"request_id": f"req_{uuid.uuid4().hex[:16]}", "source": "manhwahq.com"},
                "pagination": None,
                "error": {"code": "HTTP_403", "message": "Forbidden"},
            },
        )
    return await call_next(request)


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "success": False,
            "data": None,
            "meta": {"request_id": getattr(request.state, "request_id", "unknown"), "source": "manhwahq.com"},
            "pagination": None,
            "error": {"code": f"HTTP_{exc.status_code}", "message": str(exc.detail)},
        },
        headers=exc.headers,
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    return JSONResponse(
        status_code=422,
        content={
            "success": False,
            "data": None,
            "meta": {"request_id": getattr(request.state, "request_id", "unknown"), "source": "manhwahq.com"},
            "pagination": None,
            "error": {"code": "VALIDATION_ERROR", "message": "Request validation failed.", "details": {"errors": exc.errors()}},
        },
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    return JSONResponse(
        status_code=500,
        content={
            "success": False,
            "data": None,
            "meta": {"request_id": getattr(request.state, "request_id", "unknown"), "source": "manhwahq.com"},
            "pagination": None,
            "error": {"code": "INTERNAL_SERVER_ERROR", "message": "An unexpected server error occurred."},
        },
    )


app.include_router(router, prefix=settings.api_prefix)
app.include_router(auth_router, prefix=settings.api_prefix)
app.include_router(users_router, prefix=settings.api_prefix)

_avatar_dir = Path(settings.avatar_upload_dir)
_avatar_dir.mkdir(parents=True, exist_ok=True)
_preset_dir = Path(__file__).resolve().parent / "static" / "avatars"
app.mount(f"{settings.api_prefix}/static/avatars", StaticFiles(directory=str(_preset_dir)), name="preset-avatars")
app.mount(f"{settings.api_prefix}/uploads/avatars", StaticFiles(directory=str(_avatar_dir)), name="uploaded-avatars")


@app.head("/", include_in_schema=False)
async def root_head():
    """HEAD probe for uptime monitors pointed at the bare domain."""
    return Response(status_code=200)


@app.get("/", tags=["System"], include_in_schema=False)
async def root():
    return {
        "name": settings.app_name,
        "version": settings.app_version,
        "documentation": "/api-docs",
        "api_base": settings.api_prefix,
    }


_docs_dir = Path(__file__).resolve().parent / "static" / "developer_docs"


@app.get("/api-docs", include_in_schema=False)
@app.get("/api-docs/", include_in_schema=False)
async def developer_portal():
    """Serve the custom public manga-data documentation and playground."""
    return FileResponse(_docs_dir / "index.html", media_type="text/html")


# Only the custom portal's CSS and JavaScript are exposed as static assets.
app.mount(
    "/api-docs/assets",
    StaticFiles(directory=str(_docs_dir)),
    name="developer-portal-assets",
)
