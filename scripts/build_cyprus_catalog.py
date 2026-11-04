"""Rebuild the bounded Cyprus accommodation Parquet snapshot without network access.

Default: python scripts/build_cyprus_catalog.py
Optional acquisition: download the identified Geofabrik PBF separately, then run
  python scripts/build_cyprus_catalog.py --pbf tmp/catalog-source/cyprus-261024.osm.pbf
    --retrieved-at <actual-download-UTC-time> --wikidata-directory <saved-entity-JSON-directory>
PBF import additionally needs the development-only package osmium==4.2.0.
The directory contains wikidata-acquisition.json (IDs, revision IDs, original
modified times, actual retrieval times and file hashes) and its previously
downloaded wikidata-entity-Q*.json files. No acquisition occurs in this command.
The application never imports this builder or downloads catalog data.
"""
from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import math
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import duckdb

ASSETS = Path(__file__).resolve().parents[1] / "backend/app/catalog/assets/cyprus"
SCHEMA_VERSION = "cyprus-hotels-v1"
RELEASE_ID = "cyprus-hotels-20261025-v1"
SOURCE_ENDPOINT = "https://download.geofabrik.de/europe/cyprus-261024.osm.pbf"
SOURCE_RAW_SHA256 = "f272eccee93e84d257570261522f6fc048c39a16c99e38d5bf8eebe035fef416"
LICENSE_URL = "https://opendatacommons.org/licenses/odbl/1-0/"
ATTRIBUTION = "© OpenStreetMap contributors"
CENTERS = {"larnaca": (34.92, 33.63), "paphos": (34.77, 32.42)}
RADIUS_KM = 8.0
TAGS = frozenset({
    "stars", "wheelchair", "internet_access", "air_conditioning", "swimming_pool",
    "dog", "pets", "addr:street", "addr:housenumber", "adults_only", "min_age",
})
COLUMNS = (
    "hotel_id", "name", "name_en", "name_ru", "destination_id", "country_code",
    "latitude", "longitude", "tags_json", "website", "source_url",
    "source_updated_at", "retrieved_at",
)
SOURCE_QUERY = (
    "Geofabrik Cyprus dated bulk extract; named tourism=hotel nodes and ways; "
    "explicit disused=yes/abandoned=yes excluded; node coordinates or way bounding-box "
    "midpoint within 8km of Larnaca(34.92,33.63) or Paphos(34.77,32.42); "
    "deduplicate matching normalized hotel names within 150m, preferring more allowed "
    "source attributes then newer object; retain only allowlisted fields, at most 500 rows. "
    "Coordinates are map observations; source object timestamps are not hotel verification times."
)


def json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def iso_utc(value: str) -> str:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Catalog timestamps require an explicit UTC offset")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def distance_km(lat: float, lon: float, other_lat: float, other_lon: float) -> float:
    p1, p2 = math.radians(lat), math.radians(other_lat)
    dp, dl = p2 - p1, math.radians(other_lon - lon)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 6371.0088 * 2 * math.atan2(math.sqrt(a), math.sqrt(max(0.0, 1 - a)))


def public_https(value: str | None) -> str | None:
    if not value:
        return None
    if len(value) > 2000 or re.search(r"[\x00-\x20\\]", value):
        return None
    try:
        parsed = urlsplit(value)
        host = parsed.hostname or ""
        if parsed.scheme != "https" or not host or parsed.username or parsed.password or parsed.port not in (None, 443):
            return None
    except ValueError:
        return None
    if host.lower() in {"localhost", "localhost.localdomain"} or "." not in host:
        return None
    try:
        if not ipaddress.ip_address(host).is_global:
            return None
    except ValueError:
        if host.lower().endswith((".local", ".internal", ".localhost")):
            return None
    return value


def _name_identity(name: str) -> str:
    tokens = re.findall(r"[^\W_]+", name.casefold(), re.UNICODE)
    generic = {"the", "hotel", "hotels", "boutique", "resort", "beach", "5", "4", "3", "2", "1"}
    return " ".join(token for token in tokens if token not in generic)


def _import_v1_pbf(path: Path, retrieved_at: str) -> dict[str, Any]:
    """Read one operator-downloaded file; this function performs no HTTP requests."""
    # The endpoint/release above identify one reviewed immutable input. Importing
    # another file requires an explicit source/release update, not a false URL.
    if sha256(path) != SOURCE_RAW_SHA256:
        raise ValueError("PBF does not match the reviewed dated source checksum")
    # Bound the optional native parser and avoid its default Windows pool shutdown
    # deadlock observed on this host. Both switches are documented by libosmium.
    os.environ["OSMIUM_POOL_THREADS"] = "1"
    os.environ["OSMIUM_USE_POOL_THREADS_FOR_PBF_PARSING"] = "false"
    import osmium  # optional acquisition tool; not a runtime dependency

    rows: list[dict[str, Any]] = []
    reader = osmium.io.Reader(str(path), osmium.osm.osm_entity_bits.NOTHING)
    dataset_at = reader.header().get("osmosis_replication_timestamp")
    reader.close()
    if not dataset_at:
        raise ValueError("PBF header has no original dataset timestamp")

    class Hotels(osmium.SimpleHandler):
        def add(self, obj: Any, kind: str, lat: float, lon: float) -> None:
            tags = dict(obj.tags)
            if tags.get("tourism") != "hotel" or not tags.get("name"):
                return
            if tags.get("disused") == "yes" or tags.get("abandoned") == "yes":
                return
            city = next((city for city, center in CENTERS.items()
                         if distance_km(lat, lon, *center) <= RADIUS_KM), None)
            if city is None:
                return
            rows.append({
                "hotel_id": f"osm:{kind}:{obj.id}", "name": tags["name"],
                "name_en": tags.get("name:en"), "name_ru": tags.get("name:ru"),
                "destination_id": city, "country_code": "CY",
                "latitude": round(lat, 7), "longitude": round(lon, 7),
                "tags_json": json.dumps({k: v for k, v in sorted(tags.items()) if k in TAGS},
                                        ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                "website": public_https(tags.get("website") or tags.get("contact:website")),
                "source_url": f"https://www.openstreetmap.org/{kind}/{obj.id}",
                "source_updated_at": iso_utc(str(obj.timestamp)), "retrieved_at": retrieved_at,
            })

        def node(self, obj: Any) -> None:
            if obj.tags.get("tourism") == "hotel" and obj.location.valid():
                self.add(obj, "node", obj.location.lat, obj.location.lon)

        def way(self, obj: Any) -> None:
            if obj.tags.get("tourism") != "hotel":
                return
            coords = [(n.lat, n.lon) for n in obj.nodes if n.location.valid()]
            if coords:
                lat = (min(p[0] for p in coords) + max(p[0] for p in coords)) / 2
                lon = (min(p[1] for p in coords) + max(p[1] for p in coords)) / 2
                self.add(obj, "way", lat, lon)

    Hotels().apply_file(str(path), locations=True)
    # Keep one original object rather than merging observations from different dates.
    preferred = sorted(rows, key=lambda row: (
        -len(json.loads(row["tags_json"])),
        -datetime.fromisoformat(row["source_updated_at"].replace("Z", "+00:00")).timestamp(),
        row["hotel_id"],
    ))
    unique: list[dict[str, Any]] = []
    for row in preferred:
        identity = _name_identity(row["name"])
        duplicate = identity and any(
            _name_identity(other["name"]) == identity
            and distance_km(row["latitude"], row["longitude"], other["latitude"], other["longitude"]) <= .15
            for other in unique
        )
        if not duplicate:
            unique.append(row)
    unique.sort(key=lambda row: row["hotel_id"])
    if len(unique) > 500:
        raise ValueError("Acquisition exceeds the reviewed 500-row bound")
    coverage = {city: sum(r["destination_id"] == city for r in unique) for city in CENTERS}
    return {
        "schema_version": SCHEMA_VERSION, "release_id": RELEASE_ID,
        "retrieved_at": retrieved_at, "source_dataset_at": iso_utc(dataset_at),
        "source_endpoint": SOURCE_ENDPOINT, "source_query": SOURCE_QUERY,
        "raw_sha256": SOURCE_RAW_SHA256, "coverage": coverage,
        "coverage_radius_km": RADIUS_KM, "coverage_centers": CENTERS,
        "license": "ODbL-1.0", "license_url": LICENSE_URL, "attribution": ATTRIBUTION,
        "hotels": unique,
    }


def _validate_v1_source(source: dict[str, Any]) -> list[dict[str, Any]]:
    if source.get("schema_version") != SCHEMA_VERSION or source.get("license") != "ODbL-1.0":
        raise ValueError("Unsupported catalog schema or license")
    for key in ("retrieved_at", "source_dataset_at"):
        iso_utc(source[key])
    dataset_at = datetime.fromisoformat(iso_utc(source["source_dataset_at"]).replace("Z", "+00:00"))
    retrieved_at = datetime.fromisoformat(iso_utc(source["retrieved_at"]).replace("Z", "+00:00"))
    if dataset_at > retrieved_at:
        raise ValueError("Dataset time is newer than its acquisition")
    for key in ("release_id", "source_endpoint", "source_query", "license_url", "attribution"):
        if not isinstance(source.get(key), str) or not source[key]:
            raise ValueError(f"Missing source metadata: {key}")
    if not re.fullmatch(r"[0-9a-f]{64}", source.get("raw_sha256", "")):
        raise ValueError("Invalid raw source checksum")
    rows = source.get("hotels")
    if not isinstance(rows, list) or not 20 <= len(rows) <= 500:
        raise ValueError("Catalog needs between 20 and 500 source rows")
    seen: set[str] = set()
    for row in rows:
        if set(row) != set(COLUMNS):
            raise ValueError("Unexpected hotel fields")
        match = re.fullmatch(r"osm:(node|way|relation):([1-9][0-9]*)", row["hotel_id"])
        if match is None or row["hotel_id"] in seen:
            raise ValueError("Invalid or duplicate stable hotel identity")
        seen.add(row["hotel_id"])
        if not isinstance(row["name"], str) or not row["name"].strip() or len(row["name"]) > 200:
            raise ValueError("Invalid hotel name")
        city = row["destination_id"]
        if row["country_code"] != "CY" or city not in CENTERS:
            raise ValueError("Hotel is outside supported destinations")
        if not all(isinstance(row[k], (int, float)) and math.isfinite(row[k]) for k in ("latitude", "longitude")):
            raise ValueError("Invalid coordinates")
        if distance_km(row["latitude"], row["longitude"], *CENTERS[city]) > RADIUS_KM:
            raise ValueError("Hotel is outside its city radius")
        tags = json.loads(row["tags_json"])
        if not isinstance(tags, dict) or set(tags) - TAGS or not all(isinstance(v, str) and len(v) <= 200 for v in tags.values()):
            raise ValueError("Non-allowlisted source tags")
        if row["website"] is not None and public_https(row["website"]) != row["website"]:
            raise ValueError("Hotel website must be public HTTPS")
        if row["source_url"] != f"https://www.openstreetmap.org/{match[1]}/{match[2]}":
            raise ValueError("Source link does not match the stable identity")
        if row["source_updated_at"] is not None:
            if datetime.fromisoformat(iso_utc(row["source_updated_at"]).replace("Z", "+00:00")) > dataset_at:
                raise ValueError("Source object timestamp is newer than its extract")
        if row["retrieved_at"] != source["retrieved_at"]:
            raise ValueError("Hotel observation time differs from acquisition")
    expected = {city: sum(row["destination_id"] == city for row in rows) for city in CENTERS}
    if source.get("coverage") != expected:
        raise ValueError("Coverage metadata differs from the bounded hotel rows")
    return sorted(rows, key=lambda row: row["hotel_id"])


def _build_v1(source: dict[str, Any], output: Path) -> dict[str, Any]:
    rows = _validate_v1_source(source)
    output.mkdir(parents=True, exist_ok=True)
    source_path = output / "source.json"
    source_path.write_bytes(json_bytes({**source, "hotels": rows}))
    parquet_path = output / "hotels.parquet"
    with duckdb.connect(config={"threads": 1, "memory_limit": "64MB", "autoload_known_extensions": False,
                                "autoinstall_known_extensions": False}) as connection:
        columns = ",".join(f"{column} {'DOUBLE' if column in {'latitude', 'longitude'} else 'VARCHAR'}" for column in COLUMNS)
        connection.execute(f"CREATE TABLE hotels ({columns})")
        connection.executemany(f"INSERT INTO hotels VALUES ({','.join('?' for _ in COLUMNS)})",
                               [tuple(row[column] for column in COLUMNS) for row in rows])
        connection.execute("COPY (SELECT * FROM hotels ORDER BY hotel_id) TO ? (FORMAT PARQUET, COMPRESSION ZSTD)",
                           [str(parquet_path)])
    manifest = {key: source[key] for key in (
        "schema_version", "release_id", "retrieved_at", "source_dataset_at", "source_endpoint",
        "source_query", "raw_sha256", "coverage", "coverage_radius_km", "coverage_centers",
        "license", "license_url", "attribution",
    )}
    manifest.update(source_sha256=sha256(source_path), parquet_sha256=sha256(parquet_path), row_count=len(rows))
    (output / "manifest.json").write_bytes(json_bytes(manifest))
    (output / "LICENSE.md").write_text(
        "# Cyprus hotel snapshot data license\n\n"
        "© OpenStreetMap contributors. This derived database is distributed under the "
        "[Open Data Commons Open Database License 1.0](https://opendatacommons.org/licenses/odbl/1-0/). "
        "The individual database contents are available under the "
        "[Database Contents License](https://opendatacommons.org/licenses/dbcl/1-0/).\n\n"
        "Source: [Geofabrik Cyprus extract, 24 October 2026](https://download.geofabrik.de/europe/cyprus-261024.osm.pbf). "
        "See [OpenStreetMap copyright and attribution](https://www.openstreetmap.org/copyright). "
        "source.json and hotels.parquet contain the same bounded derived database. "
        "This data license is separate from the application code license.\n\n"
        "Map facts are community observations, not verified hotel conditions, live room availability or prices. "
        "Way coordinates are bounding-box midpoints. The slice includes hotel POIs within 8km of the "
        "documented Larnaca and Paphos centers, including nearby settlements, and is not complete inventory. "
        "Object edit timestamps describe OSM edits, not property verification dates.\n",
        encoding="utf-8", newline="\n",
    )
    return manifest


V2_SCHEMA_VERSION = "cyprus-lodgings-v2"
V2_RELEASE_ID = "cyprus-lodgings-20261025-v2"
V2_COLUMNS = (*COLUMNS, "kind", "source_records_json")
V2_CENTERS = {**CENTERS, "limassol": (34.68, 33.04), "nicosia": (35.17, 33.36),
              "ayia_napa": (34.99, 34.00), "protaras": (35.01, 34.05),
              "polis": (35.03, 32.43), "troodos": (34.92, 32.88)}
V2_KINDS = frozenset({"hotel", "apartment", "guest_house", "hostel", "chalet", "motel"})
V2_TAGS = TAGS | {"addr:city", "addr:postcode", "addr:country", "opening_hours", "check_date", "start_date"}
V2_BOUNDS = (34.5, 32.2, 35.8, 34.7)
WD_PROPERTIES = {"P31": "instance_of", "P625": "coordinates", "P17": "country",
                 "P131": "administrative_area", "P856": "website", "P6375": "street_address",
                 "P571": "inception", "P1619": "official_opening", "P576": "dissolution"}
SOURCE_RECORD_KEYS = {"source_id", "record_id", "url", "revision", "source_updated_at", "retrieved_at",
                      "license", "license_url", "link_method", "link_status", "fields"}
V2_QUERY = (
    "Pinned Geofabrik Cyprus island bulk extract; named tourism=hotel/apartment/guest_house/hostel/chalet/motel "
    "nodes, ways and multipolygon relations; explicit disused=yes/abandoned=yes excluded; "
    "node locations or geometry bounding-box midpoints. Stable source IDs deduplicated; "
    "similar names and nearby map representations are not assumed to be the same property. "
    "Nearest reviewed locality within 8km or cyprus_other; search circles overlap. "
    "Independent Wikidata entity JSON is attached only by existing OSM wikidata=Q identifier; "
    "claims, revisions, references and conflicting names/coordinates remain separate and never overwrite OSM facts."
)


def _lineage(field: str, value: Any, pointer: str, statement: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"field": field, "value": value, "pointer": pointer,
            "statement_id": statement.get("id") if statement else None,
            "qualifiers": statement.get("qualifiers", {}) if statement else {},
            "references": statement.get("references", []) if statement else []}


def _source_record(source_id: str, record_id: str, url: str, modified: str, retrieved: str,
                   fields: list[dict[str, Any]], *, revision: int | None = None,
                   link_status: str = "primary") -> dict[str, Any]:
    osm = source_id == "osm"
    return {"source_id": source_id, "record_id": record_id, "url": url, "revision": revision,
            "source_updated_at": modified, "retrieved_at": retrieved,
            "license": "ODbL-1.0" if osm else "CC0-1.0",
            "license_url": LICENSE_URL if osm else "https://creativecommons.org/publicdomain/zero/1.0/",
            "link_method": "primary" if osm else "osm_wikidata_tag", "link_status": link_status,
            "fields": fields}


def _wikidata_sources(directory: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Only read previously acquired public JSON, with its acquisition timestamps."""
    acquisitions = json.loads((directory / "wikidata-acquisition.json").read_text(encoding="utf-8"))
    entities, metadata = {}, []
    if acquisitions.get("license") != "CC0-1.0" or not 1 <= len(acquisitions.get("records", [])) <= 50:
        raise ValueError("Wikidata acquisition metadata is missing or unbounded")
    for acquisition in acquisitions["records"]:
        qid = acquisition["id"]
        if not re.fullmatch(r"Q[1-9][0-9]*", qid) or qid in entities:
            raise ValueError("Invalid Wikidata identity")
        name = f"wikidata-entity-{qid}.json"
        if acquisition["file"] != name:
            raise ValueError("Unexpected acquisition filename")
        path = directory / name
        if path.stat().st_size > 1024 * 1024 or sha256(path) != acquisition["sha256"]:
            raise ValueError("Wikidata raw entity checksum mismatch")
        entity = json.loads(path.read_text(encoding="utf-8"))["entities"][qid]
        if entity["id"] != qid or entity["lastrevid"] != acquisition["revision"] or entity["modified"] != acquisition["source_updated_at"]:
            raise ValueError("Wikidata revision metadata mismatch")
        entities[qid] = (entity, acquisition)
        metadata.append(dict(acquisition))
    return entities, sorted(metadata, key=lambda value: value["id"])


def import_pbf(path: Path, retrieved_at: str, wikidata_directory: Path | None = None) -> dict[str, Any]:
    if sha256(path) != SOURCE_RAW_SHA256:
        raise ValueError("PBF does not match the reviewed dated source checksum")
    if wikidata_directory is None:
        raise ValueError("V2 acquisition requires previously downloaded --wikidata-directory JSON")
    os.environ["OSMIUM_POOL_THREADS"] = "1"
    os.environ["OSMIUM_USE_POOL_THREADS_FOR_PBF_PARSING"] = "false"
    import osmium
    entities, acquisitions = _wikidata_sources(wikidata_directory)
    reader = osmium.io.Reader(str(path), osmium.osm.osm_entity_bits.NOTHING)
    dataset_at = iso_utc(reader.header().get("osmosis_replication_timestamp"))
    reader.close()
    rows, source_ids = [], set()

    class Lodgings(osmium.SimpleHandler):
        def add(self, obj: Any, element: str, lat: float, lon: float, method: str, number: int | None = None):
            raw = dict(obj.tags)
            if raw.get("tourism") not in V2_KINDS or not raw.get("name") or raw.get("disused") == "yes" or raw.get("abandoned") == "yes":
                return
            identity = f"osm:{element}:{number or obj.id}"
            if identity in source_ids:
                return
            source_ids.add(identity)
            source_url = f"https://www.openstreetmap.org/{element}/{number or obj.id}"
            modified = iso_utc(str(obj.timestamp))
            raw_keys = V2_TAGS | {"name", "name:en", "name:ru", "name:el", "tourism", "website", "contact:website", "wikidata"}
            fields = [_lineage(f"tags.{key}", value, f"/tags/{key}") for key, value in sorted(raw.items()) if key in raw_keys]
            fields.append(_lineage("coordinates", {"latitude": round(lat, 7), "longitude": round(lon, 7), "method": method}, "/geometry"))
            sources = [_source_record("osm", identity, source_url, modified, retrieved_at, fields, revision=obj.version)]
            if raw.get("wikidata") in entities:
                entity, acquisition = entities[raw["wikidata"]]
                wd_fields = [_lineage(f"label.{language}", label["value"], f"/labels/{language}/value")
                             for language, label in sorted(entity.get("labels", {}).items()) if language in {"en", "ru", "el"}]
                for prop, field in WD_PROPERTIES.items():
                    for index, statement in enumerate(entity.get("claims", {}).get(prop, [])):
                        snak = statement.get("mainsnak", {})
                        if statement.get("rank") != "deprecated" and snak.get("snaktype") == "value":
                            wd_fields.append(_lineage(field, snak["datavalue"]["value"],
                                                      f"/claims/{prop}/{index}/mainsnak/datavalue/value", statement))
                coordinates = [field["value"] for field in wd_fields if field["field"] == "coordinates"]
                conflict = any(distance_km(lat, lon, c["latitude"], c["longitude"]) > .25 for c in coordinates)
                english = entity.get("labels", {}).get("en", {}).get("value", "")
                # This detects disagreement only; the exact existing QID is the sole link key.
                words = lambda value: set(re.findall(r"[^\W_]+", value.casefold())) - {"hotel", "hotels", "boutique", "resort", "apartments", "apts", "hostel", "the"}
                name_conflict = english and not (words(raw["name"]) & words(english))
                status = "coordinate_conflict" if conflict else "name_conflict" if name_conflict else "linked"
                qid = entity["id"]
                sources.append(_source_record("wikidata", f"wikidata:{qid}", f"https://www.wikidata.org/wiki/{qid}",
                                              entity["modified"], iso_utc(acquisition["retrieved_at"]), wd_fields,
                                              revision=entity["lastrevid"], link_status=status))
            nearest = min(V2_CENTERS, key=lambda city: (distance_km(lat, lon, *V2_CENTERS[city]), city))
            city = nearest if distance_km(lat, lon, *V2_CENTERS[nearest]) <= RADIUS_KM else "cyprus_other"
            rows.append({"hotel_id": identity, "name": raw["name"], "name_en": raw.get("name:en"),
                         "name_ru": raw.get("name:ru"), "destination_id": city, "country_code": "CY",
                         "latitude": round(lat, 7), "longitude": round(lon, 7),
                         "tags_json": json.dumps({k: v for k, v in sorted(raw.items()) if k in V2_TAGS}, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                         "website": public_https(raw.get("website") or raw.get("contact:website")),
                         "source_url": source_url, "source_updated_at": modified, "retrieved_at": retrieved_at,
                         "kind": raw["tourism"], "source_records_json": json.dumps(sources, ensure_ascii=False, sort_keys=True, separators=(",", ":"))})

        def node(self, obj):
            if obj.tags.get("tourism") in V2_KINDS and obj.location.valid():
                self.add(obj, "node", obj.location.lat, obj.location.lon, "osm_node")

        def way(self, obj):
            if obj.tags.get("tourism") not in V2_KINDS:
                return
            coordinates = [(n.lat, n.lon) for n in obj.nodes if n.location.valid()]
            if coordinates:
                self.add(obj, "way", (min(c[0] for c in coordinates) + max(c[0] for c in coordinates)) / 2,
                         (min(c[1] for c in coordinates) + max(c[1] for c in coordinates)) / 2, "way_bbox_midpoint")

        def area(self, obj):
            if obj.from_way() or obj.tags.get("tourism") not in V2_KINDS:
                return
            coordinates = [(n.lat, n.lon) for ring in obj.outer_rings() for n in ring if n.location.valid()]
            if coordinates:
                self.add(obj, "relation", (min(c[0] for c in coordinates) + max(c[0] for c in coordinates)) / 2,
                         (min(c[1] for c in coordinates) + max(c[1] for c in coordinates)) / 2, "relation_outer_bbox_midpoint", obj.orig_id())

    Lodgings().apply_file(str(path), locations=True)
    rows.sort(key=lambda row: row["hotel_id"])
    coverage = {city: sum(distance_km(row["latitude"], row["longitude"], *center) <= RADIUS_KM for row in rows) for city, center in V2_CENTERS.items()}
    return {"schema_version": V2_SCHEMA_VERSION, "release_id": V2_RELEASE_ID, "retrieved_at": retrieved_at,
            "source_dataset_at": dataset_at, "source_endpoint": SOURCE_ENDPOINT, "source_query": V2_QUERY,
            "raw_sha256": SOURCE_RAW_SHA256, "coverage": coverage, "coverage_radius_km": RADIUS_KM,
            "coverage_centers": V2_CENTERS, "geographic_bounds": V2_BOUNDS, "license": "ODbL-1.0",
            "license_url": LICENSE_URL, "attribution": ATTRIBUTION + "; Wikidata (CC0)",
            "source_origins": {"osm": {"license": "ODbL-1.0", "endpoint": SOURCE_ENDPOINT, "raw_sha256": SOURCE_RAW_SHA256,
                                        "retrieved_at": retrieved_at, "source_dataset_at": dataset_at},
                               "wikidata": {"license": "CC0-1.0", "endpoint": "https://www.wikidata.org/wiki/Special:EntityData/",
                                            "license_url": "https://creativecommons.org/publicdomain/zero/1.0/", "acquisitions": acquisitions}},
            "hotels": rows}


def validate_source(source: dict[str, Any]) -> list[dict[str, Any]]:
    if source.get("schema_version") == SCHEMA_VERSION:
        return _validate_v1_source(source)
    # Use the same pure row/provenance validator as the runtime loader; this module
    # has no provider, HTTP, storage or LLM dependencies.
    import sys
    root = str(Path(__file__).resolve().parents[1])
    if root not in sys.path:
        sys.path.insert(0, root)
    from backend.app.catalog import validate_v2_rows
    rows = source.get("hotels")
    if source.get("schema_version") != V2_SCHEMA_VERSION or source.get("license") != "ODbL-1.0":
        raise ValueError("Unsupported catalog schema or license")
    if not isinstance(rows, list) or not 1 <= len(rows) <= 5000:
        raise ValueError("Catalog needs between 1 and 5000 records")
    if set(source.get("source_origins", {})) != {"osm", "wikidata"}:
        raise ValueError("Independent source origins are missing")
    validate_v2_rows(rows, source)
    return sorted(rows, key=lambda row: row["hotel_id"])


def build(source: dict[str, Any], output: Path) -> dict[str, Any]:
    if source.get("schema_version") == SCHEMA_VERSION:
        return _build_v1(source, output)
    rows = validate_source(source)
    output.mkdir(parents=True, exist_ok=True)
    source_path, parquet_path = output / "source.json", output / "hotels.parquet"
    source_path.write_bytes(json_bytes({**source, "hotels": rows}))
    with duckdb.connect(config={"threads": 1, "memory_limit": "64MB", "autoload_known_extensions": False,
                                "autoinstall_known_extensions": False}) as connection:
        columns = ",".join(f"{column} {'DOUBLE' if column in {'latitude', 'longitude'} else 'VARCHAR'}" for column in V2_COLUMNS)
        connection.execute(f"CREATE TABLE hotels ({columns})")
        connection.executemany(f"INSERT INTO hotels VALUES ({','.join('?' for _ in V2_COLUMNS)})",
                               [tuple(row[column] for column in V2_COLUMNS) for row in rows])
        connection.execute("COPY (SELECT * FROM hotels ORDER BY hotel_id) TO ? (FORMAT PARQUET, COMPRESSION ZSTD)", [str(parquet_path)])
    manifest = {key: value for key, value in source.items() if key != "hotels"}
    from collections import Counter
    sources = [record for row in rows for record in json.loads(row["source_records_json"])]
    manifest.update(source_sha256=sha256(source_path), parquet_sha256=sha256(parquet_path), row_count=len(rows),
                    kind_counts=dict(sorted(Counter(row["kind"] for row in rows).items())),
                    source_record_counts=dict(sorted(Counter(record["source_id"] for record in sources).items())),
                    link_status_counts=dict(sorted(Counter(record["link_status"] for record in sources if record["source_id"] == "wikidata").items())),
                    count_unit="named_source_poi_not_verified_operating_property", coverage_counts_overlap=True,
                    deduplication="Stable source identity only; no name-only or fuzzy merges")
    (output / "manifest.json").write_bytes(json_bytes(manifest))
    (output / "LICENSE.md").write_text(
        "# Cyprus accommodation snapshot licenses\n\n"
        "© OpenStreetMap contributors. The OSM-derived database is distributed under "
        "[Open Database License 1.0](https://opendatacommons.org/licenses/odbl/1-0/); individual contents are under "
        "[Database Contents License](https://opendatacommons.org/licenses/dbcl/1-0/). "
        "[Geofabrik source](https://download.geofabrik.de/europe/cyprus-261024.osm.pbf), "
        "[OSM copyright and attribution](https://www.openstreetmap.org/copyright).\n\n"
        "Independent structured Wikidata claims are "
        "[CC0 1.0](https://creativecommons.org/publicdomain/zero/1.0/); "
        "see [Wikidata licensing](https://www.wikidata.org/wiki/Wikidata:Licensing). "
        "Each claim retains its entity ID, revision, original modified time, acquisition time and field pointer. "
        "No Wikimedia image or article text is included. Data licensing is separate from the application code license.\n\n"
        "Counts describe named source POIs by accommodation kind, not verified current businesses or complete inventory. "
        "Only hotel-kind entries count as hotels; apartments, guest houses, hostels, chalets and motels remain typed. "
        "The whole-island Cyprus extract spans different administered areas; entry, licensing and operation are unverified. "
        "Way/relation coordinates are approximate bounding-box midpoints. City search circles overlap. "
        "Exact-ID links retain conflicting source claims separately and never overwrite preferred OSM fields. "
        "No prices, availability or booking conditions are established.\n\n"
        "The unchanged 009 release is preserved in archive/cyprus-hotels-20261025-v1/.\n",
        encoding="utf-8", newline="\n")
    if any((output / name).stat().st_size > 10 * 1024 * 1024 for name in ("source.json", "hotels.parquet", "manifest.json")):
        raise ValueError("Built artifact exceeds the reviewed 10 MiB bound")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=ASSETS / "source.json")
    parser.add_argument("--output", type=Path, default=ASSETS)
    parser.add_argument("--pbf", type=Path, help="Explicitly import a previously downloaded pinned PBF; never fetches it")
    parser.add_argument("--retrieved-at", help="Actual PBF download timestamp with timezone; required with --pbf")
    parser.add_argument("--wikidata-directory", type=Path, help="Previously acquired entity JSON plus wikidata-acquisition.json, no HTTP")
    arguments = parser.parse_args()
    if arguments.pbf:
        if not arguments.retrieved_at:
            parser.error("--pbf requires the actual --retrieved-at acquisition timestamp")
        source = import_pbf(arguments.pbf, iso_utc(arguments.retrieved_at), arguments.wikidata_directory)
    else:
        source = json.loads(arguments.source.read_text(encoding="utf-8"))
    manifest = build(source, arguments.output)
    print(json.dumps({"release_id": manifest["release_id"], "row_count": manifest["row_count"],
                      "coverage": manifest["coverage"], "parquet_sha256": manifest["parquet_sha256"]},
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
