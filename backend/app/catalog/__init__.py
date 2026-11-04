"""Small, integrity-checked local lodging catalog. Snapshot facts are not inventory.

Parquet is read once with a local file allowlist and no extension/network access.
Queries rank at most 5000 records in process; they never call an LLM or encoder.
"""
from __future__ import annotations

from dataclasses import dataclass
from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import ipaddress
import json
import math
import os
from pathlib import Path
import re
from typing import Any
from urllib.parse import urlsplit

import duckdb

from ..geography import Place, normalized
from ..models import TravelIntent


SCHEMA_VERSION = "cyprus-lodgings-v2"
LEGACY_SCHEMA_VERSION = "cyprus-hotels-v1"
MAX_ROWS = 5000
MAX_FILE_BYTES = 10 * 1024 * 1024
RADIUS_KM = 8.0
ASSETS = Path(__file__).parent / "assets" / "cyprus"
TAG_KEYS = frozenset({"stars", "wheelchair", "internet_access", "air_conditioning", "swimming_pool",
                      "dog", "pets", "addr:street", "addr:housenumber", "adults_only", "min_age",
                      "addr:city", "addr:postcode", "addr:country", "opening_hours", "check_date", "start_date"})
KINDS = frozenset({"hotel", "apartment", "guest_house", "hostel", "chalet", "motel"})
BOUNDS = (34.5, 32.2, 35.8, 34.7)
LOCALITIES = {
    "larnaca": (34.92, 33.63, {"larnaca", "larnaka", "ларнака", "λαρνακα", "λάρνακα"}),
    "paphos": (34.77, 32.42, {"paphos", "pafos", "пафос", "παφος", "πάφος"}),
    "limassol": (34.68, 33.04, {"limassol", "lemesos", "лимасол", "лимассол", "λεμεσος", "λεμεσός"}),
    "nicosia": (35.17, 33.36, {"nicosia", "lefkosia", "никосия", "левкосия", "λευκωσια", "λευκωσία"}),
    "ayia_napa": (34.99, 34.00, {"ayia napa", "agia napa", "айя-напа", "айя напа", "агия напа", "αγια ναπα", "αγία νάπα"}),
    "protaras": (35.01, 34.05, {"protaras", "протарас", "πρωταρας", "πρωταράς"}),
    "polis": (35.03, 32.43, {"polis", "polis chrysochous", "полис", "πολις", "πόλις"}),
    "troodos": (34.92, 32.88, {"troodos", "троодос", "τροοδος", "τρόοδος"}),
}
LEGACY_FIELDS = {"hotel_id", "name", "name_en", "name_ru", "destination_id", "country_code", "latitude",
          "longitude", "tags_json", "website", "source_url", "source_updated_at", "retrieved_at"}
FIELDS = LEGACY_FIELDS | {"kind", "source_records_json"}
SOURCE_KEYS = {"source_id", "record_id", "url", "revision", "source_updated_at", "retrieved_at", "license",
               "license_url", "link_method", "link_status", "fields"}
WD_FIELDS = {"instance_of", "coordinates", "country", "administrative_area", "website", "street_address",
             "inception", "official_opening", "dissolution", "label.en", "label.ru", "label.el"}
WD_PROPERTIES = {"instance_of": "P31", "coordinates": "P625", "country": "P17", "administrative_area": "P131",
                 "website": "P856", "street_address": "P6375", "inception": "P571", "official_opening": "P1619", "dissolution": "P576"}
OSM_RAW_KEYS = TAG_KEYS | {"name", "name:en", "name:ru", "name:el", "tourism", "website", "contact:website", "wikidata"}


def _distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    a, b = math.radians(lat1), math.radians(lat2)
    value = math.sin((b - a) / 2) ** 2 + math.cos(a) * math.cos(b) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 6371 * 2 * math.asin(min(1, math.sqrt(value)))


def _time(value: Any) -> datetime:
    if not isinstance(value, str) or len(value) > 50:
        raise ValueError("invalid_timestamp")
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("naive_timestamp")
    return result


def _file(path: Path) -> bytes:
    if not path.is_file() or not 0 < path.stat().st_size <= MAX_FILE_BYTES:
        raise ValueError("missing_or_oversized_artifact")
    return path.read_bytes()


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _public_website(value: Any) -> bool:
    if value is None:
        return True
    if not isinstance(value, str) or len(value) > 2000 or re.search(r"[\x00-\x20\\]", value):
        return False
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname or ""
        try:
            public_host = ipaddress.ip_address(hostname).is_global
        except ValueError:
            public_host = "." in hostname and not hostname.endswith((".local", ".localhost", ".internal"))
        return parsed.scheme == "https" and public_host and not parsed.username and not parsed.password and parsed.port in (None, 443)
    except ValueError:
        return False


def _metadata(manifest: dict[str, Any]) -> tuple[datetime, datetime]:
    retrieved, dataset_at = _time(manifest["retrieved_at"]), _time(manifest["source_dataset_at"])
    if dataset_at > retrieved or retrieved.timestamp() > datetime.now(timezone.utc).timestamp() + 300:
        raise ValueError("invalid_source_times")
    return retrieved, dataset_at


def _validate_sources(row: dict[str, Any], dataset_at: datetime) -> list[dict[str, Any]]:
    value = row["source_records_json"]
    if not isinstance(value, str) or len(value.encode("utf-8")) > 100_000:
        raise ValueError("invalid_source_records")
    sources = json.loads(value)
    if not isinstance(sources, list) or not 1 <= len(sources) <= 4:
        raise ValueError("invalid_source_records")
    seen = set()
    for source in sources:
        if not isinstance(source, dict) or set(source) != SOURCE_KEYS:
            raise ValueError("invalid_source_record_fields")
        source_id, record_id = source["source_id"], source["record_id"]
        if not isinstance(record_id, str) or record_id in seen or source_id not in {"osm", "wikidata"}:
            raise ValueError("invalid_source_identity")
        seen.add(record_id)
        modified, acquired = _time(source["source_updated_at"]), _time(source["retrieved_at"])
        if modified > acquired or acquired.timestamp() > datetime.now(timezone.utc).timestamp() + 300:
            raise ValueError("invalid_source_record_time")
        if source_id == "osm":
            if record_id != row["hotel_id"] or source["url"] != row["source_url"] or source["license"] != "ODbL-1.0" or source["license_url"] != "https://opendatacommons.org/licenses/odbl/1-0/":
                raise ValueError("invalid_osm_lineage")
            if modified > dataset_at or source["source_updated_at"] != row["source_updated_at"] or acquired != _time(row["retrieved_at"]):
                raise ValueError("invalid_osm_time")
            if source["link_method"] != "primary" or source["link_status"] != "primary":
                raise ValueError("invalid_osm_link")
        else:
            if not re.fullmatch(r"wikidata:Q[1-9][0-9]*", record_id) or source["url"] != f"https://www.wikidata.org/wiki/{record_id.split(':')[1]}":
                raise ValueError("invalid_wikidata_identity")
            if source["license"] != "CC0-1.0" or source["license_url"] != "https://creativecommons.org/publicdomain/zero/1.0/":
                raise ValueError("invalid_wikidata_license")
            if source["link_method"] != "osm_wikidata_tag" or source["link_status"] not in {"linked", "name_conflict", "coordinate_conflict"}:
                raise ValueError("invalid_wikidata_link")
        if type(source["revision"]) is not int or source["revision"] <= 0:
            raise ValueError("invalid_source_revision")
        fields = source["fields"]
        if not isinstance(fields, list) or not 1 <= len(fields) <= 64:
            raise ValueError("invalid_source_fields")
        for field in fields:
            if not isinstance(field, dict) or set(field) != {"field", "value", "pointer", "statement_id", "qualifiers", "references"}:
                raise ValueError("invalid_field_lineage")
            if not isinstance(field["qualifiers"], dict) or not isinstance(field["references"], list):
                raise ValueError("invalid_claim_context")
            pointer = field["pointer"]
            if not isinstance(pointer, str) or len(pointer) > 300:
                raise ValueError("invalid_source_pointer")
            if source_id == "osm":
                name = field["field"]
                if name == "coordinates":
                    if pointer != "/geometry" or not isinstance(field["value"], dict) or field["value"].get("latitude") != row["latitude"] or field["value"].get("longitude") != row["longitude"]:
                        raise ValueError("invalid_coordinate_lineage")
                elif name not in {f"tags.{key}" for key in OSM_RAW_KEYS} or pointer != f"/tags/{name[5:]}":
                    raise ValueError("invalid_osm_field")
                elif not isinstance(field["value"], str) or len(field["value"]) > 2000:
                    raise ValueError("invalid_osm_value")
            else:
                name = field["field"]
                if name not in WD_FIELDS:
                    raise ValueError("invalid_wikidata_field")
                if name.startswith("label."):
                    if pointer != f"/labels/{name[6:]}/value" or field["statement_id"] is not None or not isinstance(field["value"], str):
                        raise ValueError("invalid_wikidata_label")
                elif not re.fullmatch(rf"/claims/{WD_PROPERTIES[name]}/[0-9]+/mainsnak/datavalue/value", pointer):
                    raise ValueError("invalid_wikidata_property_pointer")
                elif not isinstance(field["statement_id"], str) or not field["statement_id"].startswith(record_id.split(":")[1] + "$"):
                    raise ValueError("invalid_wikidata_statement")
    primary = sources[0]
    if primary["source_id"] != "osm":
        raise ValueError("missing_primary_osm_record")
    raw = {field["field"]: field["value"] for field in primary["fields"]}
    if raw.get("tags.name") != row["name"] or raw.get("tags.tourism") != row["kind"]:
        raise ValueError("preferred_fact_lineage_mismatch")
    if any(raw.get(f"tags.{source_name}") != row[target] for source_name, target in (("name:en", "name_en"), ("name:ru", "name_ru"))):
        raise ValueError("preferred_name_lineage_mismatch")
    expected_tags = {key: raw[f"tags.{key}"] for key in TAG_KEYS if f"tags.{key}" in raw}
    if json.loads(row["tags_json"]) != expected_tags:
        raise ValueError("preferred_tag_lineage_mismatch")
    source_website = raw.get("tags.website") or raw.get("tags.contact:website")
    expected_website = source_website if _public_website(source_website) else None
    if row["website"] != expected_website:
        raise ValueError("preferred_website_lineage_mismatch")
    for source in sources[1:]:
        if source["source_id"] != "wikidata" or raw.get("tags.wikidata") != source["record_id"].split(":")[1]:
            raise ValueError("wikidata_link_not_explicit")
    return sources


def validate_v2_rows(rows: list[dict[str, Any]], manifest: dict[str, Any]) -> None:
    """Pure bounded release validation shared by offline builder and local loader."""
    retrieved, dataset_at = _metadata(manifest)
    if not isinstance(rows, list) or not 1 <= len(rows) <= MAX_ROWS:
        raise ValueError("invalid_count")
    origins = manifest.get("source_origins")
    if manifest.get("license") != "ODbL-1.0" or not isinstance(origins, dict) or set(origins) != {"osm", "wikidata"}:
        raise ValueError("missing_source_origins")
    if any(not isinstance(value, dict) for value in origins.values()) or origins["osm"].get("license") != "ODbL-1.0" or origins["wikidata"].get("license") != "CC0-1.0":
        raise ValueError("invalid_source_origin_license")
    seen = set()
    for row in rows:
        if set(row) != FIELDS:
            raise ValueError("invalid_columns")
        identity = row["hotel_id"]
        if not isinstance(identity, str) or not re.fullmatch(r"osm:(node|way|relation):[1-9][0-9]*", identity) or identity in seen:
            raise ValueError("invalid_identity")
        seen.add(identity)
        element, number = identity.split(":")[1:]
        if row["source_url"] != f"https://www.openstreetmap.org/{element}/{number}":
            raise ValueError("invalid_source_url")
        if row["kind"] not in KINDS or row["country_code"] != "CY":
            raise ValueError("invalid_kind_or_country")
        lat, lon = row["latitude"], row["longitude"]
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in (lat, lon)):
            raise ValueError("invalid_coordinates")
        if not BOUNDS[0] <= lat <= BOUNDS[2] or not BOUNDS[1] <= lon <= BOUNDS[3]:
            raise ValueError("invalid_geography")
        nearest = min(LOCALITIES, key=lambda city: (_distance(lat, lon, *LOCALITIES[city][:2]), city))
        expected_city = nearest if _distance(lat, lon, *LOCALITIES[nearest][:2]) <= RADIUS_KM else "cyprus_other"
        if row["destination_id"] != expected_city:
            raise ValueError("invalid_primary_locality")
        for key in ("name", "name_en", "name_ru"):
            value = row[key]
            if (key == "name" and (not isinstance(value, str) or not value.strip())) or (value is not None and (not isinstance(value, str) or len(value) > 200 or re.search(r"[\x00-\x1f]", value))):
                raise ValueError("invalid_name")
        if not isinstance(row["tags_json"], str) or len(row["tags_json"]) > 5000:
            raise ValueError("invalid_tags")
        tags = json.loads(row["tags_json"])
        if not isinstance(tags, dict) or set(tags) - TAG_KEYS or any(not isinstance(v, str) or len(v) > 200 for v in tags.values()):
            raise ValueError("invalid_tags")
        if not _public_website(row["website"]) or _time(row["retrieved_at"]) != retrieved:
            raise ValueError("invalid_metadata")
        if row["source_updated_at"] is not None and _time(row["source_updated_at"]) > dataset_at:
            raise ValueError("invalid_object_time")
        _validate_sources(row, dataset_at)
    coverage = {city: sum(_distance(r["latitude"], r["longitude"], *value[:2]) <= RADIUS_KM for r in rows) for city, value in LOCALITIES.items()}
    if manifest.get("coverage") != coverage:
        raise ValueError("invalid_coverage")


@dataclass(frozen=True)
class CatalogOutcome:
    status: str
    reason: str
    hotels: tuple[dict[str, Any], ...] = ()
    release_id: str | None = None
    retrieved_at: str | None = None


class LocalHotelCatalog:
    def __init__(self, directory: str | Path | None = None, *, enabled: bool | None = None):
        self.enabled = os.getenv("LOCAL_CATALOG_ENABLED", "1") == "1" if enabled is None else enabled
        self.status = "disabled" if not self.enabled else "unavailable"
        self.reason = "catalog_disabled" if not self.enabled else "catalog_invalid"
        self.release_id: str | None = None
        self.retrieved_at: str | None = None
        self.source_dataset_at: str | None = None
        self._schema_version: str | None = None
        self._source_counts: dict[str, int] = {}
        self._rows: tuple[dict[str, Any], ...] = ()
        if not self.enabled:
            return
        try:
            self._load(Path(directory) if directory is not None else ASSETS)
        except (OSError, ValueError, TypeError, KeyError, duckdb.Error, OverflowError):
            # Never download/repair a failed artifact or expose paths/parser details.
            self._rows = ()
            self.release_id = self.retrieved_at = self.source_dataset_at = None

    def _load(self, directory: Path) -> None:
        manifest = json.loads(_file(directory / "manifest.json"))
        if not isinstance(manifest, dict) or manifest["schema_version"] not in {SCHEMA_VERSION, LEGACY_SCHEMA_VERSION}:
            raise ValueError("invalid_schema")
        legacy = manifest["schema_version"] == LEGACY_SCHEMA_VERSION
        count = manifest["row_count"]
        if type(count) is not int or not 1 <= count <= (500 if legacy else MAX_ROWS) or manifest["license"] != "ODbL-1.0":
            raise ValueError("invalid_manifest")
        release_id = manifest["release_id"]
        if not isinstance(release_id, str) or not re.fullmatch(r"[A-Za-z0-9._:-]{1,120}", release_id):
            raise ValueError("invalid_release")
        retrieved, dataset_at = _metadata(manifest)
        parquet = (directory / "hotels.parquet").resolve()
        if _sha(_file(parquet)) != manifest["parquet_sha256"] or _sha(_file(directory / "source.json")) != manifest["source_sha256"]:
            raise ValueError("checksum_mismatch")
        with duckdb.connect(config={"threads": 1, "memory_limit": "64MB", "autoinstall_known_extensions": False,
                                    "autoload_known_extensions": False}) as connection:
            connection.execute("SET allowed_paths = ?", [[str(parquet)]])
            connection.execute("SET enable_external_access = false")
            result = connection.execute("SELECT * FROM read_parquet(?) LIMIT 5001", [str(parquet)])
            names = [column[0] for column in result.description]
            if set(names) != (LEGACY_FIELDS if legacy else FIELDS):
                raise ValueError("invalid_columns")
            rows = [dict(zip(names, row)) for row in result.fetchall()]
        if len(rows) != count:
            raise ValueError("invalid_count")
        if not legacy:
            validate_v2_rows(rows, manifest)
            if manifest.get("kind_counts") != dict(Counter(row["kind"] for row in rows)):
                raise ValueError("invalid_kind_counts")
            parsed = []
            for row in rows:
                parsed.append({key: value for key, value in row.items() if key not in {"tags_json", "source_records_json"}} |
                              {"tags": json.loads(row["tags_json"]), "source_records": json.loads(row["source_records_json"])})
            source_counts = dict(Counter(record["source_id"] for row in parsed for record in row["source_records"]))
            if manifest.get("source_record_counts") != source_counts:
                raise ValueError("invalid_source_record_counts")
            self._rows = tuple(sorted(parsed, key=lambda row: row["hotel_id"]))
            self._source_counts = source_counts
            self._schema_version = SCHEMA_VERSION
            self.release_id, self.retrieved_at = release_id, manifest["retrieved_at"]
            self.source_dataset_at = manifest["source_dataset_at"]
            self.status, self.reason = "ready", "catalog_ready"
            return
        seen: set[str] = set()
        coverage = {key: 0 for key in ("larnaca", "paphos")}
        for row in rows:
            identity = row["hotel_id"]
            if not isinstance(identity, str) or not re.fullmatch(r"osm:(node|way|relation):[1-9][0-9]*", identity) or identity in seen:
                raise ValueError("invalid_identity")
            seen.add(identity)
            kind, number = identity.split(":")[1:]
            if row["source_url"] != f"https://www.openstreetmap.org/{kind}/{number}":
                raise ValueError("invalid_source_url")
            city = row["destination_id"]
            if city not in coverage or row["country_code"] != "CY":
                raise ValueError("invalid_scope")
            lat, lon = row["latitude"], row["longitude"]
            if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in (lat, lon)):
                raise ValueError("invalid_coordinates")
            if not (-90 <= lat <= 90 and -180 <= lon <= 180) or _distance(lat, lon, *LOCALITIES[city][:2]) > RADIUS_KM:
                raise ValueError("invalid_geography")
            for key in ("name", "name_en", "name_ru"):
                value = row[key]
                if (key == "name" and (not isinstance(value, str) or not value.strip())) or (value is not None and (not isinstance(value, str) or len(value) > 200 or re.search(r"[\x00-\x1f]", value))):
                    raise ValueError("invalid_name")
            if not isinstance(row["tags_json"], str) or len(row["tags_json"]) > 5000:
                raise ValueError("invalid_tags")
            tags = json.loads(row.pop("tags_json"))
            if not isinstance(tags, dict) or set(tags) - TAG_KEYS or any(not isinstance(v, str) or len(v) > 200 for v in tags.values()):
                raise ValueError("invalid_tags")
            row["tags"] = tags
            if not _public_website(row["website"]) or _time(row["retrieved_at"]) != retrieved:
                raise ValueError("invalid_metadata")
            if row["source_updated_at"] is not None and _time(row["source_updated_at"]) > dataset_at:
                raise ValueError("invalid_object_time")
            coverage[city] += 1
            row["kind"], row["source_records"] = "hotel", []
        # Coverage is derived from validated rows; inconsistent advertised counts fail closed.
        advertised = manifest["coverage"]
        if not isinstance(advertised, dict) or any(advertised.get(city) != value for city, value in coverage.items()):
            raise ValueError("invalid_coverage")
        self._rows = tuple(sorted(rows, key=lambda row: row["hotel_id"]))
        self._schema_version = LEGACY_SCHEMA_VERSION
        self._source_counts = {"osm": len(rows)}
        self.release_id, self.retrieved_at = release_id, manifest["retrieved_at"]
        self.source_dataset_at = manifest["source_dataset_at"]
        self.status, self.reason = "ready", "catalog_ready"

    def capabilities(self) -> dict[str, Any]:
        sources = "OpenStreetMap + Wikidata" if self._source_counts.get("wikidata") else "OpenStreetMap"
        return {"name": "Локальный каталог размещения Кипра / " + sources, "configured": self.status == "ready",
                "status": self.status, "reason": self.reason, "row_count": len(self._rows), "release_id": self.release_id,
                "retrieved_at": self.retrieved_at, "source_dataset_at": self.source_dataset_at,
                "schema_version": self._schema_version,
                "coverage": {city: sum(_distance(row["latitude"], row["longitude"], *value[:2]) <= RADIUS_KM for row in self._rows) for city, value in LOCALITIES.items()},
                "coverage_counts_overlap": True, "kind_counts": dict(Counter(row["kind"] for row in self._rows)),
                "source_record_counts": dict(self._source_counts), "count_unit": "named_source_poi_not_verified_operating_property",
                "radius_km": RADIUS_KM, "license": "ODbL-1.0", "source_url": "https://www.openstreetmap.org/copyright",
                "capability": "snapshot_property_facts_not_inventory"}

    def search(self, place: Place, intent: TravelIntent, limit: int = 3) -> CatalogOutcome:
        def outcome(status: str, reason: str, hotels=()) -> CatalogOutcome:
            return CatalogOutcome(status, reason, tuple(hotels), self.release_id, self.retrieved_at)

        if self.status != "ready":
            return outcome("disabled" if not self.enabled else "unavailable", self.reason)
        city = next((key for key, (_, _, aliases) in LOCALITIES.items() if normalized(place.name) in aliases), None)
        if place.country_code.upper() != "CY" or city is None:
            return outcome("out_of_scope", "destination_not_covered")
        if not all(math.isfinite(value) for value in (place.latitude, place.longitude)):
            return outcome("out_of_scope", "destination_not_covered")
        anchor = LOCALITIES[city][:2]
        if _distance(place.latitude, place.longitude, *anchor) > RADIUS_KM:
            return outcome("out_of_scope", "destination_not_covered")
        tokens = set(normalized(" ".join([*intent.hotel_requirements, *intent.soft_constraints])).split())
        tokens = {value for value in tokens if len(value) > 2}
        pet_query = normalized(" ".join(intent.pets))
        dog_requested = bool(re.search(r"dog|собак|щен", pet_query))
        ranked = []
        for row in self._rows:
            distance = _distance(*anchor, row["latitude"], row["longitude"])
            if distance > RADIUS_KM:
                continue
            tags = row["tags"]
            raw_names = " ".join(row[key] or "" for key in ("name", "name_en", "name_ru"))
            names = normalized(raw_names)
            min_age = tags.get("min_age", "")
            adults_only = tags.get("adults_only", "").lower() == "yes" or bool(re.search(r"adults?\W*only|только\W*взросл|18\s*\+", raw_names, re.IGNORECASE)) or (min_age.isdigit() and int(min_age) >= 18)
            if intent.children_ages and adults_only:
                continue
            if intent.pets and (tags.get("pets", "").lower() == "no" or (dog_requested and tags.get("dog", "").lower() == "no")):
                continue
            score = sum(token in names for token in tokens) * 4
            if intent.pets and (tags.get("pets") == "yes" or (dog_requested and tags.get("dog") == "yes")):
                score += 2
            # Weak source-attribute relevance only; downstream checks remain unknown.
            for requested, tag in (({"pool", "бассейн", "бассейном"}, "swimming_pool"),
                                   ({"wheelchair", "коляска", "коляске", "безбарьерный"}, "wheelchair"),
                                   ({"wifi", "интернет"}, "internet_access")):
                if tokens & requested and tags.get(tag) in ("yes", "wlan", "free"):
                    score += 1
            ranked.append((-score, round(distance, 6), row["hotel_id"], row))
        ranked.sort(key=lambda item: item[:3])
        selected = [deepcopy(item[3]) for item in ranked[:max(1, min(3, int(limit)))]]
        return outcome("matched" if selected else "empty", "local_match" if selected else "no_eligible_local_hotels", selected)
