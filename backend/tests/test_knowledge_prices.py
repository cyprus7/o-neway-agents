"""Grounded extraction and shared inference accounting; all pages here are fixtures."""
import asyncio
from datetime import timedelta, datetime, timezone
import hashlib
import json
from types import SimpleNamespace

import httpx
import pytest

from backend.app.domain.travel_data import ComponentBundle, PriceDocument, context_for
from backend.app.geography import find_place
from backend.app.infrastructure.knowledge_travel_data import (
    ExtractedPrice, KnowledgeTravelDataPort, accepted_observation,
)
from backend.app.llm import LLMClient, SlidingWindowLimiter
from backend.app.models import TravelComponent, TravelIntent, local_today
from backend.app.pricing.normalization import request_context_hash
from backend.app.storage import Store
from backend.tests.test_core import FakeClock


HOTEL = "Hotel Indigo Berlin - East Side Gallery by IHG"


def scenario(city="Берлин", hotel=HOTEL):
    place, origin = find_place(city), find_place("Ларнака")
    intent = TravelIntent(origin=origin.name, destination=place.name,
        date_from=local_today() + timedelta(days=10), date_to=local_today() + timedelta(days=12),
        budget_total=1000, adults=2, rooms=1)
    bundle = ComponentBundle(flight=TravelComponent(title="Flight", description="Unknown"),
        stay=TravelComponent(title=hotel, description="Indexed hotel", source_url="https://www.booking.com/hotel/de/indigo.html"),
        transfer=TravelComponent(title="Аэропорт → размещение → аэропорт", description="Unknown"))
    return place, origin, intent, bundle


def document(component, text, place, origin, intent, bundle):
    entity = bundle.stay.details.get("pricing_entity_id")
    if component == "stay" and not entity:
        entity = "knowledge-property:" + hashlib.sha256((bundle.stay.title + "\n" + str(bundle.stay.source_url)).encode()).hexdigest()[:24]
    context = context_for(component, place, origin, intent, entity_id=entity if component == "stay" else None)
    return PriceDocument(component=component, title=bundle.stay.title if component == "stay" else "Airport transfers",
        content=text, source_url="https://supplier.example/" + component,
        retrieved_at=datetime.now(timezone.utc), context_hash=request_context_hash(context))


def extraction(component="stay", text=None, amount="125", index=0):
    text = text or f"{HOTEL}. EUR 125 per room per night in Berlin."
    return ExtractedPrice(component=component, document_index=index, amount=amount, currency="EUR",
        evidence_text=text, price_text="EUR " + amount,
        unit_text="per room per night" if component == "stay" else "per vehicle",
        unit="room_night" if component == "stay" else "group_trip",
        trip_type="not_applicable",
        destination_text="Berlin" if component == "transfer" else None)


def test_hotel_price_is_sourced_reference_with_original_scope_and_time():
    place, origin, intent, bundle = scenario()
    item = extraction()
    doc = document("stay", item.evidence_text, place, origin, intent, bundle)
    result = accepted_observation(item, doc, bundle, place, origin, intent)
    assert result.value.amount == "125" and result.outcome == "reference"
    assert result.normalized_group is None and result.scope.date_from is None
    assert result.scope.adults is None and result.scope.unit == "room_night"
    assert not result.availability_verified
    assert result.observed_at == result.retrieved_at == doc.retrieved_at


@pytest.mark.parametrize("change", ["amount", "currency", "wrong_hotel", "wrong_city", "unit", "proof", "hash"])
def test_rejects_model_hallucination_or_unrelated_source(change):
    place, origin, intent, bundle = scenario()
    item = extraction()
    doc = document("stay", item.evidence_text, place, origin, intent, bundle)
    if change == "amount":
        item = item.model_copy(update={"amount": "126"})
    elif change == "currency":
        item = item.model_copy(update={"currency": "USD"})
    elif change == "wrong_hotel":
        bundle.stay.title = "Hotel Indigo Berlin Alexanderplatz by IHG"
    elif change == "wrong_city":
        place = find_place("Прага")
    elif change == "unit":
        item = item.model_copy(update={"unit": "group_stay"})
    elif change == "proof":
        item = item.model_copy(update={"evidence_text": item.evidence_text + " Invented context"})
    else:
        doc.context_hash = "0" * 64
    with pytest.raises(ValueError):
        accepted_observation(item, doc, bundle, place, origin, intent)


def test_transfer_preserves_one_way_vehicle_tariff_without_multiplication():
    place, origin, intent, bundle = scenario()
    text = "BER airport Berlin taxi one way EUR 65 per vehicle."
    item = extraction("transfer", text=text, amount="65")
    item = item.model_copy(update={"unit_text": "taxi one way EUR 65 per vehicle", "trip_type": "one_way"})
    result = accepted_observation(item, document("transfer", text, place, origin, intent, bundle), bundle, place, origin, intent)
    assert result.value.amount == "65" and result.scope.trip_type == "one_way"
    assert "одну сторону" in result.unit_label
    assert result.normalized_group is None
    with pytest.raises(ValueError, match="return_not_sourced"):
        accepted_observation(item.model_copy(update={"trip_type": "roundtrip"}),
            document("transfer", text, place, origin, intent, bundle), bundle, place, origin, intent)


@pytest.mark.asyncio
@pytest.mark.parametrize("source_delivery", ["live_fetch", "cache_reuse"])
async def test_single_batch_and_cache_share_existing_model_client_limiter(tmp_path, monkeypatch, source_delivery):
    monkeypatch.setenv("LLM_ENABLED", "1")
    monkeypatch.setenv("LLM_API_KEY", "synthetic-secret")
    place, origin, intent, bundle = scenario()
    stay = extraction()
    transfer = extraction("transfer", text="BER airport Berlin taxi one way EUR 65 per vehicle.", amount="65", index=1)
    calls = []

    def reply(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({"prices": [stay.model_dump(), transfer.model_dump()]})}}]})

    class Base:
        document_calls = 0

        async def inventory(self, *args):
            return bundle.model_copy(deep=True)

        async def price_documents(self, *args):
            self.document_calls += 1
            return SimpleNamespace(documents=[doc.model_copy(update={"delivery": source_delivery}) for doc in (
                document("stay", stay.evidence_text, place, origin, intent, bundle),
                document("transfer", transfer.evidence_text, place, origin, intent, bundle))])

    store = Store(str(tmp_path / "quota.db"))
    async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as http:
        llm = LLMClient(http, store)
        clock = FakeClock()
        llm.limiter = SlidingWindowLimiter(clock=clock.clock, sleep=clock.sleep)
        # Nine planning calls have already consumed the same worker's window.
        for _ in range(9):
            await llm.completion([], None)
        port = KnowledgeTravelDataPort(Base(), llm)
        first, duplicate = await asyncio.gather(port.inventory(place, origin, intent), port.inventory(place, origin, intent))
        assert first.stay.quote.value.amount == "125" and first.transfer.quote.value.amount == "65"
        assert duplicate.stay.quote == first.stay.quote
        assert first.stay.details["pricing_observation_delivery"] == source_delivery
        assert first.transfer.details["pricing_observation_delivery"] == source_delivery
        assert first.stay.evidence[-1].freshness == ("cached" if source_delivery == "cache_reuse" else "live")
        assert duplicate.stay.details["pricing_observation_delivery"] == "cache_reuse"
        assert duplicate.transfer.details["pricing_observation_delivery"] == "cache_reuse"
        assert len(calls) == 10 and store.metrics()["llm_requests"] == 10
        assert port.base.document_calls == 1
        await llm.completion([], None)
        assert clock.now >= 60 and len(calls) == 11
        assert len(llm.limiter.timestamps) <= 10
    store.close()


@pytest.mark.asyncio
async def test_source_or_model_failure_keeps_existing_observation_and_cancels():
    place, origin, intent, bundle = scenario()
    stay = extraction()
    bundle.stay.quote = accepted_observation(stay, document("stay", stay.evidence_text, place, origin, intent, bundle), bundle, place, origin, intent)
    bundle.stay.details["pricing_entity_id"] = bundle.stay.quote.scope.entity_id

    class Base:
        async def refresh_inventory(self, *args):
            value = args[-1].model_copy(deep=True)
            value.stay.quote = None
            return value

        async def price_documents(self, *args):
            raise TimeoutError

    port = KnowledgeTravelDataPort(Base(), SimpleNamespace(configured=True))
    result = await port.refresh_inventory(place, origin, intent, bundle)
    assert result.stay.quote == bundle.stay.quote
    assert result.stay.quote.retrieved_at == bundle.stay.quote.retrieved_at
    assert port._locks == {}


def test_non_berlin_hotel_uses_identical_extractor():
    place, origin, intent, bundle = scenario("Прага", "Hotel Example Prague")
    item = extraction(text="Hotel Example Prague EUR 125 per room per night.")
    result = accepted_observation(item, document("stay", item.evidence_text, place, origin, intent, bundle), bundle, place, origin, intent)
    assert result.value.amount == "125"


@pytest.mark.asyncio
async def test_abandoned_extraction_cancels_model_and_releases_context_slots():
    place, origin, intent, bundle = scenario()
    item = extraction()
    started, cancelled = asyncio.Event(), asyncio.Event()

    class Base:
        async def inventory(self, *args):
            return bundle.model_copy(deep=True)

        async def price_documents(self, *args):
            return SimpleNamespace(documents=[document("stay", item.evidence_text, place, origin, intent, bundle)])

    class Model:
        configured = True

        async def completion(self, *args, **kwargs):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

    port = KnowledgeTravelDataPort(Base(), Model())
    task = asyncio.create_task(port.inventory(place, origin, intent))
    await asyncio.wait_for(started.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled.is_set() and not port._locks and not port._cache
    assert port._slots._value == 3
