"""Evaluation harness: 19 real questions, each answered by a tool and checked against an
independent reference computed with raw API calls (no code shared with the tools).

  python evals/run_eval.py --mode replay   # offline, recorded responses (CI)
  python evals/run_eval.py --mode live     # real API; tool answer vs fresh raw reference
  python evals/run_eval.py --mode record   # live + refresh cassette and `expected` snapshot

Exit code 1 if any question fails.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path

import httpx

from mcp_dpe_fr.cassette import RecordTransport, ReplayTransport
from mcp_dpe_fr.client import BASE_URL, DataFairClient
from mcp_dpe_fr.errors import DpeError
from mcp_dpe_fr.filters import Filters
from mcp_dpe_fr.tools import DpeTools

ROOT = Path(__file__).resolve().parent
QUESTIONS = ROOT / "questions.json"
CASSETTE = ROOT.parent / "tests" / "cassettes" / "eval.json"


# ---- independent reference: raw HTTP only --------------------------------------------------
async def raw(http: httpx.AsyncClient, endpoint: str, **params):
    r = await http.get(f"{BASE_URL}/{endpoint}", params=params)
    r.raise_for_status()
    return r.json()


async def reference(http: httpx.AsyncClient, ref: dict):
    op = ref["op"]
    if op == "none":
        return None
    if op == "total":
        return (await raw(http, "lines", size=0, **ref["params"]))["total"]
    if op == "share":
        n = (await raw(http, "lines", size=0, **ref["num"]))["total"]
        d = (await raw(http, "lines", size=0, **ref["den"]))["total"]
        return round(100 * n / d, 1)
    if op == "metric":
        if ref["metric"] == "p50":
            r = await raw(
                http,
                "metric_agg",
                metric="percentiles",
                percents="50",
                field=ref["field"],
                **ref["params"],
            )
            return round(r["metric"][0]["value"], 1)
        r = await raw(
            http,
            "metric_agg",
            metric="percentiles",
            percents="90",
            field=ref["field"],
            **ref["params"],
        )
        return round(r["metric"][0]["value"], 1)
    if op == "top_value":
        r = await raw(http, "values_agg", field=ref["field"], size=0, agg_size=1, **ref["params"])
        return r["aggs"][0]["value"]
    if op == "argrank":
        g, base = ref["group"], ref.get("base", {})
        extra = {}
        if "avg" in ref:
            extra = {"metric": "avg", "metric_field": ref["avg"]}
        tot = (
            await raw(http, "values_agg", field=g, size=0, agg_size=ref["pool"], **base, **extra)
        )["aggs"]
        tot = {a["value"]: a for a in tot if a["total"] >= ref["min"]}
        if "avg" in ref:
            score = {k: a["metric"] for k, a in tot.items()}
        else:
            num = (
                await raw(http, "values_agg", field=g, size=0, agg_size=1000, **base, **ref["num"])
            )["aggs"]
            hits = {a["value"]: a["total"] for a in num}
            score = {k: round(100 * hits.get(k, 0) / a["total"], 1) for k, a in tot.items()}
        pick = max if ref["order"] == "desc" else min
        return pick(score, key=lambda k: score[k])
    raise ValueError(f"unknown ref op {op}")


# ---- tool dispatch + result path ---------------------------------------------------------------
async def call_tool(svc: DpeTools, tool: str, a: dict):
    geo = {k: a.get(k) for k in ("department", "region", "commune_insee", "postal_code")}
    rest = {
        k: a.get(k)
        for k in (
            "building_type",
            "construction_period",
            "construction_year_min",
            "construction_year_max",
            "climate_zone",
            "dpe_date_from",
            "dpe_date_to",
        )
    }
    if tool == "dpe_find_commune":
        return await svc.find_commune(a["name"], a.get("department"))
    if tool == "dpe_rank_territories":
        f = Filters(a.get("department"), a.get("region"), None, None, **rest)
        return await svc.rank_territories(
            f,
            a["group_by"],
            a["metric"],
            a.get("order", "desc"),
            a.get("limit", 10),
            a.get("min_dwellings", 100),
        )
    f = Filters(**geo, **rest)
    handlers = {
        "dpe_label_distribution": lambda: svc.label_distribution(f),
        "dpe_energy_stats": lambda: svc.energy_stats(f, a.get("indicator", "")),
        "dpe_heating_energy_mix": lambda: svc.heating_energy_mix(f),
        "dpe_trend": lambda: svc.trend(f, a.get("interval", "year")),
    }
    return await handlers[tool]()


def pick(obj, path: str):
    for part in path.split("."):
        m = re.fullmatch(r"(\w+)\[(\w+)=(.+)\]", part)
        if m:
            obj = next(x for x in obj[m[1]] if str(x[m[2]]) == m[3])
        elif part.isdigit():
            obj = obj[int(part)]
        else:
            obj = obj[part]
    return obj


def close(a, b, tol) -> bool:
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return abs(a - b) <= tol
    return a == b


async def run(mode: str) -> int:
    questions = json.loads(QUESTIONS.read_text("utf-8"))
    if mode == "replay":
        transport = ReplayTransport(CASSETTE)
    elif mode == "record":
        transport = RecordTransport(CASSETTE)
    else:
        transport = None
    client = DataFairClient(transport=transport, rate=8, burst=8)
    http = httpx.AsyncClient(transport=transport, timeout=30)
    svc = DpeTools(client)
    failures = 0
    for q in questions:
        try:
            ref = await reference(http, q["ref"])
            if q.get("expect_error"):
                try:
                    await call_tool(svc, q["tool"], q["args"])
                    ok, got, detail = False, "no error", "expected a refusal"
                except DpeError as e:
                    ok, got = q["expect_error"] in str(e), str(e)[:60]
                    detail = f"refused: {got}"
            else:
                got = pick(await call_tool(svc, q["tool"], q["args"]), q["path"])
                ok = close(got, ref, q["tol"])
                if mode == "replay" and "expected" in q:
                    ok = ok and close(got, q["expected"], q["tol"])
                detail = f"tool={got} reference={ref}"
            if mode == "record" and not q.get("expect_error"):
                q["expected"] = ref
        except Exception as e:  # noqa: BLE001 - report and continue
            ok, detail = False, f"{type(e).__name__}: {e}"
        failures += not ok
        print(f"{'PASS' if ok else 'FAIL'} {q['id']} {detail}")
    if mode == "record":
        transport.save()
        QUESTIONS.write_text(json.dumps(questions, ensure_ascii=False, indent=1) + "\n", "utf-8")
    await client.aclose()
    await http.aclose()
    print(f"\n{len(questions) - failures}/{len(questions)} passed ({mode})")
    return 1 if failures else 0


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--mode", choices=("replay", "live", "record"), default="replay")
    sys.exit(asyncio.run(run(p.parse_args().mode)))


if __name__ == "__main__":
    main()
