"""Catalog release, geography and evidence boundaries; all fixture facts are synthetic."""
import hashlib
import json
import shutil
from pathlib import Path

import duckdb
import pytest

from backend.app.catalog import ASSETS, LocalHotelCatalog, LOCALITIES, _distance
from backend.app.geography import Place, find_place
from backend.app.models import TravelIntent


def make_release(directory: Path, *, mutate=None):
    directory.mkdir(exist_ok=True)
    records = []
    for number, name, city, lat, lon, tags in (
        (1, "Synthetic Harbor Hotel", "larnaca", 34.92, 33.63, {"swimming_pool": "yes"}),
        (2, "Synthetic Adults Only", "larnaca", 34.921, 33.631, {"adults_only": "yes"}),
        (3, "Synthetic No Pets", "larnaca", 34.922, 33.632, {"pets": "no"}),
        (4, "Synthetic Paphos Hotel", "paphos", 34.77, 32.42, {}),
    ):
        records.append({"hotel_id": f"osm:node:{number}", "name": name, "name_en": None, "name_ru": None,
                        "destination_id": city, "country_code": "CY", "latitude": lat, "longitude": lon,
                        "tags_json": json.dumps(tags), "website": None,
                        "source_url": f"https://www.openstreetmap.org/node/{number}",
                        "source_updated_at": "2020-01-01T00:00:00+00:00", "retrieved_at": "2020-01-03T00:00:00+00:00"})
    if mutate:
        mutate(records)
    source = json.dumps({"synthetic_fixture": True, "hotels": records}, sort_keys=True).encode()
    (directory / "source.json").write_bytes(source)
    with duckdb.connect() as connection:
        # Parameterized JSON reader avoids SQL interpolation of synthetic test content.
        connection.execute("CREATE TABLE records AS SELECT * FROM read_json_auto(?)", [str(directory / "source.json")])
        connection.execute("COPY (SELECT unnest(hotels, recursive := true) FROM records) TO ? (FORMAT PARQUET)", [str(directory / "hotels.parquet")])
    manifest = {"schema_version": "cyprus-hotels-v1", "release_id": "synthetic-test-v1", "row_count": len(records),
                "retrieved_at": "2020-01-03T00:00:00+00:00", "source_dataset_at": "2020-01-02T00:00:00+00:00",
                "license": "ODbL-1.0", "source_sha256": hashlib.sha256(source).hexdigest(),
                "parquet_sha256": hashlib.sha256((directory / "hotels.parquet").read_bytes()).hexdigest(),
                "coverage": {"larnaca": 3, "paphos": 1}}
    (directory / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return directory


def test_geography_precedes_preferences_and_origin(tmp_path):
    catalog = LocalHotelCatalog(make_release(tmp_path), enabled=True)
    assert catalog.status == "ready"
    intent = TravelIntent(origin="Ларнака", destination="Пафос", hotel_requirements=["Harbor Hotel"])
    result = catalog.search(find_place("Пафос"), intent)
    assert result.status == "matched" and all(row["destination_id"] == "paphos" for row in result.hotels)
    assert catalog.search(find_place("Афины"), intent).status == "out_of_scope"
    # A country label cannot silently stand in for a supported city.
    unknown = Place("Кипр", "Кипр", "CY", 34.92, 33.63, None, (), (), "https://example.org", "")
    assert catalog.search(unknown, intent).status == "out_of_scope"
    wrong_country = find_place("Ларнака")
    from dataclasses import replace
    assert catalog.search(replace(wrong_country, country_code="TR"), intent).status == "out_of_scope"


def test_explicit_exclusions_do_not_verify_unknown_conditions(tmp_path):
    catalog = LocalHotelCatalog(make_release(tmp_path), enabled=True)
    result = catalog.search(find_place("Ларнака"), TravelIntent(children_ages=[4], pets=["собака"]))
    assert [row["name"] for row in result.hotels] == ["Synthetic Harbor Hotel"]
    row = result.hotels[0]
    assert "pets" not in row["tags"] and "price" not in row
    assert row["retrieved_at"] == "2020-01-03T00:00:00+00:00"
    # Result projection cannot mutate stored facts for subsequent queries.
    row["tags"]["pets"] = "yes"
    second = catalog.search(find_place("Ларнака"), TravelIntent(children_ages=[4], pets=["собака"]))
    assert "pets" not in second.hotels[0]["tags"]


def test_adult_age_marker_in_name_excludes_family_without_losing_plus(tmp_path):
    catalog = LocalHotelCatalog(make_release(tmp_path, mutate=lambda rows: rows[0].update(name="Synthetic Hotel 18+")), enabled=True)
    result = catalog.search(find_place("Ларнака"), TravelIntent(children_ages=[6]))
    assert result.status == "matched"
    assert all(row["hotel_id"] != "osm:node:1" for row in result.hotels)


@pytest.mark.parametrize("mutation", [
    lambda rows: rows[0].update(latitude=float("nan")),
    lambda rows: rows[0].update(latitude=35.5),
    lambda rows: rows[0].update(source_url="https://example.org/foreign-hotel"),
    lambda rows: rows[0].update(hotel_id="osm:node:2"),
    lambda rows: rows[0].update(tags_json='{"invented_price":"100"}'),
    lambda rows: rows[0].update(source_updated_at="2021-01-01T00:00:00Z"),
    lambda rows: rows[0].update(retrieved_at="2020-01-03T00:00:00"),
])
def test_invalid_rows_fail_closed_without_claiming_coverage(tmp_path, mutation):
    catalog = LocalHotelCatalog(make_release(tmp_path, mutate=mutation), enabled=True)
    assert catalog.status == "unavailable"
    assert catalog.capabilities()["row_count"] == 0
    assert catalog.search(find_place("Ларнака"), TravelIntent()).hotels == ()


def test_checksum_missing_and_disabled_are_safe(tmp_path):
    assets = make_release(tmp_path)
    with (assets / "hotels.parquet").open("ab") as file:
        file.write(b"corrupt")
    assert LocalHotelCatalog(assets, enabled=True).status == "unavailable"
    assert LocalHotelCatalog(tmp_path / "missing", enabled=True).status == "unavailable"
    assert LocalHotelCatalog(tmp_path / "missing", enabled=False).status == "disabled"


def test_shipped_artifact_is_real_attributed_bounded_and_searchable():
    catalog = LocalHotelCatalog(enabled=True)
    metadata = catalog.capabilities()
    assert metadata["configured"], metadata
    assert 400 <= metadata["row_count"] <= 5000
    assert metadata["license"] == "ODbL-1.0"
    assert (ASSETS / "LICENSE.md").is_file()
    assert metadata["source_record_counts"]["wikidata"] >= 2
    assert metadata["source_record_counts"]["osm"] == metadata["row_count"]
    assert sum(metadata["kind_counts"].values()) == metadata["row_count"]
    assert metadata["kind_counts"]["hotel"] < metadata["row_count"]
    for city in ("Ларнака", "Пафос", "Лимасол", "Никосия", "Айя-Напа", "Протарас", "Полис", "Троодос"):
        intent = TravelIntent(destination=city)
        first = catalog.search(find_place(city), intent)
        assert first.status == "matched" and first.retrieved_at == metadata["retrieved_at"]
        assert first.hotels == catalog.search(find_place(city), intent).hotels
        assert all("synthetic" not in row["name"].casefold() for row in first.hotels)
        assert all(row["source_url"].startswith("https://www.openstreetmap.org/") for row in first.hotels)
        place = find_place(city)
        assert all(_distance(place.latitude, place.longitude, row["latitude"], row["longitude"]) <= 8 for row in first.hotels)
        assert all(row["kind"] in metadata["kind_counts"] and row["source_records"][0]["source_id"] == "osm" for row in first.hotels)


def test_overlapping_city_circles_do_not_drop_neighboring_source_records():
    catalog = LocalHotelCatalog(enabled=True)
    # A Protaras-primary POI can legitimately lie in the reviewed Ayia Napa circle.
    candidate = next(row for row in catalog._rows if row["destination_id"] == "protaras"
                     and _distance(*LOCALITIES["ayia_napa"][:2], row["latitude"], row["longitude"]) <= 8)
    result = catalog.search(find_place("Айя-Напа"), TravelIntent(hotel_requirements=[candidate["name"]]))
    assert any(row["hotel_id"] == candidate["hotel_id"] for row in result.hotels)


def test_independent_conflicting_names_never_overwrite_preferred_facts():
    catalog = LocalHotelCatalog(enabled=True)
    result = catalog.search(find_place("Ларнака"), TravelIntent(hotel_requirements=["Ibis Styles Larnaca"]))
    row = next(row for row in result.hotels if row["hotel_id"] == "osm:node:5188827315")
    assert row["name"] == "Ibis Styles Larnaca"
    independent = next(source for source in row["source_records"] if source["source_id"] == "wikidata")
    assert independent["license"] == "CC0-1.0" and independent["link_status"] == "name_conflict"
    claim = next(field for field in independent["fields"] if field["field"] == "label.en")
    assert claim["value"] == "Amorgos Boutique Hotel"
    assert claim["pointer"] == "/labels/en/value" and independent["revision"] == 1611253991
    claim["value"] = "Mutated outside the catalog"
    again = catalog.search(find_place("Ларнака"), TravelIntent(hotel_requirements=["Ibis Styles Larnaca"]))
    assert "Mutated outside the catalog" not in json.dumps(again.hotels)


def test_conflicting_coordinates_and_unrelated_kind_remain_separate():
    catalog = LocalHotelCatalog(enabled=True)
    gardens = next(row for row in catalog._rows if row["hotel_id"] == "osm:way:960093202")
    source = next(source for source in gardens["source_records"] if source["source_id"] == "wikidata")
    assert source["link_status"] == "coordinate_conflict"
    claim = next(field["value"] for field in source["fields"] if field["field"] == "coordinates")
    assert _distance(gardens["latitude"], gardens["longitude"], claim["latitude"], claim["longitude"]) > .25
    # Nearby separately mapped villas with the same name are not fuzzy-merged.
    villas = [row for row in catalog._rows if row["name"] == "Marinea Beach Villas"]
    assert sum(row["kind"] == "chalet" for row in villas) == 7
    assert any(row["kind"] == "guest_house" for row in villas)


def test_original_009_release_is_unchanged_and_still_loads():
    archive = ASSETS / "archive" / "cyprus-hotels-20261025-v1"
    hashes = {"hotels.parquet": "f8e7c73636890ffc9a926e1549609000e400faa50be30d88469f771a2f0abc9b",
              "source.json": "8ca083ee46d421fe5adfc113e89412cc6fa9e63c194e7a021cc0ed111951170e",
              "manifest.json": "9aa6379328253791212ef0e25b3cfdf0e198545c5c7e1b9335a1f9f11c9a9269",
              "LICENSE.md": "5f57f66aa860cd6a5f2659a1e72579930830bc7bc382b106981bdccca5ae4428"}
    for name, expected in hashes.items():
        assert hashlib.sha256((archive / name).read_bytes()).hexdigest() == expected
    original = LocalHotelCatalog(archive, enabled=True)
    assert original.status == "ready" and original.capabilities()["row_count"] == 140
    assert original.capabilities()["source_record_counts"] == {"osm": 140}
    assert original.search(find_place("Пафос"), TravelIntent()).status == "matched"


@pytest.mark.parametrize("alias,city", [("Λάρνακα", "Ларнака"), ("Pafos", "Пафос"), ("Lemesos", "Лимасол"), ("Λευκωσία", "Никосия"),
                                       ("Agia Napa", "Айя-Напа"), ("Протарас", "Протарас"),
                                       ("Polis Chrysochous", "Полис"), ("Τρόοδος", "Троодос")])
def test_new_cyprus_destination_aliases(alias, city):
    assert find_place(alias).name == city


def test_malformed_independent_source_manifest_fails_closed(tmp_path):
    directory = tmp_path / "catalog"
    shutil.copytree(ASSETS, directory)
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    manifest["source_origins"] = {"osm": [], "wikidata": []}
    (directory / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    catalog = LocalHotelCatalog(directory, enabled=True)
    assert catalog.status == "unavailable" and catalog.capabilities()["row_count"] == 0
