"""Behavioral local encoder fixtures, never actual provider facts or model quality."""
import asyncio
import copy
import json
import math
import time

import httpx
import pytest

from backend.app.models import TravelIntent
from backend.app.retrieval import (
    DIMENSIONS, MAX_INPUT_BYTES, EncoderError, LocalRanker, encoding_input,
    lodging_query, normalized_vector, score_rows,
)
from backend.app.storage import Store


DIGEST = "a" * 64
MODEL = "qwen3-embedding:0.6b"


def vector(first=1, second=0):
    return [first, second] + [0] * (DIMENSIONS - 2)


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("EMBEDDING_URL", "http://embedding:11434")
    monkeypatch.setenv("EMBEDDING_MODEL", MODEL)
    monkeypatch.setenv("EMBEDDING_MODEL_DIGEST", DIGEST)
    instance = Store(str(tmp_path / "retrieval.db"))
    yield instance
    instance.close()


def handler_for(calls, *, embeddings=None, digest=DIGEST):
    def handler(request):
        calls.append(request)
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": MODEL, "digest": digest}]})
        payload = json.loads(request.content)
        values = embeddings(payload) if embeddings else [vector() for _ in payload["input"]]
        return httpx.Response(200, json={"embeddings": values})
    return handler


def test_current_lodging_wishes_preserve_negations_and_qualifiers_and_exclude_route():
    intent = TravelIntent(origin="Warsaw", destination="Larnaca", pets=["собака 8 кг"],
        hotel_requirements=["без ступеней", "без ступеней", "тихий номер"],
        soft_constraints=["рядом парк", "transport_requirement:кошка в салоне",
                          "transport:train", "short_flight", "weather:низкий ветер",
                          "simulation_only", "warm"],
        hard_constraints=["exclude_country:TR", "direct_flight"])
    query = lodging_query(intent)
    assert "без ступеней" in query and query.count("без ступеней") == 1
    assert "тихий номер" in query and "рядом парк" in query and "собака 8 кг" in query
    assert "Warsaw" not in query and "салоне" not in query and "train" not in query
    assert "short_flight" not in query and "ветер" not in query and "simulation" not in query
    assert "exclude_country" not in query
    replanned = intent.model_copy(update={"hotel_requirements": ["светлая студия"], "pets": [], "soft_constraints": []})
    assert lodging_query(replanned) == "светлая студия"


def test_encoding_query_instruction_and_byte_bound_without_splitting_unicode():
    query = encoding_input(" \n без   ступеней " + "🐕" * 4000, "query")
    assert query.startswith("Instruct: ") and "\nQuery: без ступеней" in query
    assert len(query.encode("utf-8")) <= MAX_INPUT_BYTES
    assert "�" not in query
    assert encoding_input("  room\nquiet ", "document") == "room quiet"


@pytest.mark.parametrize("bad", [[], [1] * 1023, [True] * 1024, ["1"] * 1024,
                                [float("nan")] * 1024, [float("inf")] * 1024, [0] * 1024])
def test_vector_validation_rejects_invalid_and_nonfinite_values(bad):
    with pytest.raises(EncoderError):
        normalized_vector(bad)


def test_pure_cosine_is_stable_and_preserves_source_objects_and_unknown_claims():
    rows = [{"title": "first", "price": None, "status": "unavailable"},
            {"title": "second", "price": None, "status": "unavailable"},
            {"title": "tie", "price": None, "status": "unavailable"}]
    before = copy.deepcopy(rows)
    ranked = score_rows(rows, "quiet", vector(), [vector(0, 1), vector(), vector()])
    assert ranked == [rows[1], rows[2], rows[0]]
    assert ranked[0] is rows[1] and rows == before
    assert all(row["price"] is None and row["status"] == "unavailable" for row in ranked)
    assert math.isclose(math.hypot(*normalized_vector(vector(3, 4))), 1)


@pytest.mark.asyncio
async def test_cache_reuses_exact_normalized_inputs_but_keeps_roles_and_query_changes_separate(store):
    calls = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler_for(calls))) as client:
        ranker = LocalRanker(client, store)
        first = await ranker.embeddings(["quiet room", " quiet\nroom "], "document")
        second = await ranker.embeddings(["quiet room"], "document")
        await ranker.embeddings(["quiet room"], "query")
        await ranker.embeddings(["room without stairs"], "query")
    assert first[0] == first[1] == second[0]
    posts = [call for call in calls if call.method == "POST"]
    assert len(posts) == 3 and len(json.loads(posts[0].content)["input"]) == 1
    assert json.loads(posts[1].content)["input"][0].startswith("Instruct: ")
    assert store.metrics()["embedding_cache_hits"] >= 1


@pytest.mark.asyncio
async def test_model_identity_mismatch_rejects_even_previously_cached_vectors(store):
    calls = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler_for(calls))) as client:
        ranker = LocalRanker(client, store)
        await ranker.embeddings(["quiet"], "document")
    calls = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler_for(calls, digest="b" * 64))) as client:
        with pytest.raises(EncoderError, match="identity"):
            await LocalRanker(client, store).embeddings(["quiet"], "document")
    assert all(call.method == "GET" for call in calls)


@pytest.mark.asyncio
async def test_model_digest_scopes_vectors_even_when_same_tag_is_replaced(store, monkeypatch):
    calls = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler_for(calls))) as client:
        await LocalRanker(client, store).embeddings(["quiet"], "document")
    monkeypatch.setenv("EMBEDDING_MODEL_DIGEST", "b" * 64)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler_for(calls, digest="b" * 64))) as client:
        await LocalRanker(client, store).embeddings(["quiet"], "document")
    assert len([call for call in calls if call.method == "POST"]) == 2


@pytest.mark.asyncio
async def test_batches_are_at_most_four_and_order_is_retained(store):
    calls = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler_for(calls))) as client:
        result = await LocalRanker(client, store).embeddings([f"room {index}" for index in range(11)])
    assert len(result) == 11
    assert [len(json.loads(call.content)["input"]) for call in calls if call.method == "POST"] == [4, 4, 3]
    assert all(json.loads(call.content)["truncate"] is False for call in calls if call.method == "POST")


@pytest.mark.asyncio
async def test_corrupted_cached_vector_is_reencoded_instead_of_used(store):
    calls = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler_for(calls))) as client:
        ranker = LocalRanker(client, store)
        key = ranker._cache_key(encoding_input("quiet", "document"), "document")
        store.cache_put(key, [0] * 1024, 100)
        assert (await ranker.embeddings(["quiet"]))[0] == vector()
    assert store.metrics()["embedding_cache_invalid"] == 1
    assert len([call for call in calls if call.method == "POST"]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["http", "count", "dimension", "nan", "redirect", "json"])
async def test_failed_rank_returns_exact_original_rows_and_no_changed_evidence(store, kind):
    calls = []
    good_handler = handler_for(calls)
    def handler(request):
        if request.url.path == "/api/tags":
            return good_handler(request)
        if kind == "http":
            return httpx.Response(503)
        if kind == "redirect":
            return httpx.Response(307, headers={"Location": "https://external.example/secret"})
        if kind == "json":
            return httpx.Response(200, text="not json")
        value = [] if kind == "count" else [[0] * 3] if kind == "dimension" else [[float("nan")] * 1024]
        if kind == "nan":
            return httpx.Response(200, content=json.dumps({"embeddings": value}).encode())
        return httpx.Response(200, json={"embeddings": value})
    rows = [{"title": "unrelated", "content": "city center", "price": None},
            {"title": "relevant", "content": "quiet room", "pet_policy": "unknown"}]
    before = copy.deepcopy(rows)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True) as client:
        ranked = await LocalRanker(client, store).rank_rows(rows, "quiet")
    assert ranked == rows == before and all(left is right for left, right in zip(ranked, rows))
    assert store.metrics()["embedding_fallback"] == 1


@pytest.mark.asyncio
async def test_rank_uses_new_query_on_replan_and_caches_unchanged_descriptions(store):
    calls = []
    def values(payload):
        return [vector(0, 1) if ("studio" in text or "bright" in text) else vector() for text in payload["input"]]
    rows = [{"title": "quiet room", "content": "silent"}, {"title": "studio", "content": "bright"}]
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler_for(calls, embeddings=values))) as client:
        ranker = LocalRanker(client, store)
        first = await ranker.rank_rows(rows, "quiet room")
        second = await ranker.rank_rows(rows, "bright studio")
    assert first == rows and second == list(reversed(rows))
    posts = [json.loads(call.content) for call in calls if call.method == "POST"]
    assert len(posts) == 3  # first query, both descriptions, changed query; no old score cache
    assert store.metrics()["embedding_reordered"] == 1


@pytest.mark.asyncio
async def test_disabled_invalid_url_and_empty_wishes_do_not_dispatch(store, monkeypatch):
    def handler(request):
        pytest.fail("bypassed requests must not call any service")
    rows = [{"title": "a"}, {"title": "b"}]
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        monkeypatch.delenv("EMBEDDING_URL")
        assert await LocalRanker(client, store).rank_rows(rows, "quiet") == rows
        monkeypatch.setenv("EMBEDDING_URL", "https://external.example")
        assert await LocalRanker(client, store).rank_rows(rows, "quiet") == rows
        monkeypatch.setenv("EMBEDDING_URL", "http://embedding:11434")
        assert await LocalRanker(client, store).rank_rows(rows, " ") == rows
    assert store.metrics()["embedding_disabled"] == 1
    assert store.metrics()["embedding_fallback"] == 1


@pytest.mark.asyncio
async def test_timeout_includes_waiting_for_shared_operation_lock(store):
    entered = asyncio.Event()
    async def handler(request):
        entered.set()
        await asyncio.sleep(1)
        return httpx.Response(503)
    rows = [{"title": "first"}, {"title": "second"}]
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        ranker = LocalRanker(client, store, timeout_seconds=0.05)
        holding = asyncio.create_task(ranker.embeddings(["quiet"]))
        await entered.wait()
        start = time.monotonic()
        assert await ranker.rank_rows(rows, "quiet") == rows
        assert time.monotonic() - start < 0.15
        with pytest.raises(EncoderError, match="timeout"):
            await holding


@pytest.mark.asyncio
async def test_simultaneous_encoding_operations_are_serialized(store):
    active = maximum = 0
    calls = []
    regular = handler_for(calls)
    async def handler(request):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        await asyncio.sleep(0.005)
        try:
            return regular(request)
        finally:
            active -= 1
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        ranker = LocalRanker(client, store)
        await asyncio.gather(*(ranker.embeddings([f"quiet {index}"]) for index in range(4)))
    assert maximum == 1


@pytest.mark.asyncio
async def test_cancelled_ranking_releases_lock_and_does_not_swallow_cancellation(store):
    entered = asyncio.Event()
    calls = []
    regular = handler_for(calls)
    slow = True
    async def handler(request):
        if slow:
            entered.set()
            await asyncio.sleep(10)
        return regular(request)
    rows = [{"title": "first"}, {"title": "second"}]
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        ranker = LocalRanker(client, store)
        task = asyncio.create_task(ranker.rank_rows(rows, "quiet"))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        slow = False
        assert await ranker.rank_rows(rows, "quiet") == rows
