"""
Cuyahoga County Fiscal Officer parcel lookup, via the live ArcGIS REST
service (unlike Franklin's monthly bulk Auditor CSV, Cuyahoga has no
downloadable bulk file for this - it's a live query service).

STATUS: CONFIRMED against the live service (2026-10-14) via
discover_countywide_parcels.py, run against real parcels from a lead batch.

History: Open_Data_Parcels layer 0 was the first source, but it is
Cleveland-only (every parcel prefix <= 143 matched, every prefix >= 204
missed, across a real 93-lead batch). The countywide source is
CCFO/Current_Year_Appraisal_Parcels, the Fiscal Officer's own CAMA
(appraisal) data. It has three layers, and a parcel lives in exactly one:

  layer 2  FO_SP_CAMA  standard parcels (houses, lots) - the common case;
                       confirmed hits for Euclid, Bedford and Cleveland
  layer 0  FO_PP_CAMA  point parcels (condos); confirmed hit for a
                       Broadview Heights condo
  layer 1  FO_AR_CAMA  air/lease parcels (e.g. utility leases) - rarely
                       relevant, queried last as a fallback

So lookups try layers in that order and return the first match.
MyPlace/MyPLACE_Tax_Parcels has no owner/mailing fields, and
MyPlace/MyPLACE_Consolidated and CCGIS/Parcel_RP_CAMA_Table are not
usable (no queryable layers / only annotation and basemap layers).

PARCELPIN has no hyphens (e.g. "10618016" for "106-18-016"), matching
get_parcel_info()'s existing hyphen-stripping. Condo pins can carry a
trailing letter (e.g. "86301891D").
"""

import re
from typing import Optional, Dict
import requests

ARCGIS_BASE = (
    "https://gis.cuyahogacounty.gov/server/rest/services/"
    "CCFO/Current_Year_Appraisal_Parcels/MapServer"
)

# Primary layer (kept for diagnostic scripts that import LAYER_ID) and the
# full lookup order: standard parcels, then condos, then air/lease parcels.
LAYER_ID = 2
LAYER_IDS = [2, 0, 1]

FIELD_MAP = {
    "parcel_number": "PARCELPIN",
    "owner_name": "parcel_owner",
    "mail_name": "mail_name",
    "mailing_address": "mail_addr_street",
    "mailing_unit": "mail_unit_no",
    "mailing_city": "mail_city",
    "mailing_state": "mail_state",
    "mailing_zip": "mail_zip",
    "property_address_all": "par_addr_all",
}


def discover_fields(sample_parcel_number: str) -> dict:
    """
    Diagnostic helper: query every layer on the MapServer and dump the
    real field names/values, so FIELD_MAP and LAYER_IDS above can be
    corrected if the service changes. Intended to be run from
    discover_parcel_fields.py, not from the normal pipeline.
    """
    service_resp = requests.get(f"{ARCGIS_BASE}?f=json", timeout=30)
    service_resp.raise_for_status()
    service_info = service_resp.json()

    results = {"service_layers": service_info.get("layers", []), "layer_results": {}}

    for layer in service_info.get("layers", []):
        layer_id = layer.get("id")
        query_url = f"{ARCGIS_BASE}/{layer_id}/query"
        params = {
            "where": "1=1",
            "outFields": "*",
            "f": "json",
            "resultRecordCount": 5,
        }
        try:
            resp = requests.get(query_url, params=params, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            if "error" in data:
                results["layer_results"][layer_id] = {"error": data["error"]}
                continue
            features = data.get("features", [])
            results["layer_results"][layer_id] = {
                "field_names": [f.get("name") for f in data.get("fields", [])],
                "sample_attributes": [feat.get("attributes") for feat in features],
            }
        except Exception as exc:
            results["layer_results"][layer_id] = {"error": str(exc)}

    return results


def _query_layer(layer_id: int, where_clause: str) -> Optional[dict]:
    query_url = f"{ARCGIS_BASE}/{layer_id}/query"
    params = {
        "where": where_clause,
        "outFields": "*",
        "f": "json",
        "resultRecordCount": 1,
    }
    resp = requests.get(query_url, params=params, timeout=30)
    resp.raise_for_status()
    data = resp.json()

    # ArcGIS returns application-level errors as a 200 with an "error" body,
    # not an HTTP error status - handle that explicitly rather than
    # crashing on a malformed/missing "features" key.
    if "error" in data:
        return None

    features = data.get("features", [])
    if not features:
        return None
    return features[0].get("attributes", {})


def _query_arcgis(where_clause: str) -> Optional[dict]:
    """Try each layer in LAYER_IDS order; return the first match. A failure
    on one layer (network error, bad response) doesn't stop the others."""
    for layer_id in LAYER_IDS:
        try:
            attrs = _query_layer(layer_id, where_clause)
        except Exception:
            attrs = None
        if attrs:
            return attrs
    return None


def _clean(value) -> str:
    return str(value or "").strip()


def _attrs_to_result(attrs: dict) -> Dict[str, str]:
    # The Fiscal Officer uses "00000" as a placeholder when it has no zip.
    zip_code = _clean(attrs.get(FIELD_MAP["mailing_zip"]))
    if zip_code.strip("0") == "":
        zip_code = ""

    return {
        # parcel_owner is the legal title-of-record name; mail_name is the
        # addressee for the tax bill - prefer mail_name since this feeds a
        # direct-mail workflow, falling back to parcel_owner otherwise.
        "owner_name": (
            _clean(attrs.get(FIELD_MAP["mail_name"]))
            or _clean(attrs.get(FIELD_MAP["owner_name"]))
        ),
        "mailing_address": " ".join(filter(None, [
            _clean(attrs.get(FIELD_MAP["mailing_address"])),
            _clean(attrs.get(FIELD_MAP["mailing_unit"])),
            _clean(attrs.get(FIELD_MAP["mailing_city"])),
            _clean(attrs.get(FIELD_MAP["mailing_state"])),
            zip_code,
        ])).strip(),
    }


def get_parcel_info(parcel_number: str) -> Dict[str, str]:
    if not parcel_number:
        return {}
    clean = parcel_number.replace("-", "").replace(" ", "")
    where = f"{FIELD_MAP['parcel_number']} = '{clean}'"
    attrs = _query_arcgis(where)
    if not attrs:
        return {}
    return _attrs_to_result(attrs)


_STREET_SUFFIXES = {
    "ST", "STREET", "AVE", "AVENUE", "RD", "ROAD", "DR", "DRIVE", "LN", "LANE",
    "BLVD", "BOULEVARD", "CT", "COURT", "CIR", "CIRCLE", "PL", "PLACE", "TER",
    "TERRACE", "WAY", "PKWY", "PARKWAY", "HWY", "HIGHWAY", "SQ", "SQUARE",
    "TRL", "TRAIL", "PT", "POINT", "RUN", "ROW", "PATH", "ALY", "ALLEY",
}
_UNIT_RE = re.compile(r"(\bUNIT\b|\bAPT\b|\bSTE\b|\bSUITE\b|\bBLDG\b|#)")


def _street_core(street_name: str) -> str:
    """Reduce a street name to its distinctive word: "9489 PONDEROSA LANE"
    style input -> "PONDEROSA", "WEST RIDGEWOOD DR" -> "RIDGEWOOD". The
    county stores suffixes abbreviated ("LN") and direction prefixes
    inconsistently, so matching the full spelled-out name misses."""
    words = re.sub(r"[^A-Z0-9 ]", " ", street_name.upper()).split()
    while words and words[-1] in _STREET_SUFFIXES:
        words.pop()
    return words[-1] if words else ""


def get_parcel_info_by_address(street_number: str, street_name: str, city: str = "") -> Dict[str, str]:
    """Fallback lookup when the parcel number is missing or doesn't match.

    Matches the house number exactly plus the street's distinctive word
    (parcel_addr / parcel_street fields). Deliberately conservative, since a
    wrong owner is worse than none:
      - addresses with a unit/apt are skipped (a building has many owners,
        and we can't tell which unit is the lead's);
      - if several parcels match, the city must narrow it to one; a single
        match is accepted even if the city text differs (listings contain
        typos like "BROOK PARL")."""
    street_number = (street_number or "").strip()
    core = _street_core(street_name or "")
    if not street_number.isdigit() or not core:
        return {}
    if _UNIT_RE.search((street_name or "").upper()):
        return {}

    core_sql = core.replace("'", "''")
    where = (
        f"parcel_addr = '{street_number}' AND "
        f"UPPER(parcel_street) LIKE '%{core_sql}%'"
    )

    candidates = []
    for layer_id in LAYER_IDS:
        query_url = f"{ARCGIS_BASE}/{layer_id}/query"
        params = {"where": where, "outFields": "*", "f": "json", "resultRecordCount": 10}
        try:
            resp = requests.get(query_url, params=params, timeout=30)
            resp.raise_for_status()
            data = resp.json()
        except Exception:
            continue
        if "error" in data:
            continue
        candidates = [f.get("attributes", {}) for f in data.get("features", [])]
        if candidates:
            break

    if not candidates:
        return {}

    if len(candidates) > 1:
        city_up = (city or "").strip().upper()
        if not city_up:
            return {}
        candidates = [
            c for c in candidates
            if _clean(c.get("parcel_city")).upper() == city_up
        ]
        if len(candidates) != 1:
            return {}

    return _attrs_to_result(candidates[0])
