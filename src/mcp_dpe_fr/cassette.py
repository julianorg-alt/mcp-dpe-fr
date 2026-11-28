"""Record / replay of upstream HTTP responses (for offline tests and evals).

Key = path + sorted query string, so the order of parameters does not matter.
A cassette is a plain JSON file: {key: {"status": int, "json": any}}.
"""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import parse_qsl

import httpx


def request_key(request: httpx.Request) -> str:
    params = sorted(parse_qsl(request.url.query.decode(), keep_blank_values=True))
    return request.url.path + "?" + "&".join(f"{k}={v}" for k, v in params)


class ReplayTransport(httpx.AsyncBaseTransport):
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.entries: dict = json.loads(self.path.read_text("utf-8")) if self.path.exists() else {}
        self.misses: list[str] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        key = request_key(request)
        entry = self.entries.get(key)
        if entry is None:
            self.misses.append(key)
            return httpx.Response(599, json={"error": f"no recorded response for {key}"})
        return httpx.Response(entry["status"], json=entry["json"])


class RecordTransport(httpx.AsyncBaseTransport):
    """Forwards to the real API and stores every response. Used by scripts/record_cassette.py."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.entries: dict = json.loads(self.path.read_text("utf-8")) if self.path.exists() else {}
        self._real = httpx.AsyncHTTPTransport()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        response = await self._real.handle_async_request(request)
        await response.aread()
        if response.status_code == 200:
            self.entries[request_key(request)] = {"status": 200, "json": response.json()}
        return response

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        ordered = dict(sorted(self.entries.items()))
        self.path.write_text(json.dumps(ordered, ensure_ascii=False, indent=0) + "\n", "utf-8")
