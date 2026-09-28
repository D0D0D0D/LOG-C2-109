"""Marketplace entry-adapter boundary tests."""

from __future__ import annotations

import json
import sys
from types import ModuleType
from typing import Any

import pytest
from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from framework.secrets.base import MissingSecret, NullProvider
from framework.secrets.context import bound_secrets

import cli
from src.graph.graph import CustomsDocValidatorGraph


def _set_required_environment(monkeypatch, suffix: str = "one") -> dict[str, str]:
    values = {
        "AZURE_OPENAI_API_KEY": f"{suffix}-key",
        "AZURE_OPENAI_ENDPOINT": f"https://{suffix}.azure.example",
        "AZURE_OPENAI_DEPLOYMENT": f"{suffix}-deployment",
        cli._CALLER_ID_ENV: f"marketplace-caller-{suffix}",
        cli._CUSTOMER_ID_ENV: f"marketplace-customer-{suffix}",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    return values


def _packet(*, mode: str | None = None, customer_id: str = "caller-claim") -> str:
    packet: dict[str, Any] = {
        "customer_id": customer_id,
        "shipment_case_id": "case-1",
        "document_packet": [
            {
                "source_id": "invoice-1",
                "document_type": "invoice",
                "extraction_confidence": 1.0,
            }
        ],
    }
    if mode is not None:
        packet["mode"] = mode
    return json.dumps(packet)


def _contains(value: Any, needle: str) -> bool:
    if isinstance(value, dict):
        return any(_contains(key, needle) or _contains(item, needle) for key, item in value.items())
    if isinstance(value, (list, tuple)):
        return any(_contains(item, needle) for item in value)
    return needle in value if isinstance(value, str) else False


def test_marketplace_discards_runner_context_uses_deployment_scope_and_disables_hitl(monkeypatch):
    values = _set_required_environment(monkeypatch)
    sentinel = "UNSCANNED-CONVERSATION-109"
    captured = {}

    def parent_invoke(self, user_input, session_id="", ctx=None, input_context=None):
        captured.update(user_input=user_input, session_id=session_id, ctx=ctx, input_context=input_context)
        return {"status": "success"}

    monkeypatch.setattr(CustomsDocValidatorGraph, "invoke", parent_invoke)
    original_ctx = InvocationContext(
        caller_id="runner-claim",
        caller_trust_level=TrustLevel.VERIFIED_EXTERNAL,
        hitl_allowed=True,
    )
    result = cli.MarketplaceGraph().invoke(
        _packet(customer_id="attacker-customer"),
        ctx=original_ctx,
        input_context={
            "conversation_history": [{"role": "user", "content": sentinel}],
            "customer_id": "attacker-customer",
            "future_runner_key": sentinel,
        },
    )

    assert result == {"status": "success"}
    assert captured["input_context"] == {"customer_id": values[cli._CUSTOMER_ID_ENV]}
    assert not _contains(captured["input_context"], sentinel)
    assert json.loads(captured["user_input"])["customer_id"] == values[cli._CUSTOMER_ID_ENV]
    assert captured["ctx"] is not original_ctx
    assert captured["ctx"].caller_id == values[cli._CALLER_ID_ENV]
    assert captured["ctx"].caller_trust_level is TrustLevel.VERIFIED_EXTERNAL
    assert captured["ctx"].hitl_allowed is False


def test_runner_conversation_history_never_enters_real_graph_state(monkeypatch):
    values = _set_required_environment(monkeypatch)
    sentinel = "UNSCANNED-CONVERSATION-STATE-109"
    agent = cli.MarketplaceGraph()
    agent.compile()
    original_invoke = agent._compiled.invoke
    captured = {}

    def capture_state(initial_state, config=None):
        captured["initial"] = initial_state
        final_state = original_invoke(initial_state, config=config)
        captured["final"] = final_state
        return final_state

    agent._compiled.invoke = capture_state
    result = agent.invoke(
        _packet(),
        ctx=InvocationContext(caller_trust_level=TrustLevel.VERIFIED_EXTERNAL),
        input_context={"conversation_history": [{"role": "user", "content": sentinel}]},
    )

    assert captured["initial"]["input_context"] == {"customer_id": values[cli._CUSTOMER_ID_ENV]}
    assert not _contains(captured["initial"], sentinel)
    assert not _contains(captured["final"], sentinel)
    assert not _contains(result, sentinel)
    assert result["status"] == "success"


@pytest.mark.parametrize("mode", ["ingest", "feedback", "write"])
def test_marketplace_rejects_write_modes(monkeypatch, mode):
    values = _set_required_environment(monkeypatch)

    adapted = json.loads(cli._marketplace_user_input(_packet(mode=mode), values[cli._CUSTOMER_ID_ENV]))

    assert adapted == {"mode": cli._MARKETPLACE_REJECTION_MODE}


def test_marketplace_read_only_kb_rejects_writes():
    kb = cli._MarketplaceReadOnlyCustomerKB()

    assert kb.search("customs completeness") == []
    with pytest.raises(RuntimeError, match="writes are not available"):
        kb.upsert([])
    with pytest.raises(RuntimeError, match="writes are not available"):
        kb.delete([])


@pytest.mark.parametrize("missing_key", cli._REQUIRED_SECRET_KEYS)
def test_missing_required_environment_fails_during_startup(monkeypatch, missing_key):
    _set_required_environment(monkeypatch)
    monkeypatch.delenv(missing_key)

    with pytest.raises(MissingSecret, match=missing_key):
        cli.MarketplaceGraph().provision_secrets(NullProvider())


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://example.cognitiveservices.azure.com/openai/responses?api-version=2025-04-01-preview",
        "https://example.services.ai.azure.example/openai",
        "https://example.services.ai.azure.example/v1",
        "https://example.services.ai.azure.example?api-version=2024-10-21",
        "example.services.ai.azure.example",
    ],
    ids=["api-path", "openai-path", "base-path", "query-only", "missing-scheme"],
)
def test_unusable_azure_endpoint_shape_fails_during_startup(monkeypatch, endpoint):
    """AzureOpenAIClient appends its own path, so anything but a bare endpoint breaks.

    The resulting error surfaces inside complete(); a node that swallows it
    degrades silently while the invocation still reports status: success, so the
    adapter has to reject the shape at startup instead.
    """

    _set_required_environment(monkeypatch)
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", endpoint)

    with pytest.raises(ValueError, match="bare"):
        cli.MarketplaceGraph().provision_secrets(NullProvider())


@pytest.mark.parametrize("key", cli._REQUIRED_SECRET_KEYS)
@pytest.mark.parametrize("invalid", [" ", "bad value", "bad\tvalue", "bad\nvalue"])
def test_blank_or_whitespace_required_environment_fails_during_startup(monkeypatch, key, invalid):
    _set_required_environment(monkeypatch)
    monkeypatch.setenv(key, invalid)

    with pytest.raises(ValueError, match=key):
        cli.MarketplaceGraph().provision_secrets(NullProvider())


def test_azure_llm_resolves_fresh_credentials_for_every_call(monkeypatch):
    configs = []

    class FakeAzureOpenAIClient:
        def __init__(self, config):
            configs.append(dict(config))

        def complete(self, messages):
            return {"content": "ok", "messages": messages}

        def stream(self, messages):
            del messages
            yield "ok"

        def bind_tools(self, tools):
            del tools
            return self

    azure_module = ModuleType("shared.services.llm.azure_openai_client")
    azure_module.AzureOpenAIClient = FakeAzureOpenAIClient
    monkeypatch.setitem(sys.modules, azure_module.__name__, azure_module)

    provider = cli._AllowlistedEnvironmentProvider()
    llm = cli._AzureMarketplaceLLM()
    assert vars(llm) == {}

    with bound_secrets(provider):
        first = _set_required_environment(monkeypatch, "first")
        llm.complete([{"role": "user", "content": "first"}])
        second = _set_required_environment(monkeypatch, "second")
        llm.complete([{"role": "user", "content": "second"}])

    assert vars(llm) == {}
    assert configs == [
        {
            "api_key": first["AZURE_OPENAI_API_KEY"],
            "azure_endpoint": first["AZURE_OPENAI_ENDPOINT"],
            "azure_deployment": first["AZURE_OPENAI_DEPLOYMENT"],
        },
        {
            "api_key": second["AZURE_OPENAI_API_KEY"],
            "azure_endpoint": second["AZURE_OPENAI_ENDPOINT"],
            "azure_deployment": second["AZURE_OPENAI_DEPLOYMENT"],
        },
    ]


def test_marketplace_graph_injects_azure_and_deployment_binding_before_compile(monkeypatch):
    values = _set_required_environment(monkeypatch)
    agent = cli.MarketplaceGraph(config={"llm": object()})

    assert isinstance(agent.config["llm"], cli._AzureMarketplaceLLM)
    assert agent.config["caller_customer_map"] == {values[cli._CALLER_ID_ENV]: values[cli._CUSTOMER_ID_ENV]}
    assert set(agent.config["kb_connections"]) == {values[cli._CUSTOMER_ID_ENV]}
    agent.compile()
    assert agent._nodes["main"]._config["llm"] is agent.config["llm"]


def test_allowlisted_provider_hides_unrelated_environment(monkeypatch):
    _set_required_environment(monkeypatch)
    monkeypatch.setenv("UNRELATED_PLATFORM_SECRET", "must-not-be-visible")
    agent = cli.MarketplaceGraph()

    agent.provision_secrets(NullProvider())

    assert agent._secrets_provider.get("UNRELATED_PLATFORM_SECRET") is None


def test_main_passes_repository_config_to_marketplace_runner(monkeypatch):
    _set_required_environment(monkeypatch)
    called = {}

    def fake_runner(agent_cls, **kwargs):
        called["agent_cls"] = agent_cls
        called.update(kwargs)

    monkeypatch.setattr(cli, "run_agent_marketplace", fake_runner)
    cli.main()

    assert called["agent_cls"] is cli.MarketplaceGraph
    assert called["agent_name"] == "LOG-C2-109"
    assert called["namespace"] == "log"
    assert called["config"]["max_retry"] == 3
