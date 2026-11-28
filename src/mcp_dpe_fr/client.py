"""Async client for the ADEME data-fair API: rate limit, retries, TTL cache, typed errors."""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from typing import Any

import httpx

from .errors import InvalidInput, UpstreamError

BASE_URL = "https://data.ademe.fr/data-fair/api/v1/datasets/dpe03existant"
RETRY_STATUS = {429, 502, 503, 504}


class TokenBucket:
    """Client-side rate limit: `rate` requests per second, bursts up to `burst`."""

    def __init__(self, rate: float, burst: int):
        self.rate, self.capacity = rate, float(burst)
        self.tokens, self.updated = float(burst), time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
                self.updated = now
                if self.tokens >= 1:
                    self.tokens -= 1
                    return
                await asyncio.sleep((1 - self.tokens) / self.rate)


class DataFairClient:
    def __init__(
        self,
        base_url: str = BASE_URL,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        rate: float = 5.0,
        burst: int = 5,
        timeout: float = 20.0,
        retries: int = 2,
        backoff: float = 0.5,
        cache_ttl: float = 300.0,
        cache_size: int = 256,
        max_concurrency: int = 4,
    ):
        self.base_url = base_url.rstrip("/")
        self._http = httpx.AsyncClient(
            transport=transport,
            timeout=timeout,
            headers={"User-Agent": "mcp-dpe-fr/0.1 (+open-data MCP server)"},
        )
        self._bucket = TokenBucket(rate, burst)
        self._sem = asyncio.Semaphore(max_concurrency)
        self._retries, self._backoff = retries, backoff
        self._ttl, self._cache_size = cache_ttl, cache_size
        self._cache: OrderedDict[tuple, tuple[float, Any]] = OrderedDict()
        self.requests_sent = 0

    async def aclose(self) -> None:
        await self._http.aclose()

    async def get(self, endpoint: str, params: dict[str, Any]) -> dict[str, Any]:
        key = (endpoint, tuple(sorted((k, str(v)) for k, v in params.items())))
        hit = self._cache.get(key)
        if hit and time.monotonic() - hit[0] < self._ttl:
            self._cache.move_to_end(key)
            return hit[1]
        data = await self._fetch(endpoint, params)
        self._cache[key] = (time.monotonic(), data)
        while len(self._cache) > self._cache_size:
            self._cache.popitem(last=False)
        return data

    async def _fetch(self, endpoint: str, params: dict[str, Any]) -> dict[str, Any]:
        url = f"{self.base_url}/{endpoint}" if endpoint else self.base_url
        last = "unknown error"
        for attempt in range(self._retries + 1):
            await self._bucket.acquire()
            try:
                async with self._sem:
                    self.requests_sent += 1
                    r = await self._http.get(url, params=params)
            except httpx.TransportError as e:
                last = f"network error ({type(e).__name__})"
            else:
                if r.status_code == 200:
                    return r.json()
                if r.status_code == 400:
                    raise InvalidInput(f"The ADEME API rejected the query: {_detail(r)}")
                last = f"HTTP {r.status_code}"
                if r.status_code not in RETRY_STATUS:
                    raise UpstreamError(f"ADEME API error ({last}): {_detail(r)}")
                retry_after = r.headers.get("Retry-After", "")
                if retry_after.isdigit():
                    await asyncio.sleep(min(float(retry_after), 10.0))
                    continue
            if attempt < self._retries:
                await asyncio.sleep(self._backoff * 2**attempt)
        raise UpstreamError(
            f"The ADEME API is unavailable after {self._retries + 1} attempts ({last}). "
            "Retry in a minute; do not loop."
        )


def _detail(r: httpx.Response) -> str:
    try:
        body = r.json()
        return str(body.get("error") or body.get("message") or body)[:300]
    except ValueError:
        return r.text[:300]
