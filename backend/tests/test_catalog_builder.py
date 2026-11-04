"""Validate reproducible release packaging and reject edited source assertions."""
from copy import deepcopy
import json

import pytest

from scripts.build_cyprus_catalog import ASSETS, build, import_pbf, validate_source


@pytest.fixture
def source_release():
    return json.loads((ASSETS / "source.json").read_text(encoding="utf-8"))


def test_offline_rebuild_matches_shipped_release_and_preserves_times(tmp_path, source_release):
    first, second = tmp_path / "first", tmp_path / "second"
    manifest = build(source_release, first)
    build(source_release, second)
    for name in ("source.json", "hotels.parquet", "manifest.json", "LICENSE.md"):
        assert (first / name).read_bytes() == (second / name).read_bytes() == (ASSETS / name).read_bytes()
    assert manifest["row_count"] == len(source_release["hotels"])
    assert manifest["retrieved_at"] == source_release["retrieved_at"]
    assert manifest["source_dataset_at"] == source_release["source_dataset_at"]


@pytest.mark.parametrize("change", ["wrong_destination", "invented_price", "new_object_time", "duplicate_identity"])
def test_builder_rejects_invalid_source_assertions(source_release, change):
    source = deepcopy(source_release)
    row = source["hotels"][0]
    if change == "wrong_destination":
        row["longitude"] = 28.0
    elif change == "invented_price":
        row["tags_json"] = json.dumps({"price_per_night": "100"})
    elif change == "new_object_time":
        row["source_updated_at"] = source["source_dataset_at"].replace("Z", ".000001Z")
    else:
        source["hotels"][1]["hotel_id"] = row["hotel_id"]
    with pytest.raises(ValueError):
        validate_source(source)


def test_import_rejects_unreviewed_raw_file_before_optional_parser(tmp_path):
    raw = tmp_path / "wrong.osm.pbf"
    raw.write_bytes(b"Not the identified Geofabrik source")
    with pytest.raises(ValueError, match="reviewed dated source checksum"):
        import_pbf(raw, "2026-10-25T13:17:32.480818Z")


def test_archived_v1_offline_rebuild_is_preserved(tmp_path):
    archive = ASSETS / "archive" / "cyprus-hotels-20261025-v1"
    source = json.loads((archive / "source.json").read_text(encoding="utf-8"))
    build(source, tmp_path)
    for name in ("source.json", "hotels.parquet", "manifest.json", "LICENSE.md"):
        assert (tmp_path / name).read_bytes() == (archive / name).read_bytes()


@pytest.mark.parametrize("mutation", ["fuzzy_link", "source_license", "promoted_website", "invented_amenity", "source_time"])
def test_builder_rejects_untraced_or_rewritten_independent_claims(source_release, mutation):
    source = deepcopy(source_release)
    row = next(row for row in source["hotels"] if len(json.loads(row["source_records_json"])) > 1)
    records = json.loads(row["source_records_json"])
    independent = records[1]
    if mutation == "fuzzy_link":
        independent["record_id"] = "wikidata:Q1"
        independent["url"] = "https://www.wikidata.org/wiki/Q1"
    elif mutation == "source_license":
        independent["license"] = "unlicensed"
    elif mutation == "promoted_website":
        row["website"] = "https://example.org/other-property"
    elif mutation == "invented_amenity":
        row["tags_json"] = json.dumps({**json.loads(row["tags_json"]), "pets": "yes"})
    else:
        independent["source_updated_at"] = "2099-01-01T00:00:00Z"
    row["source_records_json"] = json.dumps(records)
    with pytest.raises(ValueError):
        validate_source(source)
