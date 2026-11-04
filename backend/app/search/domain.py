"""Search values and stable merging; snippets are never verified provider facts."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Literal
from urllib.parse import urlsplit, urlunsplit


Freshness = Literal["live", "cached"]


def public_search_url(value: Any) -> str | None:
    if not isinstance(value, str) or len(value) > 3000:
        return None
    try:
        parsed = urlsplit(value)
        if (parsed.scheme in ("http", "https") and parsed.hostname
                and not parsed.username and not parsed.password):
            parsed.port  # Reject malformed ports without fetching a result URL.
            return value
    except ValueError:
        pass
    return None


def url_identity(url: str) -> str:
    """Fragments cannot identify a different source; preserve meaningful query parameters."""
    parsed = urlsplit(url)
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path or "/", parsed.query, ""))


@dataclass(frozen=True)
class SearchHit:
    provider: str
    title: str
    url: str
    snippet: str
    retrieved_at: str
    freshness: Freshness
    engine: str = "web"
    stale_if_error: bool = False

    @property
    def provider_label(self) -> str:
        return "Ollama Web Search" if self.provider == "ollama" else "SearXNG / " + self.engine

    def row(self) -> dict[str, Any]:
        # Ranking consumes title/content only. Keep the original typed value attached.
        return {"title": self.title, "url": self.url, "content": self.snippet,
                "engine": self.engine, "_search_hit": self}


@dataclass(frozen=True)
class SearchOutcome:
    provider: str
    hits: tuple[SearchHit, ...]
    status: Literal["live", "cached", "unavailable", "disabled", "cooldown"]
    reason: str | None = None
    retrieved_at: str | None = None


def fair_merge(groups: Iterable[Iterable[SearchHit]], *, limit: int = 15,
               seen: set[str] | None = None) -> list[SearchHit]:
    """Round-robin eligible providers before the ranker cap, retaining first provenance."""
    iterators = [iter(group) for group in groups]
    result: list[SearchHit] = []
    identities = seen if seen is not None else set()
    while iterators and len(result) < limit:
        remaining = []
        for iterator in iterators:
            for hit in iterator:
                identity = url_identity(hit.url)
                if identity not in identities:
                    identities.add(identity)
                    result.append(hit)
                    remaining.append(iterator)
                    break
            if len(result) >= limit:
                break
        iterators = remaining
    return result
