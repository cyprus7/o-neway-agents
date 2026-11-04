"""Synthetic airline fixtures prove scope/limits, not actual fare availability."""
import asyncio
from datetime import datetime, timedelta, timezone
import json

import httpx
import pytest

from backend.app.pricing import airlines
from backend.app.pricing.airlines import AEGEAN, RYANAIR, PublicAirlinePrices
from backend.app.storage import Store


NOW = datetime(2026, 10, 24, 17, 30, tzinfo=timezone.utc)
OUT, BACK = "2026-10-25", "2026-11-01"


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(airlines, "_now", lambda: NOW)
    instance = Store(str(tmp_path / "airline.db"))
    yield instance
    instance.close()


def aegean_row(reverse=False):
    return {"datacoreId": "synthetic-inbound" if reverse else "synthetic-outbound",
        "priceSpecification": {"totalPrice": "148.79" if reverse else "70.38", "currencyCode": "EUR"},
        "outboundFlight": {"departureAirportIataCode": "ATH" if reverse else "LCA",
            "arrivalAirportIataCode": "LCA" if reverse else "ATH", "fareClassInput": "NOBAG"},
        "departureDate": BACK if reverse else OUT, "journeyType": "ONE_WAY",
        "searchDate": (NOW - timedelta(hours=1)).isoformat()}


def page(rows):
    data = {"props": {"pageProps": {"apolloState": {"data": {
        'CmpConfig:{"cmp":"fare-daily-histogram"}': {"cmpConfig": {"settings": {
            "initialState": {"dailyHistogram": rows}}}}
    }}}}}
    return '<html><script id="__NEXT_DATA__" type="application/json">' + json.dumps(data) + '</script></html>'


def ryanair_row():
    def leg(reverse=False):
        return {"departureAirport": {"iataCode": "ATH" if reverse else "LCA"},
            "arrivalAirport": {"iataCode": "LCA" if reverse else "ATH"},
            "departureDate": (BACK if reverse else OUT) + "T08:05:00",
            "flightNumber": "SYNTHETIC02" if reverse else "SYNTHETIC01",
            "priceUpdated": int((NOW - timedelta(hours=1)).timestamp() * 1000),
            "price": {"value": "46.99" if reverse else "23.39", "currencyCode": "EUR"}}
    return {"outbound": leg(), "inbound": leg(True),
            "summary": {"price": {"value": "70.38", "currencyCode": "EUR"}, "tripDurationDays": 7}}


def handler(rows=None, fares=None, calls=None):
    def respond(request):
        if calls is not None:
            calls.append(request)
        if request.url.host == "www.ryanair.com":
            return httpx.Response(200, json={"fares": [] if fares is None else fares})
        reverse = "from-athens-to-larnaca" in request.url.path
        selected = rows(reverse) if callable(rows) else rows
        return httpx.Response(200, text=page([aegean_row(reverse)] if rows is None else selected))
    return respond


async def search(store, respond):
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        return await PublicAirlinePrices(client, store).search("LCA", "ATH", OUT, BACK, "EUR")


@pytest.mark.asyncio
async def test_exact_two_one_way_fares_have_decimal_total_and_honest_scope(store):
    calls = []
    result = await search(store, handler(calls=calls))
    assert len(calls) == 2
    assert len(result["facts"]) == 1
    fact = result["facts"][0]
    assert fact["amount"] == "219.17"
    assert fact["provider_id"] == AEGEAN and fact["trip_type"] == "roundtrip"
    assert fact["unit"] == "person_trip" and len(fact["legs"]) == 2
    assert not fact["availability_verified"] and not fact["party_verified"]
    assert fact["scope"]["date_from"] == OUT and fact["scope"]["date_to"] == BACK
    assert "не единый билет" in fact["message"] and fact["taxes"] == "unknown"
    assert len(set(fact["source_observation_ids"])) == 2
    assert fact["observed_at"] == (NOW - timedelta(hours=1)).isoformat()
    assert fact["expires_at"] == (NOW + timedelta(minutes=10)).isoformat()


@pytest.mark.asyncio
@pytest.mark.parametrize("reverse_present", [False, True])
async def test_one_leg_never_turns_into_return_trip(store, reverse_present):
    result = await search(store, handler(rows=lambda reverse: [aegean_row(reverse)] if reverse == reverse_present else []))
    fact, = result["facts"]
    assert fact["trip_type"] == "one_way" and fact["scope"]["date_to"] is None
    assert len(fact["legs"]) == 1
    assert fact["scope"]["origin_iata"] == ("ATH" if reverse_present else "LCA")
    assert fact["amount"] == ("148.79" if reverse_present else "70.38")
    assert "обратный перелёт не включён" in fact["message"]


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [
    ("departureDate", "2026-10-26"), ("journeyType", "ROUND_TRIP"),
    ("searchDate", "not a date"), ("searchDate", "2026-10-01T00:00:00Z"),
    ("searchDate", "2026-10-26T00:00:00Z"), ("emptyFare", True),
])
async def test_aegean_mismatched_stale_or_empty_records_do_not_supply_prices(store, field, value):
    def rows(reverse):
        row = aegean_row(reverse)
        row[field] = value
        return [row]
    assert (await search(store, handler(rows=rows)))["facts"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("origin,destination", [("WAW", "ATH"), ("PFO", "ATH"), ("LCA", "IST")])
async def test_wrong_airport_is_never_silently_substituted(store, origin, destination):
    def rows(reverse):
        row = aegean_row(reverse)
        row["outboundFlight"].update(departureAirportIataCode=origin, arrivalAirportIataCode=destination)
        return [row]
    assert (await search(store, handler(rows=rows)))["facts"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [0, "0.00", -1, "NaN", "Infinity", True, {}, 1000001])
async def test_zero_invalid_or_unbounded_price_is_not_a_fare(store, value):
    def rows(reverse):
        row = aegean_row(reverse)
        row["priceSpecification"]["totalPrice"] = value
        return [row]
    assert (await search(store, handler(rows=rows)))["facts"] == []


@pytest.mark.asyncio
async def test_currency_is_read_from_response_and_never_converted(store):
    def rows(reverse):
        row = aegean_row(reverse)
        row["priceSpecification"]["currencyCode"] = "PLN"
        return [row]
    result = await search(store, handler(rows=rows))
    assert result["facts"] == []
    assert sum(row["outcome"] == "currency_mismatch" for row in result["diagnostics"]) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("rows", [{}, "fare", 2, None])
async def test_malformed_array_does_not_crash_or_produce_fares(store, rows):
    # Explicit lambda distinguishes a malformed null from the helper's default fixture.
    result = await search(store, handler(rows=lambda _: rows))
    assert not result["facts"]
    assert any(row["outcome"] == "parse_error" for row in result["diagnostics"])


@pytest.mark.asyncio
async def test_ryanair_fallback_checks_both_legs_and_total(store):
    calls = []
    result = await search(store, handler(rows=[], fares=[ryanair_row()], calls=calls))
    fact, = result["facts"]
    assert fact["provider_id"] == RYANAIR and fact["amount"] == "70.38"
    assert fact["scope"]["origin_iata"] == "LCA" and len(fact["legs"]) == 2
    request = calls[-1]
    assert request.url.params["departureAirportIataCode"] == "LCA"
    assert request.url.params["outboundDepartureDateFrom"] == OUT
    assert request.url.params["inboundDepartureDateFrom"] == BACK
    assert "authorization" not in request.headers and "cookie" not in request.headers


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["route", "return_date", "missing_return", "currency", "sum", "old", "malformed_leg"])
async def test_ryanair_rejects_mismatched_unscoped_or_stale_fares(store, mutation):
    fare = ryanair_row()
    if mutation == "route":
        fare["outbound"]["departureAirport"]["iataCode"] = "PFO"
    elif mutation == "return_date":
        fare["inbound"]["departureDate"] = "2026-11-02T08:05:00"
    elif mutation == "missing_return":
        fare.pop("inbound")
    elif mutation == "currency":
        fare["summary"]["price"]["currencyCode"] = "PLN"
    elif mutation == "sum":
        fare["summary"]["price"]["value"] = "10.00"
    elif mutation == "old":
        fare["inbound"]["priceUpdated"] = 0
    elif mutation == "malformed_leg":
        fare["inbound"]["price"] = []
    result = await search(store, handler(rows=[], fares=[fare]))
    assert result["facts"] == []
    assert result["diagnostics"][-1]["diagnostic_code"] == {
        "route": "route_mismatch", "return_date": "no_matching_date_fare", "missing_return": "source_parse_error",
        "currency": "currency_mismatch", "sum": "fare_total_mismatch", "old": "stale_observation",
        "malformed_leg": "source_parse_error",
    }[mutation]


@pytest.mark.asyncio
async def test_corrupt_ryanair_array_is_a_diagnostic_not_a_price(store):
    result = await search(store, handler(rows=[], fares={"unexpected": "shape"}))
    assert not result["facts"]
    assert result["diagnostics"][-1]["outcome"] == "parse_error"


@pytest.mark.asyncio
async def test_cache_reuse_preserves_provider_time_fetch_time_and_expiry(store):
    calls = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler(calls=calls))) as client:
        adapter = PublicAirlinePrices(client, store)
        initial = await adapter.search("LCA", "ATH", OUT, BACK, "EUR")
        again = await adapter.search("LCA", "ATH", OUT, BACK, "EUR")
    assert len(calls) == 2
    fact = again["facts"][0]
    assert fact["delivery"] == "cache_reuse"
    for field in ["observed_at", "retrieved_at", "expires_at", "source_observation_ids"]:
        assert fact[field] == initial["facts"][0][field]


@pytest.mark.asyncio
async def test_expired_cache_is_not_returned_on_network_failure(store):
    calls = []
    await search(store, handler(calls=calls))
    with store.lock, store.db:
        store.db.execute("UPDATE cache SET expires_at=0")
    def unavailable(request):
        calls.append(request)
        raise httpx.ConnectError("synthetic offline", request=request)
    result = await search(store, unavailable)
    assert not result["facts"] and len(calls) == 5
    assert all(row["outcome"] == "unavailable" for row in result["diagnostics"])


@pytest.mark.asyncio
async def test_expired_observation_is_rejected_even_if_sql_cache_remains_fresh(store, monkeypatch):
    calls = []
    await search(store, handler(calls=calls))
    monkeypatch.setattr(airlines, "_now", lambda: NOW + timedelta(minutes=11))
    def unavailable(request):
        calls.append(request)
        raise httpx.ConnectError("synthetic offline", request=request)
    result = await search(store, unavailable)
    assert not result["facts"] and len(calls) == 5


@pytest.mark.asyncio
async def test_cache_expiry_never_outlives_provider_record_retention(store):
    observed = NOW - timedelta(hours=47, minutes=59)
    def rows(reverse):
        row = aegean_row(reverse)
        row["searchDate"] = observed.isoformat()
        return [row]
    fact, = (await search(store, handler(rows=rows)))["facts"]
    assert fact["expires_at"] == (NOW + timedelta(minutes=1)).isoformat()


@pytest.mark.asyncio
@pytest.mark.parametrize("status,outcome", [(403, "blocked"), (405, "blocked"), (429, "rate_limited"), (302, "context_mismatch")])
async def test_protection_or_redirect_is_not_followed(store, status, outcome):
    calls = []
    def restricted(request):
        calls.append(request)
        return httpx.Response(status, headers={"Location": "http://127.0.0.1/"}, text="Human verification")
    result = await search(store, restricted)
    assert not result["facts"] and len(calls) == 3
    assert all(row["outcome"] == outcome for row in result["diagnostics"])
    assert all(request.url.scheme == "https" for request in calls)


@pytest.mark.asyncio
async def test_request_rate_budget_is_shared_by_adapter_instances(store):
    calls = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler(rows=[], calls=calls))) as client:
        first, second = PublicAirlinePrices(client, store), PublicAirlinePrices(client, store)
        await first.search("LCA", "ATH", OUT, BACK, "EUR")
        await second.search("LCA", "ATH", "2026-10-26", BACK, "EUR")
        result = await first.search("LCA", "ATH", "2026-10-27", BACK, "EUR")
    assert len(calls) == 6
    assert all(row["outcome"] == "rate_limited" for row in result["diagnostics"])


@pytest.mark.asyncio
async def test_at_most_two_network_requests_run_concurrently(store):
    active = maximum = 0
    async def slow(request):
        nonlocal active, maximum
        active += 1
        maximum = max(active, maximum)
        await asyncio.sleep(0.005)
        active -= 1
        return httpx.Response(200, json={"fares": []}) if request.url.host == "www.ryanair.com" else httpx.Response(200, text=page([]))
    async with httpx.AsyncClient(transport=httpx.MockTransport(slow)) as client:
        adapter = PublicAirlinePrices(client, store)
        await asyncio.gather(*(adapter.search("LCA", "ATH", f"2026-10-{day}", BACK, "EUR") for day in [25, 26, 27]))
    assert maximum == 2 and active == 0


@pytest.mark.asyncio
async def test_total_deadline_cancels_pending_fetches(store, monkeypatch):
    monkeypatch.setattr(airlines, "SEARCH_TIMEOUT", 0.01)
    async def slow(request):
        await asyncio.sleep(1)
        return httpx.Response(200, text=page([]))
    result = await search(store, slow)
    assert not result["facts"] and result["diagnostics"][-1]["outcome"] == "timeout"


@pytest.mark.asyncio
async def test_partial_observed_leg_survives_fallback_deadline(store, monkeypatch):
    monkeypatch.setattr(airlines, "SEARCH_TIMEOUT", 0.2)
    async def respond(request):
        if request.url.host == "www.ryanair.com":
            await asyncio.sleep(1)
            return httpx.Response(200, json={"fares": []})
        reverse = "from-athens-to-larnaca" in request.url.path
        return httpx.Response(200, text=page([] if reverse else [aegean_row()]))
    result = await search(store, respond)
    assert result["facts"][0]["trip_type"] == "one_way"
    assert result["facts"][0]["amount"] == "70.38"
    assert result["diagnostics"][-1]["outcome"] == "timeout"


@pytest.mark.asyncio
async def test_response_size_is_bounded(store, monkeypatch):
    monkeypatch.setattr(airlines, "MAX_BYTES", 128)
    result = await search(store, lambda request: httpx.Response(200, content=b"X" * 129))
    assert not result["facts"]
    assert all(row["outcome"] == "parse_error" for row in result["diagnostics"])


@pytest.mark.asyncio
@pytest.mark.parametrize("origin,destination,departure,returning,currency", [
    ("lca", "ATH", OUT, BACK, "EUR"), ("LCA", "ATH", "bad", BACK, "EUR"),
    ("LCA", "ATH", BACK, OUT, "EUR"), ("LCA", "ATH", OUT, BACK, "euro"),
    ("LCA", "LCA", OUT, BACK, "EUR"), ("https://private/", "ATH", OUT, BACK, "EUR"),
])
async def test_invalid_input_never_dispatches(store, origin, destination, departure, returning, currency):
    calls = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler(calls=calls))) as client:
        result = await PublicAirlinePrices(client, store).search(origin, destination, departure, returning, currency)
    assert not calls and not result["facts"]
    assert result["diagnostics"][0]["outcome"] == "context_mismatch"


@pytest.mark.asyncio
@pytest.mark.parametrize("positive", [False, True])
async def test_delivery_metadata_covers_positive_and_negative_cache_without_rechecking(store, positive):
    calls = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler(rows=None if positive else [], calls=calls))) as client:
        adapter = PublicAirlinePrices(client, store)
        first = await adapter.search("LCA", "ATH", OUT, BACK, "EUR")
        stored_before = [tuple(row) for row in store.db.execute("SELECT * FROM cache")]
        again = await adapter.search("LCA", "ATH", OUT, BACK, "EUR")
        stored_after = [tuple(row) for row in store.db.execute("SELECT * FROM cache")]
    assert len(calls) == (2 if positive else 3)
    assert stored_before == stored_after
    assert all(row["delivery"] == "live_fetch" and row["dispatched"] for row in first["diagnostics"])
    assert all(row["delivery"] == "cache_reuse" and not row["dispatched"] for row in again["diagnostics"])
    for original, cached in zip(first["diagnostics"], again["diagnostics"]):
        assert cached["retrieved_at"] == original["retrieved_at"] == NOW.isoformat()
        assert cached["checked_at"] == original["checked_at"] == NOW.isoformat()
        assert cached["diagnostic_code"] == original["diagnostic_code"]
        assert cached["http_status"] == original["http_status"] == 200
    assert all(row["diagnostic_code"] == ("observed" if positive else "no_matching_date_fare" if row["provider_id"] == AEGEAN else "no_offers")
               for row in first["diagnostics"])


@pytest.mark.asyncio
async def test_local_budget_skip_is_distinct_from_supplier_429_and_does_not_fake_a_check(store):
    calls = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler(rows=[], calls=calls))) as client:
        adapter = PublicAirlinePrices(client, store)
        await adapter.search("LCA", "ATH", OUT, BACK, "EUR")
        await adapter.search("LCA", "ATH", "2026-10-26", BACK, "EUR")
        skipped = await adapter.search("LCA", "ATH", "2026-10-27", BACK, "EUR")
    assert len(calls) == 6
    assert all(row["diagnostic_code"] == "local_rate_limit" and row["delivery"] == "not_attempted"
               and row["dispatched"] is False and row["retrieved_at"] is None for row in skipped["diagnostics"])


@pytest.mark.asyncio
async def test_deadline_retains_completed_and_cancelled_source_attempts(store, monkeypatch):
    monkeypatch.setattr(airlines, "SEARCH_TIMEOUT", 0.03)
    active, calls = 0, []

    async def respond(request):
        nonlocal active
        calls.append(request)
        active += 1
        try:
            if "from-athens-to-larnaca" in request.url.path:
                await asyncio.sleep(1)
            return httpx.Response(200, text=page([]))
        finally:
            active -= 1

    result = await search(store, respond)
    sources = [row for row in result["diagnostics"] if row["provider_id"] == AEGEAN]
    assert len(calls) == len(sources) == 2 and active == 0
    assert {row["diagnostic_code"] for row in sources} == {"no_matching_date_fare", "source_deadline"}
    assert all(row["delivery"] == "live_fetch" and row["dispatched"] for row in sources)
    assert result["diagnostics"][-1]["diagnostic_code"] == "search_deadline"
