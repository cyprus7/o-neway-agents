"""Bounded local source relevance; similarity never verifies travel conditions."""
import asyncio
import hashlib
import json
import math
import os
import re
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx

from .models import TravelIntent
from .domain.travel_data import SourceCacheProtocol


DIMENSIONS = 1024
MAX_ROWS = 15
MAX_BATCH = 4
MAX_TEXT_CHARS = 1600
MAX_INPUT_BYTES = 1800
CACHE_TTL = 7 * 86400
CONTRACT_VERSION = "qwen3-local-v1"
QUERY_INSTRUCTION = (
    "Instruct: Given a travel preference, retrieve relevant accommodation descriptions\nQuery: "
)
Role = Literal["query", "document"]


class EncoderError(Exception):
    """A non-sensitive failure category, safe for internal diagnostics."""


def _bounded_text(text: str, byte_limit: int = MAX_INPUT_BYTES) -> str:
    value = " ".join(text.split())[:MAX_TEXT_CHARS]
    return value.encode("utf-8")[:byte_limit].decode("utf-8", errors="ignore").rstrip()


def encoding_input(text: str, role: Role) -> str:
    if not isinstance(text, str) or role not in ("query", "document"):
        raise EncoderError("invalid_input")
    prefix = QUERY_INSTRUCTION if role == "query" else ""
    value = _bounded_text(text, MAX_INPUT_BYTES - len(prefix.encode("utf-8")))
    if not value:
        raise EncoderError("empty_input")
    return prefix + value


def document_text(row: dict[str, Any]) -> str:
    """Runtime and benchmark use identical provider title/content, never labels or URLs."""
    return str(row.get("title", "")) + " " + str(row.get("content", ""))


def lodging_query(intent: TravelIntent) -> str:
    """Build from current intent; typed transport and app flags cannot become hotel wishes.

    Necessary hotel conditions are already retained in hotel_requirements by the intent
    contract. Do not mix untyped hard constraints (country, budget, route) into retrieval.
    """
    excluded = {
        "short_flight", "direct_flight", "budget_unlimited", "no_purchases",
        "no_bookings_without_consent", "simulation_only", "warm", "mild", "cold",
        "snow", "avoid_heat", "low_wind", "avoid_humidity", "dry_weather",
    }
    prefixes = ("transport:", "transport_requirement:", "exclude_country:",
                "exclude_destination:", "climate:", "weather:", "route:")
    wishes = []
    for value in [*intent.hotel_requirements, *intent.soft_constraints]:
        value = " ".join(value.split())
        key = value.casefold()
        if value and key not in excluded and not key.startswith(prefixes):
            wishes.append(value)
    if intent.pets:
        wishes.append("Питомцы в отеле: " + ", ".join(intent.pets))
    seen = set()
    unique = []
    for value in wishes:
        if value.casefold() not in seen:
            unique.append(value)
            seen.add(value.casefold())
    return _bounded_text("; ".join(unique))


def normalized_vector(value: Any) -> list[float]:
    if not isinstance(value, list) or len(value) != DIMENSIONS:
        raise EncoderError("invalid_dimensions")
    if any(isinstance(item, bool) or not isinstance(item, (int, float)) for item in value):
        raise EncoderError("invalid_numbers")
    vector = [float(item) for item in value]
    if not all(math.isfinite(item) for item in vector):
        raise EncoderError("nonfinite_vector")
    norm = math.hypot(*vector)
    if not math.isfinite(norm) or norm <= 1e-12:
        raise EncoderError("zero_vector")
    return [item / norm for item in vector]


def lexical_overlap(query: str, text: str) -> float:
    words = set(re.findall(r"\w{3,}", query.casefold()))
    return len(words & set(re.findall(r"\w{3,}", text.casefold()))) / max(1, len(words))


def source_caution(query: str, text: str) -> float:
    """Small retrieval-only discount for explicit missing details or restrictive language.

    This is deliberately NOT entailment, policy validation or a hard filter. Development
    cases exposed the encoder's tendency to retrieve negated near-matches. Explicit absence
    requests and common barrier-free wording bypass the denial discount. Unhandled languages
    and implicit contradictions remain a limitation, rather than a reason to confirm a claim.
    """
    value = text.casefold()
    uncertainty = bool(re.search(
        r"\b(?:not|never)\s+(?:\w+\s+)?(?:published|stated|described|documented|specified|mentioned|guaranteed|supplied|explained)\b"
        r"|\b(?:no|without)\s+(?:information|statement|details|guarantee|confirmation)\b"
        r"|\bне\s+(?:указ\w*|опис\w*|опублик\w*|подтвержд\w*|оговор\w*|документир\w*|предусмотр\w*)", value))
    polarity_query = re.sub(r"\b(?:not\s+(?:only|just)|не\s+только)\b", "", query.casefold())
    absence_request = bool(re.search(r"\b(?:no|not|without|free|без|нет|исключ\w*|не)\b|\w+-free\b", polarity_query))
    # These are common desired absences, not evidence of an unavailable amenity.
    denial_text = re.sub(
        r"\bno\s+(?:stairs|steps|thresholds|carpets?|alcohol|smoking|perfume|fragrances?|cross[- ]contact|cross[- ]contamination|wi-?fi|television|telephones?)\b",
        "", value)
    denial = not absence_request and bool(re.search(
        r"\b(?:not|never)\s+(?:\w+\s+)?(?:accept\w*|admit\w*|allow\w*|permit\w*|available)\b"
        r"|\b(?:cannot|can't|unable\s+to|must\s+not|prohibit\w*|forbid\w*)\b"
        r"|\bthere\s+(?:is|are)\s+no\b"
        r"|\b(?:нельзя|запрещ\w*)\b|\bне\s+(?:приним\w*|разреш\w*|допуск\w*)", denial_text))
    # Missing details still make a useful partial source; discount less than an explicit denial.
    return 0.06 * uncertainty + 0.18 * denial


def score_rows(rows: list[dict[str, Any]], query: str, query_vector: list[float],
               document_vectors: list[list[float]], variant: str = "semantic") -> list[dict[str, Any]]:
    """Stable cosine or measured cautious lexical hybrid; never remove source objects."""
    if variant not in ("semantic", "cosine", "hybrid"):
        raise EncoderError("unsupported_variant")
    if len(rows) != len(document_vectors):
        raise EncoderError("invalid_count")
    unit_query = normalized_vector(query_vector)
    scores = [sum(left * right for left, right in zip(unit_query, normalized_vector(vector)))
              for vector in document_vectors]
    if variant == "hybrid":
        scores = [score + 0.04 * lexical_overlap(query, document_text(row))
                  - source_caution(query, document_text(row)) for row, score in zip(rows, scores)]
    return [rows[index] for index in sorted(range(len(rows)), key=lambda index: (-scores[index], index))]


class LocalRanker:
    def __init__(self, client: httpx.AsyncClient, store: SourceCacheProtocol, *, timeout_seconds: float = 8):
        self.client = client
        self.store = store
        self.url = os.getenv("EMBEDDING_URL", "").rstrip("/")
        self.model = os.getenv("EMBEDDING_MODEL", "qwen3-embedding:0.6b")
        self.digest = os.getenv("EMBEDDING_MODEL_DIGEST", "").removeprefix("sha256:").lower()
        self.timeout_seconds = min(8.0, max(0.01, float(timeout_seconds)))
        self._lock = asyncio.Lock()

    @property
    def configured(self) -> bool:
        try:
            parsed = urlsplit(self.url)
            local = parsed.hostname in ("embedding", "localhost", "127.0.0.1", "::1")
            clean = not (parsed.username or parsed.password or parsed.query or parsed.fragment)
            return bool(local and clean and parsed.scheme == "http" and parsed.path in ("", "/")
                        and re.fullmatch(r"[a-f0-9]{64}", self.digest) and self.model)
        except ValueError:
            return False

    def _cache_key(self, text: str, role: Role) -> str:
        scope = [CONTRACT_VERSION, self.model, self.digest, role, text]
        return "embedding:" + hashlib.sha256(json.dumps(scope, ensure_ascii=False).encode()).hexdigest()

    async def _identity(self) -> None:
        if not self.configured:
            raise EncoderError("disabled" if not self.url else "invalid_configuration")
        self.store.increment("embedding_identity_checks")
        response = await self.client.get(self.url + "/api/tags", timeout=self.timeout_seconds,
                                         follow_redirects=False)
        response.raise_for_status()
        payload = response.json()
        models = payload.get("models") if isinstance(payload, dict) else None
        if not isinstance(models, list):
            raise EncoderError("invalid_identity")
        if not any(isinstance(model, dict) and model.get("name") == self.model
                   and str(model.get("digest", "")).removeprefix("sha256:").lower() == self.digest
                   for model in models):
            raise EncoderError("model_identity_mismatch")

    async def _encode(self, texts: list[str], role: Role) -> list[list[float]]:
        if not isinstance(texts, list) or not 1 <= len(texts) <= MAX_ROWS + 1:
            raise EncoderError("invalid_count")
        inputs = [encoding_input(text, role) for text in texts]
        vectors: dict[str, list[float]] = {}
        missing = []
        for text in dict.fromkeys(inputs):
            key = self._cache_key(text, role)
            cached = self.store.cache_get(key)
            if cached:
                try:
                    vectors[text] = normalized_vector(cached[0])
                    self.store.increment("embedding_cache_hits")
                    continue
                except EncoderError:
                    self.store.increment("embedding_cache_invalid")
            missing.append(text)
            self.store.increment("embedding_cache_misses")
        for offset in range(0, len(missing), MAX_BATCH):
            batch = missing[offset:offset + MAX_BATCH]
            self.store.increment("embedding_requests")
            response = await self.client.post(self.url + "/api/embed", json={
                "model": self.model, "input": batch, "dimensions": DIMENSIONS,
                "truncate": False, "keep_alive": "10m", "options": {"num_ctx": 2048},
            }, timeout=self.timeout_seconds, follow_redirects=False)
            response.raise_for_status()
            payload = response.json()
            values = payload.get("embeddings") if isinstance(payload, dict) else None
            if not isinstance(values, list) or len(values) != len(batch):
                raise EncoderError("invalid_count")
            # Validate the entire batch before trusting or persisting any member.
            normalized = [normalized_vector(value) for value in values]
            for text, vector in zip(batch, normalized):
                vectors[text] = vector
                self.store.cache_put(self._cache_key(text, role), vector, CACHE_TTL)
        return [vectors[text] for text in inputs]

    async def embeddings(self, texts: list[str], role: Role = "document") -> list[list[float]]:
        """One bounded operation for the evaluator; corpus loops call this per batch."""
        try:
            async with asyncio.timeout(self.timeout_seconds):
                async with self._lock:
                    await self._identity()
                    return await self._encode(texts, role)
        except TimeoutError as exc:
            raise EncoderError("timeout") from exc
        except (httpx.HTTPError, ValueError, TypeError, OverflowError) as exc:
            raise EncoderError("invalid_response") from exc

    async def rank_rows(self, rows: list[dict[str, Any]], query: str,
                        variant: str = "hybrid") -> list[dict[str, Any]]:
        original = list(rows)
        if not self.url:
            self.store.increment("embedding_disabled")
            return original
        if not query.strip() or len(rows) < 2:
            return original
        if len(rows) > MAX_ROWS:
            self.store.increment("embedding_fallback")
            return original
        try:
            async with asyncio.timeout(self.timeout_seconds):
                async with self._lock:
                    await self._identity()
                    query_vector = (await self._encode([query], "query"))[0]
                    texts = [document_text(row) for row in rows]
                    document_vectors = await self._encode(texts, "document")
                    ranked = score_rows(rows, query, query_vector, document_vectors, variant)
                    self.store.increment("embedding_rankings")
                    if any(left is not right for left, right in zip(original, ranked)):
                        self.store.increment("embedding_reordered")
                    return ranked
        except (TimeoutError, EncoderError, httpx.HTTPError, ValueError, TypeError, OverflowError):
            self.store.increment("embedding_fallback")
            return original
