"""MCP server (stdio by default, streamable HTTP optional). Six read-only tools."""

from __future__ import annotations

import argparse
import logging
from collections.abc import Awaitable, Callable
from typing import Annotated, Any

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from .client import DataFairClient
from .errors import DpeError
from .filters import BUILDING_TYPES, CLIMATE_ZONES, PERIODS, Filters
from .geo import REGIONS
from .tools import INDICATORS, K_MIN, DpeTools

log = logging.getLogger("mcp_dpe_fr")

INSTRUCTIONS = f"""French DPE (energy performance diagnostics of existing dwellings, ADEME open data,
Licence Ouverte 2.0, DPE issued since July 2021). Aggregated statistics only: no address, no
dwelling-level record is ever returned, and any group under {K_MIN} dwellings is suppressed.
Geography uses INSEE codes: department ('75', '2A', '971'), region ('11'), commune ('33063').
Unsure of a commune code? Call dpe_find_commune first. Always cite the `source` block."""

READ_ONLY = ToolAnnotations(
    readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True
)

Dept = Annotated[
    str | None, Field(description="INSEE department code: '01'-'95', '2A', '2B', '971'-'976'.")
]
Region = Annotated[
    str | None,
    Field(
        description="INSEE region code, e.g. '11' Île-de-France, '84' Auvergne-Rhône-Alpes, '93' PACA."
    ),
]
Commune = Annotated[
    str | None,
    Field(
        description="5-character INSEE commune code, e.g. '33063' (Bordeaux). Get it from dpe_find_commune."
    ),
]
Postal = Annotated[str | None, Field(description="5-digit postal code, e.g. '33000'.")]
Btype = Annotated[str | None, Field(description=f"One of: {', '.join(BUILDING_TYPES)}.")]
Period = Annotated[
    str | None, Field(description=f"Construction period, one of: {'; '.join(PERIODS)}.")
]
YMin = Annotated[int | None, Field(description="Earliest construction year (1700-2030).")]
YMax = Annotated[int | None, Field(description="Latest construction year (1700-2030).")]
Zone = Annotated[
    str | None, Field(description=f"Climate zone, one of: {', '.join(CLIMATE_ZONES)}.")
]
DFrom = Annotated[
    str | None,
    Field(
        description="Only DPE established on/after this date (YYYY-MM-DD). Dataset starts 2021-07-01."
    ),
]
DTo = Annotated[
    str | None, Field(description="Only DPE established on/before this date (YYYY-MM-DD).")
]


def build_server(client: DataFairClient | None = None) -> FastMCP:
    svc = DpeTools(client or DataFairClient())
    mcp = FastMCP("dpe-fr", instructions=INSTRUCTIONS)

    async def run(fn: Callable[[], Awaitable[dict[str, Any]]]) -> dict[str, Any]:
        try:
            return await fn()
        except DpeError as e:
            raise ToolError(str(e)) from None
        except Exception:  # never leak internals to the model
            log.exception("unexpected error")
            raise ToolError("Unexpected internal error; the query was not answered.") from None

    @mcp.tool(name="dpe_find_commune", annotations=READ_ONLY)
    async def dpe_find_commune(
        name: Annotated[
            str, Field(description="Commune name, accents optional (e.g. 'Saint-Etienne').")
        ],
        department: Dept = None,
    ) -> dict[str, Any]:
        """Find the INSEE code of a French commune from its name, with how many DPE it has.
        Use this before any commune-level question. Paris is split into arrondissements
        (75101-75120); use department='75' for the whole city. Returns up to 10 matches ranked
        by number of DPE."""
        return await run(lambda: svc.find_commune(name, department))

    @mcp.tool(name="dpe_label_distribution", annotations=READ_ONLY)
    async def dpe_label_distribution(
        department: Dept = None,
        region: Region = None,
        commune_insee: Commune = None,
        postal_code: Postal = None,
        building_type: Btype = None,
        construction_period: Period = None,
        construction_year_min: YMin = None,
        construction_year_max: YMax = None,
        climate_zone: Zone = None,
        dpe_date_from: DFrom = None,
        dpe_date_to: DTo = None,
    ) -> dict[str, Any]:
        """How many dwellings get each energy label (A to G), plus the share of A-B and of
        F-G ('passoires thermiques'). Use for questions like "what share of homes in Gironde
        are F or G?". At most one of department, region, commune_insee, postal_code; no
        geography means all of France. Counts are DPE issued since July 2021, not the housing stock."""
        f = Filters(
            department,
            region,
            commune_insee,
            postal_code,
            building_type,
            construction_period,
            construction_year_min,
            construction_year_max,
            climate_zone,
            dpe_date_from,
            dpe_date_to,
        )
        return await run(lambda: svc.label_distribution(f))

    @mcp.tool(name="dpe_energy_stats", annotations=READ_ONLY)
    async def dpe_energy_stats(
        indicator: Annotated[
            str,
            Field(
                description=f"One of: {', '.join(INDICATORS)}. primary_energy_kwh_m2 = 5-usage primary energy; ghg_kg_m2 = greenhouse-gas emissions."
            ),
        ],
        department: Dept = None,
        region: Region = None,
        commune_insee: Commune = None,
        postal_code: Postal = None,
        building_type: Btype = None,
        construction_period: Period = None,
        construction_year_min: YMin = None,
        construction_year_max: YMax = None,
        climate_zone: Zone = None,
        dpe_date_from: DFrom = None,
        dpe_date_to: DTo = None,
    ) -> dict[str, Any]:
        """Mean, 10th/50th/90th percentile of an energy, emissions or surface indicator for a
        scope. Use for "what is the median consumption of houses built before 1948 in Lozère?".
        Prefer the median: a few declared values are data-entry outliers."""
        f = Filters(
            department,
            region,
            commune_insee,
            postal_code,
            building_type,
            construction_period,
            construction_year_min,
            construction_year_max,
            climate_zone,
            dpe_date_from,
            dpe_date_to,
        )
        return await run(lambda: svc.energy_stats(f, indicator))

    @mcp.tool(name="dpe_rank_territories", annotations=READ_ONLY)
    async def dpe_rank_territories(
        group_by: Annotated[
            str,
            Field(
                description="'department', 'region' or 'commune'. commune requires `department` and ranks the 200 communes with the most DPE."
            ),
        ],
        metric: Annotated[
            str,
            Field(
                description="'share_fg' (% of F-G labels), 'share_ab' (% of A-B) or 'mean_primary_energy' (kWh/m²/year)."
            ),
        ],
        order: Annotated[
            str, Field(description="'desc' (highest first, default) or 'asc'.")
        ] = "desc",
        limit: Annotated[int, Field(description="Rows to return, 1-20.")] = 10,
        min_dwellings: Annotated[
            int,
            Field(
                description=f"Ignore groups with fewer DPE than this (default 100, minimum {K_MIN})."
            ),
        ] = 100,
        department: Dept = None,
        region: Region = None,
        building_type: Btype = None,
        construction_period: Period = None,
        construction_year_min: YMin = None,
        construction_year_max: YMax = None,
        climate_zone: Zone = None,
        dpe_date_from: DFrom = None,
        dpe_date_to: DTo = None,
    ) -> dict[str, Any]:
        """Rank départements, régions or communes by share of F-G dwellings, share of A-B, or
        mean primary energy. Use for "which departments have the most passoires thermiques?".
        region may narrow a department ranking; group_by=region takes no geography."""
        f = Filters(
            department,
            region,
            None,
            None,
            building_type,
            construction_period,
            construction_year_min,
            construction_year_max,
            climate_zone,
            dpe_date_from,
            dpe_date_to,
        )
        return await run(
            lambda: svc.rank_territories(f, group_by, metric, order, limit, min_dwellings)
        )

    @mcp.tool(name="dpe_heating_energy_mix", annotations=READ_ONLY)
    async def dpe_heating_energy_mix(
        department: Dept = None,
        region: Region = None,
        commune_insee: Commune = None,
        postal_code: Postal = None,
        building_type: Btype = None,
        construction_period: Period = None,
        construction_year_min: YMin = None,
        construction_year_max: YMax = None,
        climate_zone: Zone = None,
        dpe_date_from: DFrom = None,
        dpe_date_to: DTo = None,
    ) -> dict[str, Any]:
        """Main energy source of dwellings (electricity, natural gas, heating oil, wood,
        district heating...) with counts and shares. Use for "how many homes still heat with
        oil in Finistère?"."""
        f = Filters(
            department,
            region,
            commune_insee,
            postal_code,
            building_type,
            construction_period,
            construction_year_min,
            construction_year_max,
            climate_zone,
            dpe_date_from,
            dpe_date_to,
        )
        return await run(lambda: svc.heating_energy_mix(f))

    @mcp.tool(name="dpe_trend", annotations=READ_ONLY)
    async def dpe_trend(
        interval: Annotated[str, Field(description="'year' (default) or 'month'.")] = "year",
        department: Dept = None,
        region: Region = None,
        commune_insee: Commune = None,
        postal_code: Postal = None,
        building_type: Btype = None,
        construction_period: Period = None,
        construction_year_min: YMin = None,
        construction_year_max: YMax = None,
        climate_zone: Zone = None,
        dpe_date_from: DFrom = None,
        dpe_date_to: DTo = None,
    ) -> dict[str, Any]:
        """Number of DPE issued and label mix per year or month (from 2021-07 by default).
        Use for "has the share of F-G diagnoses fallen since 2022 in Paris?". It shows diagnoses
        issued, which is not the evolution of the housing stock."""
        f = Filters(
            department,
            region,
            commune_insee,
            postal_code,
            building_type,
            construction_period,
            construction_year_min,
            construction_year_max,
            climate_zone,
            dpe_date_from,
            dpe_date_to,
        )
        return await run(lambda: svc.trend(f, interval))

    @mcp.resource("dpe://regions", description="INSEE region codes and names")
    def regions() -> dict[str, str]:
        return REGIONS

    return mcp


def main() -> None:
    p = argparse.ArgumentParser(prog="mcp-dpe-fr", description=__doc__)
    p.add_argument("--transport", choices=("stdio", "streamable-http"), default="stdio")
    p.add_argument("--host", default="127.0.0.1", help="HTTP only; default localhost")
    p.add_argument("--port", type=int, default=8000, help="HTTP only")
    p.add_argument("--log-level", default="WARNING")
    a = p.parse_args()
    logging.basicConfig(level=a.log_level.upper())  # stderr: stdout is the stdio protocol
    mcp = build_server()
    if a.transport == "streamable-http":
        mcp.settings.host, mcp.settings.port = a.host, a.port
    mcp.run(transport=a.transport)


if __name__ == "__main__":
    main()
