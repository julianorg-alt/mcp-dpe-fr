"""Tool logic against stubbed API responses (no network)."""

import pytest

from mcp_dpe_fr.errors import InsufficientData, InvalidInput
from mcp_dpe_fr.filters import Filters
from mcp_dpe_fr.tools import DpeTools


class Stub:
    """Fake DataFairClient: answers by endpoint from a dict of callables."""

    def __init__(self, **handlers):
        self.handlers, self.calls = handlers, []

    async def get(self, endpoint, params):
        self.calls.append((endpoint, params))
        h = self.handlers[endpoint]
        return h(params) if callable(h) else h


META = {"metric": "2026-11-11", "total": 1000}


def labels(**counts):
    aggs = [{"value": k, "total": v} for k, v in counts.items()]
    return {"total": sum(counts.values()), "aggs": aggs}


async def test_small_label_cells_are_suppressed_but_total_kept():
    svc = DpeTools(Stub(values_agg=labels(A=3, B=40, C=100, G=57), metric_agg=META))
    r = await svc.label_distribution(Filters(department="75"))
    cells = {x["label"]: x["count"] for x in r["labels"]}
    assert cells["A"] == "<10" and cells["B"] == 40 and cells["D"] == "<10"
    assert r["dwellings"] == 200 and r["share_fg_pct"] == 28.5


async def test_scope_under_k_is_refused_and_never_returns_numbers():
    svc = DpeTools(Stub(values_agg=labels(C=4, D=3), metric_agg=META))
    with pytest.raises(InsufficientData, match="at least 10"):
        await svc.label_distribution(Filters(commune_insee="33063"))


async def test_every_response_carries_source_and_licence():
    svc = DpeTools(Stub(values_agg=labels(C=50), metric_agg=META))
    r = await svc.label_distribution(Filters())
    assert "Licence Ouverte" in r["source"]["licence"] and r["source"]["latest_dpe_date"]


@pytest.mark.parametrize(
    "args",
    [
        dict(group_by="department", metric="share_fg", f=Filters(department="75")),
        dict(group_by="region", metric="share_fg", f=Filters(region="11")),
        dict(group_by="commune", metric="share_fg", f=Filters()),
        dict(group_by="planet", metric="share_fg", f=Filters()),
        dict(group_by="region", metric="vibes", f=Filters()),
        dict(group_by="region", metric="share_fg", f=Filters(), limit=0),
        dict(group_by="region", metric="share_fg", f=Filters(), limit=21),
        dict(group_by="region", metric="share_fg", f=Filters(), order="sideways"),
    ],
)
async def test_rank_rejects_incoherent_requests(args):
    svc = DpeTools(Stub(values_agg=labels(C=50), metric_agg=META))
    f = args.pop("f")
    with pytest.raises(InvalidInput):
        await svc.rank_territories(f, **args)


async def test_rank_min_dwellings_cannot_go_below_k():
    def agg(params):
        if "etiquette_dpe_in" in params:
            return {"aggs": [{"value": "11", "total": 5}]}
        return {"aggs": [{"value": "11", "total": 5}, {"value": "24", "total": 500}]}

    svc = DpeTools(Stub(values_agg=agg, metric_agg=META))
    r = await svc.rank_territories(Filters(), "region", "share_fg", min_dwellings=1)
    assert [x["code"] for x in r["ranking"]] == ["24"]  # group of 5 never shown
    assert r["min_dwellings"] == 10


async def test_energy_stats_rejects_unknown_indicator():
    svc = DpeTools(Stub(values_agg=labels(C=50), metric_agg=META))
    with pytest.raises(InvalidInput):
        await svc.energy_stats(Filters(), "adresse_ban")


async def test_find_commune_rejects_odd_names_and_hides_tiny_groups():
    inner = lambda n: [{"value": "Foo", "total": n}]  # noqa: E731
    stub = Stub(
        values_agg={
            "aggs": [
                {"value": "33063", "total": 500, "aggs": inner(500)},
                {"value": "33001", "total": 3, "aggs": inner(3)},
            ]
        },
        metric_agg=META,
    )
    svc = DpeTools(stub)
    for bad in ("", "a", 'x"; select=adresse_ban', "x" * 61):
        with pytest.raises(InvalidInput):
            await svc.find_commune(bad)
    r = await svc.find_commune("Foo")
    assert [m["commune_insee"] for m in r["matches"]] == ["33063"]
