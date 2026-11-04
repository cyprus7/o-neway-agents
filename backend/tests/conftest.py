"""Keep unit tests independent of the operator's hosted-search credentials."""
import pytest


@pytest.fixture(autouse=True)
def no_ambient_hosted_search_key(monkeypatch):
    # Tests that exercise Ollama explicitly opt in with a synthetic key. Importing
    # main may load .env during collection; it must not enable an extra provider
    # in older geography/ranker fixtures or accidentally dispatch a real request.
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
    # Operators may deliberately pause inference for maintenance. Unit tests
    # control this switch explicitly and opt in with synthetic keys/transports.
    monkeypatch.delenv("LLM_ENABLED", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    # Existing provider tests exercise explicit remote fixtures. Catalog tests
    # opt in separately so a new bundled release cannot change their subjects.
    monkeypatch.setenv("LOCAL_CATALOG_ENABLED", "0")
    monkeypatch.setenv("WEB_FALLBACK_ENABLED", "1")
