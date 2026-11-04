"""Executable adoption example and dependency direction, not provider behavior."""
import ast
import json
from pathlib import Path
import sqlite3
import subprocess
import sys

from fastapi.testclient import TestClient
import pytest

from examples.host_integration import create_host


def test_existing_host_mount_owns_agent_lifespan_and_session_api(tmp_path):
    class HostDataPort:
        async def capabilities(self):
            return {"external_data_fallback_enabled": False, "providers": {}, "pricing": {"refresh_enabled": False},
                    "gateway": {"status": "synthetic_host_adapter"}}

    host = create_host(database_path=str(tmp_path / "host.sqlite3"), travel_data_factory=lambda _: HostDataPort())
    with TestClient(host, base_url="http://localhost") as client:
        assert client.get("/health").json()["service"] == "host-product"
        assert client.get("/travel/health").json()["service"] == "one-request"
        login = client.post("/travel/api/auth/guest", headers={"Origin": "http://localhost"})
        assert login.status_code == 200
        client.headers.update({"Origin": "http://localhost", "X-CSRF-Token": login.json()["csrf_token"]})
        created = client.post("/travel/api/sessions", json={})
        assert created.status_code == 201
        session_id = created.json()["id"]
        assert client.get(f"/travel/api/sessions/{session_id}").json()["id"] == session_id
        assert client.get("/travel/api/config").json()["data_gateway"]["status"] == "synthetic_host_adapter"
    with pytest.raises(sqlite3.ProgrammingError):
        host.state.travel_agent.state.store.db.execute("SELECT 1")


def test_agent_composition_does_not_load_provider_implementations():
    root = Path(__file__).resolve().parents[2]
    code = "import json,sys; import backend.app.main; print(json.dumps(sorted(k for k in sys.modules if k.startswith(('backend.app.providers','backend.app.catalog','backend.app.search','backend.app.retrieval','backend.app.regional_events.discovery','backend.app.regional_events.adapters','backend.app.data_gateway')))))"
    result = subprocess.run([sys.executable, "-c", code], cwd=root, capture_output=True, text=True, check=True, timeout=15)
    assert json.loads(result.stdout) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("close_fails", [False, True])
async def test_host_failure_closes_owned_client_and_store(tmp_path, close_fails):
    class HostDataPort:
        closed = False

        async def aclose(self):
            self.closed = True
            if close_fails:
                raise RuntimeError("adapter_close_failed")

    port, clients = HostDataPort(), []

    def factory(client):
        clients.append(client)
        return port

    host = create_host(database_path=str(tmp_path / "host.sqlite3"), travel_data_factory=factory)
    with pytest.raises(RuntimeError, match="adapter_close_failed" if close_fails else "host_failed"):
        async with host.router.lifespan_context(host):
            raise RuntimeError("host_failed")
    assert port.closed and clients[0].is_closed
    with pytest.raises(sqlite3.ProgrammingError):
        host.state.travel_agent.state.store.db.execute("SELECT 1")


@pytest.mark.asyncio
async def test_adapter_startup_failure_closes_client_and_database(tmp_path, monkeypatch):
    from backend.app import main

    stores, clients = [], []
    original_store = main.Store

    def recording_store(*args, **kwargs):
        store = original_store(*args, **kwargs)
        stores.append(store)
        return store

    def broken_factory(client):
        clients.append(client)
        raise ValueError("adapter_configuration_invalid")

    monkeypatch.setattr(main, "Store", recording_store)
    host = create_host(database_path=str(tmp_path / "host.sqlite3"), travel_data_factory=broken_factory)
    with pytest.raises(ValueError, match="adapter_configuration_invalid"):
        async with host.router.lifespan_context(host):
            pytest.fail("invalid adapter must prevent startup")
    assert clients[0].is_closed
    with pytest.raises(sqlite3.ProgrammingError):
        stores[0].db.execute("SELECT 1")


def test_planner_uses_application_ports_and_has_no_provider_internals():
    tree = ast.parse((Path(__file__).parents[1] / "app/planner.py").read_text(encoding="utf-8"))
    concrete = {"providers", "storage", "llm", "catalog", "search", "retrieval", "infrastructure"}
    assert not [node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
                and node.module and node.module.split(".")[0] in concrete]
    assert not [node for node in ast.walk(tree) if isinstance(node, ast.Attribute) and node.attr == "pricing"
                and isinstance(node.value, ast.Attribute) and node.value.attr == "providers"]
