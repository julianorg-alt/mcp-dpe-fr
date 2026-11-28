"""Task-oriented, read-only tools over the DPE dataset. Aggregates only, never rows.

Privacy: the dataset's row-level fields (addresses, building complements, coordinates, DPE
numbers) are never requested. Every group smaller than K_MIN is suppressed, and a scope with
fewer than K_MIN dwellings is refused.
"""

from __future__ import annotations

import re
from typing import Any

from .client import DataFairClient
from .errors import InsufficientData, InvalidInput
from .filters import FIRST_DPE_DATE, LABELS, Filters
from .geo import DEPARTMENTS, REGIONS

K_MIN = 10
DATASET = "DPE Logements existants (depuis juillet 2021)"
LICENCE = "Licence Ouverte / Open Licence version 2.0 (Etalab)"
PAGE = "https://data.ademe.fr/datasets/dpe03existant"

INDICATORS = {
    "primary_energy_kwh_m2": ("conso_5_usages_par_m2_ep", "kWh of primary energy / m² / year"),
    "ghg_kg_m2": ("emission_ges_5_usages_par_m2", "kg CO2-equivalent / m² / year"),
    "living_area_m2": ("surface_habitable_logement", "m² (dwelling living area)"),
}
GROUPINGS = {
    "department": "code_departement_ban",
    "region": "code_region_ban",
    "commune": "code_insee_ban",
}
_NAME = re.compile(r"^[\w' .\-]{2,60}$", re.UNICODE)


def _pct(part: float, whole: float) -> float | None:
    return round(100 * part / whole, 1) if whole else None


class DpeTools:
    def __init__(self, client: DataFairClient):
        self.c = client

    # ---- shared -------------------------------------------------------------------------
    async def _source(self) -> dict[str, Any]:
        latest = await self.c.get(
            "metric_agg", {"metric": "max", "field": "date_etablissement_dpe"}
        )
        return {
            "dataset": DATASET,
            "publisher": "ADEME",
            "licence": LICENCE,
            "url": PAGE,
            "latest_dpe_date": latest.get("metric"),
            "dataset_rows": latest.get("total"),
        }

    async def _labels(self, params: dict[str, str]) -> tuple[int, dict[str, int]]:
        r = await self.c.get(
            "values_agg", {"field": "etiquette_dpe", "size": 0, "agg_size": 10, **params}
        )
        total = int(r["total"])
        if total < K_MIN:
            raise InsufficientData(
                f"Only {total} DPE match this scope; at least {K_MIN} are required before any "
                "statistic is returned. Widen the scope (department instead of commune, drop filters)."
            )
        counts = {a["value"]: int(a["total"]) for a in r["aggs"]}
        return total, {label: counts.get(label, 0) for label in LABELS}

    # ---- 1. commune lookup ----------------------------------------------------------------
    async def find_commune(self, name: str, department: str | None = None) -> dict[str, Any]:
        name = (name or "").strip()
        if not _NAME.match(name):
            raise InvalidInput("name must be 2-60 characters: letters, digits, spaces, - ' .")
        f = Filters(department=department).validate()
        r = await self.c.get(
            "values_agg",
            {
                "field": "code_insee_ban;nom_commune_ban",
                "size": 0,
                "agg_size": 10,
                "q": f'"{name}"',
                "q_fields": "nom_commune_ban",
                **f.to_params(),
            },
        )
        communes = []
        for a in r["aggs"]:
            if a["total"] < K_MIN or not a.get("aggs"):
                continue
            insee = a["value"]
            communes.append(
                {
                    "commune_insee": insee,
                    "name": a["aggs"][0]["value"],
                    "department": insee[:3] if insee[:2] in ("97", "98") else insee[:2],
                    "dpe_count": a["total"],
                }
            )
        return {
            "query": name,
            "matches": communes,
            "hint": "Pass commune_insee to the other tools. Paris is split by arrondissement "
            "(75101-75120): use department='75' for the whole city.",
            "source": await self._source(),
        }

    # ---- 2. label distribution --------------------------------------------------------------
    async def label_distribution(self, f: Filters) -> dict[str, Any]:
        f.validate()
        total, counts = await self._labels(f.to_params())
        return {
            "scope": f.describe(),
            "dwellings": total,
            "labels": [
                {
                    "label": label,
                    "count": counts[label] if counts[label] >= K_MIN else f"<{K_MIN}",
                    "share_pct": _pct(counts[label], total),
                }
                for label in LABELS
            ],
            "share_ab_pct": _pct(counts["A"] + counts["B"], total),
            "share_fg_pct": _pct(counts["F"] + counts["G"], total),
            "notes": [
                "Counts DPE issued since July 2021 (one dwelling can appear again after a new "
                "diagnosis), not the whole housing stock.",
                "F and G are the 'passoires thermiques'.",
            ],
            "source": await self._source(),
        }

    # ---- 3. energy / GHG statistics -------------------------------------------------------
    async def energy_stats(self, f: Filters, indicator: str) -> dict[str, Any]:
        f.validate()
        if indicator not in INDICATORS:
            raise InvalidInput(f"indicator must be one of: {', '.join(INDICATORS)}.")
        field, unit = INDICATORS[indicator]
        params = f.to_params()
        total, _ = await self._labels(params)
        mean = await self.c.get("metric_agg", {"metric": "avg", "field": field, **params})
        pct = await self.c.get(
            "metric_agg",
            {"metric": "percentiles", "percents": "10,50,90", "field": field, **params},
        )
        q = {int(p["key"]): p["value"] for p in pct["metric"]}
        rnd = lambda v: None if v is None else round(v, 1)  # noqa: E731
        return {
            "scope": f.describe(),
            "indicator": indicator,
            "unit": unit,
            "dwellings": total,
            "mean": rnd(mean.get("metric")),
            "p10": rnd(q.get(10)),
            "median": rnd(q.get(50)),
            "p90": rnd(q.get(90)),
            "notes": [
                "Declared values include a few data-entry outliers; prefer the median to the mean.",
            ],
            "source": await self._source(),
        }

    # ---- 4. territory ranking -------------------------------------------------------------
    async def rank_territories(
        self,
        f: Filters,
        group_by: str,
        metric: str,
        order: str = "desc",
        limit: int = 10,
        min_dwellings: int = 100,
    ) -> dict[str, Any]:
        f.validate()
        if group_by not in GROUPINGS:
            raise InvalidInput(f"group_by must be one of: {', '.join(GROUPINGS)}.")
        if metric not in ("share_fg", "share_ab", "mean_primary_energy"):
            raise InvalidInput("metric must be share_fg, share_ab or mean_primary_energy.")
        if order not in ("asc", "desc"):
            raise InvalidInput("order must be asc or desc.")
        if not 1 <= limit <= 20:
            raise InvalidInput("limit must be between 1 and 20.")
        min_dwellings = max(min_dwellings, K_MIN)
        if group_by == "department" and (f.department or f.commune_insee or f.postal_code):
            raise InvalidInput("group_by=department cannot be combined with a narrower scope.")
        if group_by == "region" and (f.department or f.region or f.commune_insee or f.postal_code):
            raise InvalidInput("group_by=region takes no geographic scope.")
        if group_by == "commune" and not f.department:
            raise InvalidInput(
                "group_by=commune requires a department (the 200 communes with the most DPE are ranked)."
            )
        if group_by == "commune" and (f.commune_insee or f.postal_code):
            raise InvalidInput(
                "group_by=commune cannot be combined with commune_insee/postal_code."
            )

        field = GROUPINGS[group_by]
        pool = 200 if group_by == "commune" else 1000
        base = f.to_params()
        call: dict[str, Any] = {"field": field, "size": 0, "agg_size": pool, **base}
        if metric == "mean_primary_energy":
            call.update(metric="avg", metric_field=INDICATORS["primary_energy_kwh_m2"][0])
        r = await self.c.get("values_agg", call)
        rows = {a["value"]: a for a in r["aggs"] if a["total"] >= min_dwellings}
        if not rows:
            raise InsufficientData(
                f"No {group_by} has at least {min_dwellings} DPE in this scope; lower min_dwellings."
            )

        value: dict[str, float | None]
        if metric == "mean_primary_energy":
            value = {k: a.get("metric") for k, a in rows.items()}
        else:
            wanted = ("F", "G") if metric == "share_fg" else ("A", "B")
            sub = await self.c.get(
                "values_agg",
                {
                    "field": field,
                    "size": 0,
                    "agg_size": min(len(rows), 1000),
                    f"{field}_in": ",".join(rows),
                    "etiquette_dpe_in": ",".join(wanted),
                    **base,
                },
            )
            hits = {a["value"]: a["total"] for a in sub["aggs"]}
            value = {k: _pct(hits.get(k, 0), a["total"]) for k, a in rows.items()}

        names = await self._names(group_by, rows, base, field)
        ranked = sorted(
            (k for k in rows if value[k] is not None),
            key=lambda k: value[k],  # type: ignore[arg-type,return-value]
            reverse=order == "desc",
        )[:limit]
        unit = "kWh primary energy/m²/year" if metric == "mean_primary_energy" else "% of DPE"
        return {
            "scope": f.describe(),
            "group_by": group_by,
            "metric": metric,
            "unit": unit,
            "order": order,
            "min_dwellings": min_dwellings,
            "groups_considered": len(rows),
            "ranking": [
                {
                    "rank": i + 1,
                    "code": k,
                    "name": names.get(k),
                    "dwellings": rows[k]["total"],
                    "value": round(value[k], 1),  # type: ignore[arg-type]
                }
                for i, k in enumerate(ranked)
            ],
            "notes": (
                ["Only the 200 communes with the most DPE in the department are considered."]
                if group_by == "commune"
                else []
            )
            + (
                ["Mean is sensitive to data-entry outliers."]
                if metric == "mean_primary_energy"
                else []
            ),
            "source": await self._source(),
        }

    async def _names(self, group_by: str, rows: dict, base: dict, field: str) -> dict[str, str]:
        if group_by == "department":
            return {k: DEPARTMENTS.get(k, "") for k in rows}
        if group_by == "region":
            return {k: REGIONS.get(k, "") for k in rows}
        r = await self.c.get(
            "values_agg",
            {"field": "code_insee_ban;nom_commune_ban", "size": 0, "agg_size": 200, **base},
        )
        return {a["value"]: a["aggs"][0]["value"] for a in r["aggs"] if a.get("aggs")}

    # ---- 5. heating energy mix ------------------------------------------------------------
    async def heating_energy_mix(self, f: Filters) -> dict[str, Any]:
        f.validate()
        total, _ = await self._labels(f.to_params())
        r = await self.c.get(
            "values_agg",
            {"field": "type_energie_n1", "size": 0, "agg_size": 20, **f.to_params()},
        )
        known = sum(a["total"] for a in r["aggs"])
        mix = [
            {"energy": a["value"], "count": a["total"], "share_pct": _pct(a["total"], known)}
            for a in r["aggs"]
            if a["total"] >= K_MIN
        ]
        return {
            "scope": f.describe(),
            "dwellings": total,
            "dwellings_with_energy_declared": known,
            "main_energy": mix,
            "notes": [
                "type_energie_n1 = main energy source of the dwelling (first energy in the DPE); "
                "shares are computed over dwellings where it is declared.",
                f"Sources with fewer than {K_MIN} dwellings are omitted.",
            ],
            "source": await self._source(),
        }

    # ---- 6. trend ---------------------------------------------------------------------------
    async def trend(self, f: Filters, interval: str = "year") -> dict[str, Any]:
        f.validate()
        if interval not in ("year", "month"):
            raise InvalidInput("interval must be 'year' or 'month'.")
        if not f.dpe_date_from:
            f = Filters(**{**f.__dict__, "dpe_date_from": FIRST_DPE_DATE.isoformat()})
        await self._labels(f.to_params())
        r = await self.c.get(
            "values_agg",
            {
                "field": "date_etablissement_dpe;etiquette_dpe",
                "interval": interval,
                "size": 0,
                "agg_size": 100,
                **f.to_params(),
            },
        )
        periods = []
        for a in r["aggs"]:
            if a["total"] < K_MIN:
                continue
            counts = {s["value"]: s["total"] for s in a.get("aggs", [])}
            periods.append(
                {
                    "period": a["value"][:4] if interval == "year" else a["value"][:7],
                    "dwellings": a["total"],
                    "share_ab_pct": _pct(counts.get("A", 0) + counts.get("B", 0), a["total"]),
                    "share_fg_pct": _pct(counts.get("F", 0) + counts.get("G", 0), a["total"]),
                    "share_pct": {k: _pct(counts.get(k, 0), a["total"]) for k in LABELS},
                }
            )
        return {
            "scope": f.describe(),
            "interval": interval,
            "periods": periods,
            "notes": [
                "Period = date the DPE was established. The last period may be incomplete.",
                "Shows the mix of diagnoses issued, which is not the evolution of the housing stock "
                "(sales and rentals trigger diagnoses, so the sample is biased toward transactions).",
            ],
            "source": await self._source(),
        }
