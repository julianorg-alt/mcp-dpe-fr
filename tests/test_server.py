"""End-to-end through the real MCP protocol (in-memory), upstream replayed from a cassette."""

import json
import re
from contextlib import asynccontextmanager
from pathlib import Path

from mcp.shared.memory import create_connected_server_and_client_session

from mcp_dpe_fr.cassette import ReplayTransport
from mcp_dpe_fr.client import DataFairClient
from mcp_dpe_fr.server import build_server

CASSETTE = Path(__file__).parent / "cassettes" / "eval.json"
FORBIDDEN = re.compile(
    r"adresse|numero_dpe|coordonnee|nom_rue|numero_voie|identifiant_ban|nom_residence"
    r"|complement_adresse",
    re.I,
)


@asynccontextmanager
async def open_session():
    client = DataFairClient(transport=ReplayTransport(CASSETTE), rate=100, burst=100)
    try:
        async with create_connected_server_and_client_session(
            build_server(client)._mcp_server
        ) as session:
            yield session
    finally:
        await client.aclose()


async def test_tool_catalogue():
    async with open_session() as s:
        tools = (await s.list_tools()).tools
    assert 4 <= len(tools) <= 6
    for t in tools:
        assert t.annotations.readOnlyHint is True and t.annotations.destructiveHint is False
        assert len(t.description) > 80, t.name
        assert not FORBIDDEN.search(json.dumps(t.inputSchema)), t.name


async def test_call_returns_structured_result():
    async with open_session() as s:
        r = await s.call_tool("dpe_label_distribution", {"department": "75"})
    assert not r.isError
    data = r.structuredContent
    assert data["labels"][0]["label"] == "A" and data["labels"][0]["count"] == 1065
    assert "Licence Ouverte" in data["source"]["licence"]


async def test_validation_error_is_a_tool_error_with_guidance():
    async with open_session() as s:
        r = await s.call_tool("dpe_label_distribution", {"department": "99"})
    assert r.isError and "Unknown department" in r.content[0].text


async def test_too_small_scope_is_refused():
    async with open_session() as s:
        r = await s.call_tool(
            "dpe_label_distribution", {"department": "75", "construction_year_min": 2027}
        )
    assert r.isError and "at least 10" in r.content[0].text


async def test_upstream_failure_is_a_clean_tool_error():
    # This query is not in the cassette: the replay transport answers 599.
    async with open_session() as s:
        r = await s.call_tool("dpe_label_distribution", {"department": "33", "climate_zone": "H3"})
    text = r.content[0].text
    assert r.isError and "ADEME API error" in text and "Traceback" not in text


def test_recorded_traffic_never_touches_personal_fields():
    # Every upstream call made by the tools (and by the references) is in the cassette.
    keys = list(json.loads(CASSETTE.read_text("utf-8")))
    assert keys and not any(FORBIDDEN.search(k) for k in keys)
    assert not any("/lines?" in k and "select" in k for k in keys)
