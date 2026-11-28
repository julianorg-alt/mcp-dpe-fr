import time

import httpx
import pytest

from mcp_dpe_fr.client import DataFairClient
from mcp_dpe_fr.errors import InvalidInput, UpstreamError


def make(handler, **kw):
    kw.setdefault("backoff", 0.0)
    return DataFairClient("https://example.test/api", transport=httpx.MockTransport(handler), **kw)


async def test_retries_503_then_succeeds():
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(503) if len(calls) < 3 else httpx.Response(200, json={"ok": 1})

    c = make(handler, retries=2)
    assert await c.get("values_agg", {"a": 1}) == {"ok": 1}
    assert len(calls) == 3


async def test_gives_up_with_actionable_message():
    c = make(lambda req: httpx.Response(503), retries=1)
    with pytest.raises(UpstreamError, match="unavailable after 2 attempts"):
        await c.get("x", {})
    assert c.requests_sent == 2


async def test_429_honours_retry_after():
    calls = []

    def handler(req):
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "0"})
        return httpx.Response(200, json={"ok": True})

    assert await make(handler).get("x", {}) == {"ok": True}


async def test_400_is_not_retried_and_maps_to_invalid_input():
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(400, json={"error": "unknown field"})

    with pytest.raises(InvalidInput, match="unknown field"):
        await make(handler).get("x", {})
    assert len(calls) == 1


async def test_404_is_an_upstream_error_without_retry():
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(404, text="nope")

    with pytest.raises(UpstreamError, match="HTTP 404"):
        await make(handler).get("x", {})
    assert len(calls) == 1


async def test_network_error_is_retried():
    calls = []

    def handler(req):
        calls.append(1)
        if len(calls) == 1:
            raise httpx.ConnectError("boom")
        return httpx.Response(200, json={"ok": True})

    assert await make(handler).get("x", {}) == {"ok": True}


async def test_cache_avoids_second_request_and_expires():
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(200, json={"n": len(calls)})

    c = make(handler, cache_ttl=0.2)
    assert (await c.get("x", {"a": 1, "b": 2}))["n"] == 1
    assert (await c.get("x", {"b": 2, "a": 1}))["n"] == 1  # same params, other order
    time.sleep(0.25)
    assert (await c.get("x", {"a": 1, "b": 2}))["n"] == 2


async def test_client_side_rate_limit_spaces_requests():
    c = make(lambda req: httpx.Response(200, json={}), rate=20, burst=1, cache_ttl=0)
    t0 = time.monotonic()
    for i in range(4):
        await c.get("x", {"i": i})
    assert time.monotonic() - t0 >= 3 / 20 - 0.01  # first is free, three wait 50 ms each
