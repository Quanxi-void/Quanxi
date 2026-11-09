from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

from app.main import app
from app.services.rate_limiter import MongoRateLimiter, RateLimitPolicy, RateLimitResult, rate_limiter


def make_request(path: str, method: str = "GET", client_ip: str = "127.0.0.1"):
    from starlette.requests import Request

    return Request({
        "type": "http", "method": method, "path": path, "headers": [],
        "query_string": b"", "server": ("testserver", 80),
        "client": (client_ip, 1234), "scheme": "http",
    })


def test_authentication_routes_have_strict_ip_policies():
    policies = rate_limiter.policies_for(make_request("/api/v1/auth/login", "POST"))
    assert [policy.name for policy in policies] == ["login-ip"]
    assert policies[-1].limit == 5
    assert policies[-1].window_seconds == 300
    assert policies[-1].scope == "ip"



def test_discovery_route_uses_one_actor_bucket_not_global_plus_actor():
    policies = rate_limiter.policies_for(make_request("/api/v1/search"))
    assert len(policies) == 1
    assert policies[0].name == "discovery-actor"
    assert policies[0].scope == "actor"


def test_reader_route_uses_its_own_policy():
    policies = rate_limiter.policies_for(make_request("/api/v1/manga/eleceed/chapters/42"))
    assert policies[-1].name == "reader-actor"
    assert policies[-1].limit == 30


def test_health_endpoint_is_not_rate_limited():
    assert rate_limiter.policies_for(make_request("/api/v1/health")) == []


def test_static_assets_and_cors_preflight_are_exempt():
    assert rate_limiter.policies_for(make_request("/api/v1/static/avatars/avatar.jpg")) == []
    assert rate_limiter.policies_for(make_request("/api/v1/uploads/avatars/user.webp")) == []
    assert rate_limiter.policies_for(make_request("/api/v1/home", "OPTIONS")) == []


def test_token_bucket_pipeline_uses_mongo_server_clock_and_ttl():
    pipeline = MongoRateLimiter._token_bucket_pipeline(limit=10, window_ms=60_000)
    serialized = repr(pipeline)
    assert "$$NOW" in serialized
    assert "$dateAdd" in serialized
    assert "$divide" in serialized
    assert "$min" in serialized
    assert "last_allowed" in serialized
    assert "expires_at" in serialized


class FakeCollection:
    def __init__(self):
        self.index_calls = []
        self.update_calls = []

    async def create_index(self, *args, **kwargs):
        self.index_calls.append((args, kwargs))

    async def find_one_and_update(self, query, pipeline, *, upsert, return_document):
        self.update_calls.append((query, pipeline, upsert, return_document))
        # Simulate a successful atomic MongoDB token-bucket result.
        return {
            "_id": query["_id"],
            "tokens": 4.0,
            "last_allowed": True,
            "retry_after_ms": 0,
        }


class FakeDatabase:
    def __init__(self, collection):
        self.collection = collection

    def __getitem__(self, name):
        assert name == "rate_limits"
        return self.collection


async def test_connect_initializes_shared_mongodb_collection(monkeypatch):
    import app.services.rate_limiter as module

    collection = FakeCollection()
    monkeypatch.setattr(module, "get_database", lambda: FakeDatabase(collection))
    limiter = MongoRateLimiter()
    await limiter.connect()

    assert limiter.available is True
    assert limiter._collection is collection
    assert collection.index_calls[0][0] == ("expires_at",)
    assert collection.index_calls[0][1]["expireAfterSeconds"] == 0
    await limiter.close()
    assert limiter._collection is None


async def test_check_uses_atomic_mongodb_updates_for_shared_keys(monkeypatch):
    collection = FakeCollection()
    limiter = MongoRateLimiter()
    limiter._collection = collection
    limiter.available = True

    result = await limiter.check(make_request("/api/v1/home", client_ip="203.0.113.10"))

    assert result.allowed is True
    assert result.limit is not None
    assert len(collection.update_calls) == 1  # one effective policy per request
    assert all(call[2] is True for call in collection.update_calls)
    assert all(call[3] is True for call in collection.update_calls)
    assert "default-actor" in collection.update_calls[0][0]["_id"]


async def test_missing_mongodb_fails_open_or_closed(monkeypatch):
    limiter = MongoRateLimiter()
    limiter._collection = None
    monkeypatch.setattr("app.services.rate_limiter.settings.rate_limit_fail_open", True)
    opened = await limiter.check(make_request("/api/v1/home"))
    assert opened.allowed is True and opened.degraded is True

    monkeypatch.setattr("app.services.rate_limiter.settings.rate_limit_fail_open", False)
    closed = await limiter.check(make_request("/api/v1/home"))
    assert closed.allowed is False and closed.unavailable is True


def test_rate_limited_response_uses_standard_error_envelope(monkeypatch):
    monkeypatch.setattr(
        rate_limiter,
        "check",
        AsyncMock(return_value=RateLimitResult(
            allowed=False, limit=5, remaining=0, reset_seconds=42,
            retry_after=42, policy="login-ip",
        )),
    )
    with TestClient(app) as client:
        response = client.get("/api/v1/home")

    assert response.status_code == 429
    assert response.headers["retry-after"] == "42"
    payload = response.json()
    assert payload["success"] is False
    assert payload["error"]["code"] == "RATE_LIMIT_EXCEEDED"
    assert payload["error"]["details"]["policy"] == "login-ip"
    assert response.headers["x-ratelimit-limit"] == "5"


def test_rate_limiter_unavailable_can_fail_closed(monkeypatch):
    monkeypatch.setattr(
        rate_limiter,
        "check",
        AsyncMock(return_value=RateLimitResult(allowed=False, unavailable=True)),
    )
    with TestClient(app) as client:
        response = client.get("/api/v1/home")

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "RATE_LIMITER_UNAVAILABLE"
