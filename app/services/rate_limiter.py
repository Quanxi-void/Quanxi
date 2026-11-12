"""MongoDB-backed shared token-bucket rate limiting for ManhwaHQ API.

Every deployment must use the same MongoDB database to share counters. The
MongoDB update pipeline is atomic and uses MongoDB's $$NOW clock, so requests
from Render and Ubuntu share one token bucket without relying on app clocks.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import Request

from app.core.config import settings
from app.services.database import get_database

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RateLimitPolicy:
    name: str
    limit: int
    window_seconds: int
    scope: str = "actor"  # actor or ip


@dataclass(frozen=True)
class RateLimitResult:
    allowed: bool
    limit: int | None = None
    remaining: int | None = None
    reset_seconds: int | None = None
    retry_after: int | None = None
    policy: str | None = None
    unavailable: bool = False
    degraded: bool = False


class MongoRateLimiter:
    """Atomic MongoDB token bucket, shared by all app instances using one DB."""

    def __init__(self) -> None:
        self._collection = None
        self.available = False

    async def connect(self) -> None:
        if not settings.rate_limit_enabled:
            self.available = False
            logger.info("Rate limiting is disabled by configuration.")
            return

        try:
            database = get_database()
            self._collection = database[settings.rate_limit_collection]
            # TTL cleanup is asynchronous in MongoDB. The limiter itself also
            # handles expired buckets, so TTL lag cannot affect correctness.
            await self._collection.create_index(
                "expires_at", expireAfterSeconds=0, name="rate_limit_expiry"
            )
            self.available = True
            logger.info(
                "MongoDB rate limiter ready (database=%s, collection=%s).",
                settings.mongodb_database,
                settings.rate_limit_collection,
            )
        except Exception as exc:
            self._collection = None
            self.available = False
            logger.warning("MongoDB rate limiter is unavailable: %s", exc)
            if not settings.rate_limit_fail_open:
                raise RuntimeError(
                    "MongoDB rate limiting is required but unavailable. "
                    "Configure MONGODB_URI and check the MongoDB connection."
                ) from exc

    async def close(self) -> None:
        # The MongoDB client belongs to app.services.database; do not close it here.
        self._collection = None
        self.available = False

    @staticmethod
    def _digest(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8", errors="ignore")).hexdigest()[:32]

    @staticmethod
    def _client_ip(request: Request) -> str:
        # Use the ASGI client address. Trust forwarded headers only when Uvicorn
        # is explicitly configured with the actual proxy addresses.
        return request.client.host if request.client and request.client.host else "unknown"

    @staticmethod
    def _actor(request: Request, ip: str) -> str:
        authorization = request.headers.get("authorization", "")
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() == "bearer" and token:
            try:
                import jwt
                from app.auth.security import decode_token

                claims = decode_token(token.strip(), "access")
                subject = claims.get("sub")
                if isinstance(subject, str) and subject:
                    return f"user:{subject}"
            except (jwt.PyJWTError, ValueError, TypeError):
                pass
        return f"ip:{ip}"

    def policies_for(self, request: Request) -> list[RateLimitPolicy]:
        """Choose one effective bucket per request to minimize database round trips.

        Authenticated traffic is keyed by user ID; anonymous traffic is keyed by
        client IP. Authentication endpoints always use IP-scoped protection.
        """
        if not settings.rate_limit_enabled:
            return []
        path = request.url.path
        prefix = settings.api_prefix.rstrip("/")
        if not path.startswith(prefix + "/"):
            return []
        if path == f"{prefix}/health":
            return []
        if path.startswith(f"{prefix}/static/avatars/") or path.startswith(f"{prefix}/uploads/avatars/"):
            return []
        if request.method.upper() == "OPTIONS":
            return []

        method = request.method.upper()
        if path == f"{prefix}/auth/login" and method == "POST":
            return [RateLimitPolicy(
                "login-ip", settings.rate_limit_login_per_5_minutes, 300, "ip"
            )]
        if path == f"{prefix}/auth/register" and method == "POST":
            return [RateLimitPolicy(
                "register-ip", settings.rate_limit_register_per_hour, 3600, "ip"
            )]
        if path == f"{prefix}/auth/refresh" and method == "POST":
            return [RateLimitPolicy(
                "refresh-ip", settings.rate_limit_refresh_per_minute, 60, "ip"
            )]

        if path.startswith(f"{prefix}/manga/") and "/chapters/" in path and method == "GET":
            return [RateLimitPolicy(
                "reader-actor", settings.rate_limit_reader_per_minute, 60, "actor"
            )]
        if path in {f"{prefix}/search", f"{prefix}/browse"} or path.startswith(f"{prefix}/genres"):
            return [RateLimitPolicy(
                "discovery-actor", settings.rate_limit_discovery_per_minute, 60, "actor"
            )]
        if path.startswith(f"{prefix}/users/"):
            return [RateLimitPolicy(
                "users-actor", settings.rate_limit_user_per_minute, 60, "actor"
            )]
        return [RateLimitPolicy(
            "default-actor", settings.rate_limit_default_per_minute, 60, "actor"
        )]

    @staticmethod
    def _token_bucket_pipeline(limit: int, window_ms: int) -> list[dict[str, Any]]:
        """Build an atomic update pipeline using MongoDB server time.

        The bucket starts full, refills continuously at limit/window, and holds
        at most `limit` tokens. Each request atomically consumes one token.
        """
        missing_last_refill = {"$eq": [{"$type": "$last_refill_at"}, "missing"]}
        elapsed_ms = {
            "$cond": [
                missing_last_refill,
                0,
                {"$max": [0, {"$subtract": ["$$NOW", "$last_refill_at"]}]},
            ]
        }
        tokens_before = {
            "$min": [
                limit,
                {
                    "$add": [
                        {"$ifNull": ["$tokens", limit]},
                        {"$multiply": [{"$divide": [limit, window_ms]}, elapsed_ms]},
                    ]
                },
            ]
        }
        enough_tokens = {"$gte": ["$_tokens_before", 1]}
        retry_ms = {
            "$max": [
                1,
                {
                    "$ceil": {
                        "$multiply": [
                            {"$subtract": [1, "$_tokens_before"]},
                            {"$divide": [window_ms, limit]},
                        ]
                    }
                },
            ]
        }
        return [
            {
                "$set": {
                    "_tokens_before": tokens_before,
                }
            },
            {
                "$set": {
                    "last_allowed": enough_tokens,
                    "tokens": {
                        "$cond": [
                            enough_tokens,
                            {"$max": [0, {"$subtract": ["$_tokens_before", 1]}]},
                            "$_tokens_before",
                        ]
                    },
                    "retry_after_ms": {"$cond": [enough_tokens, 0, retry_ms]},
                    "last_refill_at": "$$NOW",
                    "expires_at": {
                        "$dateAdd": {
                            "startDate": "$$NOW",
                            "unit": "millisecond",
                            "amount": window_ms * 2,
                        }
                    },
                }
            },
            {"$unset": "_tokens_before"},
        ]

    async def _consume(self, key: str, policy: RateLimitPolicy) -> dict[str, Any]:
        if self._collection is None:
            raise RuntimeError("MongoDB rate-limit collection is not initialized.")
        window_ms = policy.window_seconds * 1000
        pipeline = self._token_bucket_pipeline(policy.limit, window_ms)
        query = {"_id": key}

        async def update(upsert: bool):
            operation = self._collection.find_one_and_update(
                query,
                pipeline,
                upsert=upsert,
                return_document=True,
            )
            return await asyncio.wait_for(
                operation, timeout=settings.rate_limit_operation_timeout
            )

        try:
            document = await update(upsert=True)
        except Exception as exc:
            # Two instances can race to create the same bucket. The _id index
            # resolves that race; retry the atomic update against the winner.
            if getattr(exc, "code", None) != 11000:
                raise
            document = await update(upsert=False)
            if document is None:
                document = await update(upsert=True)
        if document is None:
            raise RuntimeError("MongoDB did not return the updated rate-limit bucket.")
        return document

    async def check(self, request: Request) -> RateLimitResult:
        policies = self.policies_for(request)
        if not policies:
            return RateLimitResult(allowed=True)

        if self._collection is None:
            if settings.rate_limit_fail_open:
                return RateLimitResult(allowed=True, degraded=True)
            return RateLimitResult(allowed=False, unavailable=True)

        ip = self._client_ip(request)
        actor = self._actor(request, ip)
        tightest: RateLimitResult | None = None

        try:
            for policy in policies:
                identity = ip if policy.scope == "ip" else actor
                key = (
                    f"{settings.rate_limit_key_prefix}:v1:{policy.name}:"
                    f"{self._digest(identity)}"
                )
                document = await self._consume(key, policy)
                tokens = max(0.0, min(float(policy.limit), float(document.get("tokens", 0))))
                remaining = max(0, math.floor(tokens))
                allowed = bool(document.get("last_allowed", False))
                retry_ms = max(0, int(document.get("retry_after_ms", 0) or 0))
                reset_ms = (
                    retry_ms
                    if not allowed
                    else math.ceil((policy.limit - tokens) * (policy.window_seconds * 1000) / policy.limit)
                )
                reset_seconds = max(1, math.ceil(reset_ms / 1000))
                current = RateLimitResult(
                    allowed=allowed,
                    limit=policy.limit,
                    remaining=remaining,
                    reset_seconds=reset_seconds,
                    retry_after=reset_seconds if not allowed else None,
                    policy=policy.name,
                )
                if not current.allowed:
                    return current
                if tightest is None or (
                    (current.remaining or 0) / max(current.limit or 1, 1)
                    < (tightest.remaining or 0) / max(tightest.limit or 1, 1)
                ):
                    tightest = current
            self.available = True
            return tightest or RateLimitResult(allowed=True)
        except Exception as exc:
            self.available = False
            logger.warning("MongoDB rate-limit check failed (%s): %r", type(exc).__name__, exc, exc_info=True)
            if settings.rate_limit_fail_open:
                return RateLimitResult(allowed=True, degraded=True)
            return RateLimitResult(allowed=False, unavailable=True)


rate_limiter = MongoRateLimiter()
