"""Bounded search evidence, independent of travel inventory and price arithmetic."""
from .adapters import FederatedSearch
from .domain import SearchHit, SearchOutcome, fair_merge, public_search_url

__all__ = ["FederatedSearch", "SearchHit", "SearchOutcome", "fair_merge", "public_search_url"]
