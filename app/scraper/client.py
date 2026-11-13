from __future__ import annotations

import asyncio
import hashlib
import json
import random
import time
import logging
from urllib.parse import urljoin, urlparse, urlencode

import httpx

from app.core.config import settings
from app.services.cache import cache

logger = logging.getLogger(__name__)

class SourceFetchError(Exception):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class SourceClient:
    """Reusable HTTP client with bounded upstream concurrency and cache coalescing."""

    def __init__(self):
        self._client: httpx.AsyncClient | None = None
        self._semaphore = asyncio.Semaphore(settings.source_max_concurrent)
        self._inflight: dict[str, asyncio.Task] = {}
        self._inflight_guard = asyncio.Lock()
        self._cooldown_until = 0.0

    async def start(self):
        if self._client is None:
            limits = httpx.Limits(
                max_connections=settings.source_connection_limit,
                max_keepalive_connections=settings.source_keepalive_limit,
            )
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(settings.source_timeout, connect=min(5.0, settings.source_timeout), pool=5.0),
                limits=limits,
                follow_redirects=True,
                headers={
                    "User-Agent": settings.source_user_agent,
                    "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
                    "Accept-Language": "en-US,en;q=0.9",
                },
            )

    async def close(self):
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        async with self._inflight_guard:
            self._inflight.clear()

    def normalize_page_url(self, path_or_url: str) -> str:
        base = settings.source_base_url.rstrip("/") + "/"
        url = urljoin(base, path_or_url.lstrip("/") if not path_or_url.startswith("http") else path_or_url)
        parsed = urlparse(url)
        base_host = urlparse(settings.source_base_url).hostname
        if parsed.scheme not in {"http", "https"} or parsed.hostname != base_host:
            raise ValueError("Only pages on the configured ManhwaHQ source host are allowed.")
        return url

    async def _forget_inflight(self, key: str, task: asyncio.Task) -> None:
        async with self._inflight_guard:
            if self._inflight.get(key) is task:
                self._inflight.pop(key, None)

    async def _coalesced(self, key: str, fetcher):
        """Share one in-flight fetch for a key among requests in this worker."""
        async with self._inflight_guard:
            task = self._inflight.get(key)
            if task is None:
                task = asyncio.create_task(fetcher())
                self._inflight[key] = task

                def cleanup(completed: asyncio.Task):
                    # Consume an unobserved exception if every waiter disconnected.
                    if not completed.cancelled():
                        try:
                            completed.exception()
                        except Exception:
                            pass
                    asyncio.create_task(self._forget_inflight(key, completed))

                task.add_done_callback(cleanup)
        return await asyncio.shield(task)

    def _raise_if_upstream_cooling_down(self) -> None:
        if time.monotonic() < self._cooldown_until:
            raise SourceFetchError(
                "The manga source is temporarily cooling down after rejecting requests.",
                429,
            )

    def _set_upstream_cooldown(self, seconds: float = 45.0) -> None:
        self._cooldown_until = max(self._cooldown_until, time.monotonic() + seconds)

    @staticmethod
    def _retry_delay(attempt: int) -> float:
        return min(4.0, 0.4 * (2 ** attempt)) + random.uniform(0.0, 0.2)

    async def get_json(self, endpoint: str, params: dict | None = None, use_cache: bool = True):
        base = settings.source_data_api_base_url.rstrip("/")
        url = base + endpoint
        parsed = urlparse(url)
        allowed_host = urlparse(settings.source_data_api_base_url).hostname
        if parsed.scheme != "https" or parsed.hostname != allowed_host:
            raise ValueError("The source data API URL is outside the configured backend host.")
        query = urlencode(params or {}, doseq=True)
        full_url = url + ("?" + query if query else "")
        key = "json:" + hashlib.sha256(full_url.encode()).hexdigest()

        async def fetch():
            if use_cache:
                cached = cache.get(key)
                if cached is not None:
                    return cached, full_url, True
            self._raise_if_upstream_cooling_down()
            if self._client is None:
                await self.start()
            last_error = None
            for attempt in range(settings.source_max_retries + 1):
                try:
                    async with self._semaphore:
                        response = await self._client.get(full_url, headers={"Accept": "application/json"})
                    if response.status_code in (403, 429):
                        self._set_upstream_cooldown(60.0 if response.status_code == 429 else 90.0)
                        raise SourceFetchError(
                            f"Upstream source rejected the request (HTTP {response.status_code}).",
                            response.status_code,
                        )
                    response.raise_for_status()
                    payload = response.json()
                    if use_cache:
                        cache.set(key, payload)
                    return payload, full_url, False
                except SourceFetchError:
                    raise
                except (httpx.HTTPError, ValueError, json.JSONDecodeError) as exc:
                    last_error = exc
                    if attempt < settings.source_max_retries:
                        await asyncio.sleep(self._retry_delay(attempt))
            return None, full_url, False

        if not use_cache:
            return await fetch()
        cached = cache.get(key)
        if cached is not None:
            return cached, full_url, True
        return await self._coalesced(key, fetch)

    async def get_text(self, path_or_url: str, use_cache: bool = True) -> tuple[str, str, bool]:
        url = self.normalize_page_url(path_or_url)
        key = "html:" + hashlib.sha256(url.encode()).hexdigest()

        async def fetch():
            if use_cache:
                cached = cache.get(key)
                if cached is not None:
                    return cached, url, True
            self._raise_if_upstream_cooling_down()
            if self._client is None:
                await self.start()

            last_error = None
            for attempt in range(settings.source_max_retries + 1):
                try:
                    async with self._semaphore:
                        response = await self._client.get(url)
                    if response.status_code == 404:
                        raise SourceFetchError("The requested page was not found.", 404)
                    if response.status_code in (403, 429):
                        self._set_upstream_cooldown(60.0 if response.status_code == 429 else 90.0)
                        raise SourceFetchError(
                            f"Upstream source rejected the request (HTTP {response.status_code}).",
                            response.status_code,
                        )
                    response.raise_for_status()
                    content_type = response.headers.get("content-type", "")
                    if "text/html" not in content_type and not response.text.lstrip().lower().startswith("<"):
                        raise SourceFetchError("The source did not return an HTML document.")
                    html = response.text
                    if use_cache:
                        cache.set(key, html)
                    return html, str(response.url), False
                except SourceFetchError:
                    raise
                except (httpx.HTTPError, OSError) as exc:
                    last_error = exc
                    logger.warning(
                        "SOURCE_REQUEST_ATTEMPT_FAILED | "
                        "url=%s | attempt=%s/%s | "
                        "exception=%s | error=%r",
                        url,
                        attempt + 1,
                        settings.source_max_retries + 1,
                        type(exc).__name__,
                        exc,
                    )
                    if attempt < settings.source_max_retries:
                        await asyncio.sleep(self._retry_delay(attempt))

            if settings.enable_browser_fallback:
                try:
                    html = await self._fetch_with_browser(url)
                    if use_cache:
                        cache.set(key, html)
                    return html, url, False
                except Exception as browser_error:
                    raise SourceFetchError(
                        f"HTTP fetch failed ({last_error}); optional browser fallback failed ({browser_error}). "
                        "Install the browser extra and Chromium if browser fallback is enabled."
                    ) from browser_error
            raise SourceFetchError(
                f"Unable to fetch source page. "
                f"Exception={type(last_error).__name__ if last_error else 'Unknown'}; "
                f"Details={last_error!r}"
            ) from last_error

        if not use_cache:
            return await fetch()
        cached = cache.get(key)
        if cached is not None:
            return cached, url, True
        return await self._coalesced(key, fetch)

    async def _fetch_with_browser(self, url: str) -> str:
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise RuntimeError("Playwright is not installed. Install requirements-browser.txt.") from exc
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page(user_agent=settings.source_user_agent)
                response = await page.goto(url, wait_until="domcontentloaded", timeout=int(settings.source_timeout * 1000))
                if response and response.status == 404:
                    raise SourceFetchError("The requested page was not found.", 404)
                if response and response.status in (403, 429):
                    self._set_upstream_cooldown(60.0 if response.status == 429 else 90.0)
                    raise SourceFetchError(
                        f"Upstream source rejected the request (HTTP {response.status}).",
                        response.status,
                    )
                await page.wait_for_timeout(900)
                return await page.content()
            finally:
                await browser.close()


source_client = SourceClient()
