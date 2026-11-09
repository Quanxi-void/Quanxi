import asyncio
import httpx
import pytest

from app.services.cache import MemoryCache
from app.scraper.client import SourceClient, SourceFetchError


def test_memory_cache_reports_and_enforces_budgets():
    cache = MemoryCache()
    cache.clear()
    cache.set("one", "hello")
    assert cache.get("one") == "hello"
    stats = cache.stats()
    assert stats["items"] == 1
    assert stats["bytes"] >= 5
    assert stats["bytes"] <= stats["max_bytes"]


async def test_source_client_coalesces_concurrent_requests_for_same_key():
    client = SourceClient()
    calls = 0

    async def fetch():
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.02)
        return "shared-result"

    results = await asyncio.gather(
        *(client._coalesced("same-resource", fetch) for _ in range(20))
    )
    assert results == ["shared-result"] * 20
    assert calls == 1


async def test_source_client_releases_coalescing_lock_after_fetch():
    client = SourceClient()
    first = await client._coalesced("resource", lambda: asyncio.sleep(0, result="first"))
    second = await client._coalesced("resource", lambda: asyncio.sleep(0, result="second"))
    assert first == "first"
    assert second == "second"
    await asyncio.sleep(0)
    assert "resource" not in client._inflight



async def test_source_client_stops_retries_and_cools_down_after_upstream_429():
    calls = 0

    async def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(429, headers={"Retry-After": "30"}, request=request)

    client = SourceClient()
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(SourceFetchError):
            await client.get_text("/some-page", use_cache=False)
        assert calls == 1
        with pytest.raises(SourceFetchError):
            await client.get_text("/another-page", use_cache=False)
        assert calls == 1
    finally:
        await client.close()
