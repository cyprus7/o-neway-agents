"""Tariff semantics and bounded fallback; fixtures do not establish live availability."""
import asyncio
from datetime import date

import httpx

from backend.app.pricing.lodging import PublicHotelPrices, parse_apollo, parse_saruhan, tariff_fact, visible_text
from backend.app.storage import Store


APOLLO = """Prices EURO / night SEASON ROOM TYPE Single room Double Room Triple room
1st Season 01/11 - 31/03 35 45 55
2nd Season 01/04 - 31/05 50 65 75
3rd Season 01/06 - 31/10 60 70 90 * Prices include breakfast."""
SARUHAN = """HIGH SEASON RATES (between end of March - beginning of November)
STANDARD DOUBLE/TWIN ROOM : 80-105 EURO
LOW SEASON RATES (beginning of November - end of March, except new year)
STANDARD DOUBLE/TWIN ROOM : 50-70 EURO
The prices are in EURO per ROOM, not per person, including Turkish breakfast, tax and service charges."""
OBSERVED = "2026-10-24T17:08:38+00:00"


def test_parse_tariffs_rejects_wrong_units_order_and_partial_table():
    assert parse_apollo(APOLLO)["seasons"][0]["single"] == "35"
    assert parse_apollo(APOLLO.replace("EURO / night", "USD per stay")) is None
    assert parse_apollo(APOLLO.replace("Single room Double Room", "Double room Single room")) is None
    assert parse_apollo(APOLLO.replace("01/04 - 31/05 50 65 75", "")) is None
    assert parse_saruhan(SARUHAN)["high"] == {"amount_from": "80", "amount_to": "105"}
    assert parse_saruhan(SARUHAN.replace("80-105", "105-80")) is None
    assert parse_saruhan(SARUHAN.replace("not per person", "per person")) is None


def test_saruhan_tax_phrase_requires_positive_inclusion_and_rejects_negation():
    assert parse_saruhan(SARUHAN)["taxes"] == "included"
    assert parse_saruhan(SARUHAN.replace("including Turkish breakfast, tax and service charges", "tax and service charges are not included"))["taxes"] == "unknown"
    assert parse_saruhan(SARUHAN + " Taxes are not included and charged separately.")["taxes"] == "unknown"


def test_published_tariff_arithmetic_crosses_season_per_night_but_never_becomes_quote():
    parsed = parse_apollo(APOLLO)
    fact = tariff_fact("ATH", parsed, OBSERVED, "2026-10-30", "2026-11-02", 1, [], 1)
    assert fact["reference_total_from"] == "155"  # Oct30+31 = 60 each, Nov1 = 35.
    assert fact["nights"] == 3
    assert fact["amount_from"] == "35" and fact["amount_to"] == "60"
    assert fact["status"] == "reference"
    assert fact["tariff_year"] is None and fact["requested_dates_matched"] is False
    assert fact["availability_verified"] is False
    assert fact["taxes"] == "unknown" and fact["breakfast_included"] is True
    assert "normalized_group" not in fact


def test_double_or_twin_reference_preserves_range_and_inclusions():
    fact = tariff_fact("IST", parse_saruhan(SARUHAN), OBSERVED, date(2026, 10, 25), date(2026, 11, 1), 2, [], 1)
    assert fact["reference_total_from"] == "560" and fact["reference_total_to"] == "735"
    assert fact["taxes"] == "included" and fact["breakfast_included"] is True
    assert fact["scope"]["matched"] is False
    assert fact["projection_kind"] == "published_tariff_projection_not_quote"


def test_ambiguous_season_boundaries_and_new_year_do_not_invent_cheap_totals():
    for start, end in (("2026-10-31", "2026-11-02"), ("2026-12-30", "2027-01-02"), ("2027-03-20", "2027-03-22")):
        fact = tariff_fact("IST", parse_saruhan(SARUHAN), OBSERVED, start, end, 2, [], 1)
        assert "reference_total_from" not in fact
        assert "season_boundary_or_new_year_unknown" in fact["limitations"]


def test_children_rooms_and_unsupported_party_are_not_multiplied_from_single_rate():
    for adults, ages, rooms in ((2, [7], 1), (2, [], 2), (4, [], 1)):
        fact = tariff_fact("ATH", parse_apollo(APOLLO), OBSERVED, "2026-10-25", "2026-11-01", adults, ages, rooms)
        assert "reference_total_from" not in fact
        assert "party_not_covered_by_published_tariff" in fact["limitations"]
    for start, end in (("bad", "2026-11-01"), ("2026-10-25", "2026-10-25"), ("2026-10-25", "2027-02-01")):
        fact = tariff_fact("ATH", parse_apollo(APOLLO), OBSERVED, start, end, 1, [], 1)
        assert "reference_total_from" not in fact


def test_source_script_text_cannot_supply_a_tariff():
    assert parse_apollo(visible_text("<script>" + APOLLO + "</script><p>No tariff here</p>")) is None


def test_async_fetch_cache_preserves_observation_time_and_uses_no_credentials(tmp_path):
    calls = []
    def handler(request):
        calls.append(request)
        assert request.url == "https://www.apollonhotel-athens.com/rates.php"
        assert "authorization" not in request.headers
        return httpx.Response(200, text="<html><body>" + APOLLO + "</body></html>")
    async def run():
        store = Store(str(tmp_path / "tariffs.sqlite"))
        try:
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                provider = PublicHotelPrices(client, store)
                first = await provider.search("ATH", "2026-10-25", "2026-11-01", 1, [], 1, "EUR")
                second = await provider.search("ATH", "2026-11-25", "2026-12-02", 2, [], 1, "EUR")
                assert first["delivery"] == "live" and second["delivery"] == "cached"
                assert first["observed_at"] == second["observed_at"]
                assert first["reference_total_from"] == "420"
                assert second["reference_total_from"] == "315"
                assert len(calls) == 1
        finally:
            store.close()
    asyncio.run(run())


def test_http_challenge_redirect_and_oversized_response_fail_without_retry():
    async def run():
        for status, headers, body, reason in (
            (403, {}, "captcha", "http_403"),
            (302, {"location": "http://127.0.0.1/admin"}, "", "http_302"),
            (200, {}, "x" * 300001, "response_too_large"),
            (200, {}, "no tariff available", "tariff_not_found"),
        ):
            calls = []
            def handler(request):
                calls.append(request)
                return httpx.Response(status, headers=headers, text=body)
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                fact = await PublicHotelPrices(client, None).search("ATH", "2026-10-25", "2026-11-01")
                assert fact["status"] == "unknown" and fact["reason"] == reason
                assert len(calls) == 1
                assert "amount_from" not in fact
    asyncio.run(run())


def test_unsupported_destination_currency_never_dispatch():
    async def run():
        def handler(request):
            raise AssertionError("unsupported input must not fetch a random destination")
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = PublicHotelPrices(client, None)
            assert (await provider.search("TBS", "2026-10-25", "2026-11-01"))["reason"] == "unsupported_destination"
            assert (await provider.search("ATH", "2026-10-25", "2026-11-01", currency="USD"))["reason"] == "unsupported_currency"
    asyncio.run(run())
