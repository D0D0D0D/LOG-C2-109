"""Standalone auth, payload-ref, and resume-ownership HTTP contracts."""

from __future__ import annotations

from types import SimpleNamespace

from fastapi.testclient import TestClient
from framework.schemas.trust_level import TrustLevel
from payload_store import PayloadStore

from src.api import server


class FakeCompiled:
    def __init__(self, values_by_thread=None):
        self.values_by_thread = values_by_thread or {}

    def get_state(self, config):
        values = self.values_by_thread.get(config["configurable"]["thread_id"])
        return SimpleNamespace(values=values)


class FakeAgent:
    def __init__(self, provider, values_by_thread=None):
        self._secrets_provider = provider
        self._compiled = FakeCompiled(values_by_thread)
        self.calls = []

    def invoke(self, input_value, *, ctx):
        self.calls.append(("invoke", input_value, ctx))
        return {"status": "success", "output": {"caller_id": ctx.caller_id, "trust": ctx.caller_trust_level.value}}

    def resume(self, thread_id, feedback):
        self.calls.append(("resume", thread_id, feedback))
        return {"status": "success", "output": {"resumed": True}}


def _install_server(monkeypatch, *, mapping=None, checkpoints=None):
    store = PayloadStore(scope_keys=("customer_id",), ttl_seconds=300)
    config = {"caller_customer_map": mapping or {}, "payload_store": store}
    fake = FakeAgent(server.agent._secrets_provider, checkpoints)
    monkeypatch.setattr(server, "runtime_config", config)
    monkeypatch.setattr(server, "payload_store", store)
    monkeypatch.setattr(server, "agent", fake)
    return TestClient(server.app), fake


def test_standalone_bearer_auth_establishes_verified_context_for_invoke_and_resume(monkeypatch):
    monkeypatch.setenv("INVOKE_AUTH_TOKEN", "mock-invoke-auth-token")
    client, _ = _install_server(
        monkeypatch,
        mapping={"standalone_bearer": "customer_a"},
        checkpoints={"test-thread": {"customer_id": "customer_a"}},
    )
    headers = {"Authorization": "Bearer mock-invoke-auth-token"}

    invoke = client.post("/invoke", json={"input": "{}"}, headers=headers)
    resume = client.post("/resume", json={"thread_id": "test-thread", "feedback": {"action": "approve"}}, headers=headers)

    assert invoke.status_code == 200
    assert invoke.json()["output"] == {"caller_id": "standalone_bearer", "trust": TrustLevel.VERIFIED_EXTERNAL.value}
    assert resume.status_code == 200


def test_standalone_bearer_auth_rejects_missing_or_incorrect_bearer(monkeypatch):
    monkeypatch.setenv("INVOKE_AUTH_TOKEN", "mock-invoke-auth-token")
    client, _ = _install_server(monkeypatch)

    for headers in ({}, {"Authorization": "Bearer wrong-mock-token"}):
        response = client.post("/invoke", json={"input": "{}"}, headers=headers)
        assert response.status_code == 401
        assert response.json() == {"detail": "Token is invalid or expired."}


def test_env_unset_preserves_scaffold_non_http_auth_behaviour(monkeypatch):
    monkeypatch.delenv("INVOKE_AUTH_TOKEN", raising=False)
    client, _ = _install_server(monkeypatch)
    response = client.post("/invoke", json={"input": "{}"})

    assert response.status_code == 200
    assert response.json()["output"]["trust"] == TrustLevel.ANONYMOUS.value


def test_resume_rejects_unknown_and_cross_customer_checkpoint_before_resume(monkeypatch):
    monkeypatch.setenv("INVOKE_AUTH_TOKEN", "mock-invoke-auth-token")
    client, fake = _install_server(
        monkeypatch,
        mapping={"standalone_bearer": "customer_a"},
        checkpoints={"other-thread": {"customer_id": "customer_b"}, "empty-owner-thread": {"customer_id": ""}, "own-thread": {"customer_id": "customer_a"}},
    )
    headers = {"Authorization": "Bearer mock-invoke-auth-token"}

    assert client.post("/resume", json={"thread_id": "unknown-thread", "feedback": {"action": "approve"}}, headers=headers).status_code == 404
    assert client.post("/resume", json={"thread_id": "other-thread", "feedback": {"action": "approve"}}, headers=headers).status_code == 403
    assert client.post("/resume", json={"thread_id": "empty-owner-thread", "feedback": {"action": "approve"}}, headers=headers).status_code == 403
    valid = client.post("/resume", json={"thread_id": "own-thread", "feedback": {"action": "approve"}}, headers=headers)

    assert valid.status_code == 200
    assert fake.calls == [("resume", "own-thread", {"action": "approve"})]


def test_resume_authentication_precedes_checkpoint_lookup(monkeypatch):
    monkeypatch.setenv("INVOKE_AUTH_TOKEN", "mock-invoke-auth-token")
    client, _ = _install_server(monkeypatch, checkpoints={"own-thread": {"customer_id": "customer_a"}})

    assert client.post("/resume", json={"thread_id": "own-thread", "feedback": {"action": "approve"}}).status_code == 401
    assert client.post("/resume", json={"thread_id": "own-thread", "feedback": {"action": "approve"}}, headers={"Authorization": "Bearer wrong-mock-token"}).status_code == 401
