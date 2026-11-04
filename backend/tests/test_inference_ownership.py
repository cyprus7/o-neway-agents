"""The explicit maintenance switch stops dispatch before quota acquisition."""
import asyncio

import httpx
import pytest

from backend.app.llm import LLMClient, PlannerUnavailable
from backend.app.storage import Store


@pytest.mark.parametrize("value", ["0", "false", "off", "", "invalid"])
def test_inactive_environment_cannot_dispatch_even_with_a_key(tmp_path, monkeypatch, value):
    monkeypatch.setenv("LLM_ENABLED", value)
    monkeypatch.setenv("LLM_API_KEY", "synthetic-secret")
    calls = []

    async def scenario():
        store = Store(str(tmp_path / "owner.db"))
        session = store.create_session()
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: calls.append(request))) as http:
            client = LLMClient(http, store)
            assert not client.configured
            with pytest.raises(PlannerUnavailable, match="Планирование временно недоступно"):
                await client.completion([], session.id)
            assert not client.limiter.timestamps
            assert store.metrics().get("llm_requests", 0) == 0
        store.close()

    asyncio.run(scenario())
    assert calls == []


def test_dispatch_defaults_to_enabled_for_existing_compose(tmp_path, monkeypatch):
    monkeypatch.delenv("LLM_ENABLED", raising=False)
    monkeypatch.setenv("LLM_API_KEY", "synthetic-secret")
    store = Store(str(tmp_path / "default.db"))
    client = LLMClient(None, store)
    assert client.enabled and client.configured
    store.close()
