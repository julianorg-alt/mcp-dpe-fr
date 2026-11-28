"""Input validation. Only enumerated or strictly-patterned values reach the upstream query."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from .errors import InvalidInput
from .geo import DEPARTMENTS, REGIONS

LABELS = ("A", "B", "C", "D", "E", "F", "G")
BUILDING_TYPES = ("appartement", "maison", "immeuble")
PERIODS = (
    "avant 1948",
    "1948-1974",
    "1975-1977",
    "1978-1982",
    "1983-1988",
    "1989-2000",
    "2001-2005",
    "2006-2012",
    "2013-2021",
    "après 2021",
)
CLIMATE_ZONES = ("H1a", "H1b", "H1c", "H2a", "H2b", "H2c", "H2d", "H3")
FIRST_DPE_DATE = date(2021, 7, 1)  # dataset starts with the July 2021 DPE reform

_INSEE = re.compile(r"^(\d{5}|2[AB]\d{3})$")
_POSTAL = re.compile(r"^\d{5}$")


@dataclass(frozen=True)
class Filters:
    department: str | None = None
    region: str | None = None
    commune_insee: str | None = None
    postal_code: str | None = None
    building_type: str | None = None
    construction_period: str | None = None
    construction_year_min: int | None = None
    construction_year_max: int | None = None
    climate_zone: str | None = None
    dpe_date_from: str | None = None
    dpe_date_to: str | None = None

    def validate(self) -> Filters:
        geo = [x for x in (self.department, self.region, self.commune_insee, self.postal_code) if x]
        if len(geo) > 1:
            raise InvalidInput(
                "Give at most one geographic scope among department, region, commune_insee, "
                "postal_code (they overlap)."
            )
        if self.department and self.department.upper() not in DEPARTMENTS:
            raise InvalidInput(
                f"Unknown department '{self.department}'. Use the INSEE code: '01'..'95', "
                "'2A', '2B', '971'..'976'."
            )
        if self.region and self.region not in REGIONS:
            raise InvalidInput(
                f"Unknown region '{self.region}'. Use the INSEE code, e.g. '11' (Île-de-France), "
                "'93' (PACA). See the tool description for the full list."
            )
        if self.commune_insee and not _INSEE.match(self.commune_insee.upper()):
            raise InvalidInput(
                "commune_insee must be the 5-character INSEE code (e.g. '33063'). "
                "Use dpe_find_commune to get it from a name."
            )
        if self.postal_code and not _POSTAL.match(self.postal_code):
            raise InvalidInput("postal_code must be 5 digits, e.g. '33000'.")
        _enum("building_type", self.building_type, BUILDING_TYPES)
        _enum("construction_period", self.construction_period, PERIODS)
        _enum("climate_zone", self.climate_zone, CLIMATE_ZONES)
        for name in ("construction_year_min", "construction_year_max"):
            v = getattr(self, name)
            if v is not None and not 1700 <= v <= 2030:
                raise InvalidInput(f"{name} must be between 1700 and 2030.")
        if (
            self.construction_year_min is not None
            and self.construction_year_max is not None
            and self.construction_year_min > self.construction_year_max
        ):
            raise InvalidInput("construction_year_min is greater than construction_year_max.")
        d_from = _iso("dpe_date_from", self.dpe_date_from)
        d_to = _iso("dpe_date_to", self.dpe_date_to)
        if d_from and d_to and d_from > d_to:
            raise InvalidInput("dpe_date_from is after dpe_date_to.")
        return self

    def to_params(self) -> dict[str, str]:
        """Translate to data-fair query parameters (all values already validated)."""
        p: dict[str, str] = {}
        if self.department:
            p["code_departement_ban_eq"] = self.department.upper()
        if self.region:
            p["code_region_ban_eq"] = self.region
        if self.commune_insee:
            p["code_insee_ban_eq"] = self.commune_insee.upper()
        if self.postal_code:
            p["code_postal_ban_eq"] = self.postal_code
        if self.building_type:
            p["type_batiment_eq"] = self.building_type
        if self.construction_period:
            p["periode_construction_eq"] = self.construction_period
        if self.construction_year_min is not None:
            p["annee_construction_gte"] = str(self.construction_year_min)
        if self.construction_year_max is not None:
            p["annee_construction_lte"] = str(self.construction_year_max)
        if self.climate_zone:
            p["zone_climatique_eq"] = self.climate_zone
        if self.dpe_date_from:
            p["date_etablissement_dpe_gte"] = self.dpe_date_from
        if self.dpe_date_to:
            p["date_etablissement_dpe_lte"] = self.dpe_date_to
        return p

    def describe(self) -> dict[str, object]:
        return {k: v for k, v in self.__dict__.items() if v is not None}


def _enum(name: str, value: str | None, allowed: tuple[str, ...]) -> None:
    if value is not None and value not in allowed:
        raise InvalidInput(f"{name} must be one of: {', '.join(allowed)}.")


def _iso(name: str, value: str | None) -> date | None:
    if value is None:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise InvalidInput(f"{name} must be an ISO date YYYY-MM-DD.") from None
