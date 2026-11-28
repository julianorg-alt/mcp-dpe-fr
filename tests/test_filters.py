import pytest

from mcp_dpe_fr.errors import InvalidInput
from mcp_dpe_fr.filters import Filters


def test_params_are_translated():
    f = Filters(department="2a", building_type="maison", construction_year_min=1990).validate()
    assert f.to_params() == {
        "code_departement_ban_eq": "2A",
        "type_batiment_eq": "maison",
        "annee_construction_gte": "1990",
    }


@pytest.mark.parametrize(
    "kwargs",
    [
        {"department": "99"},
        {"department": "75", "region": "11"},
        {"region": "99"},
        {"commune_insee": "3306"},
        {"commune_insee": "33063; DROP"},
        {"postal_code": "3300"},
        {"building_type": "villa"},
        {"construction_period": "1950"},
        {"climate_zone": "H9"},
        {"construction_year_min": 1500},
        {"construction_year_min": 2000, "construction_year_max": 1990},
        {"dpe_date_from": "2024-13-01"},
        {"dpe_date_from": "2025-02-01", "dpe_date_to": "2025-01-01"},
    ],
)
def test_invalid_inputs_are_rejected(kwargs):
    with pytest.raises(InvalidInput):
        Filters(**kwargs).validate()


def test_injection_cannot_reach_the_query():
    # Free text never goes into a filter: only enumerated / patterned values pass validation.
    with pytest.raises(InvalidInput):
        Filters(building_type="maison&select=adresse_ban").validate()
    with pytest.raises(InvalidInput):
        Filters(department="75&select=adresse_ban").validate()
