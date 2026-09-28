"""AGENTIC STAR Marketplace entry adapter for LOG-C2-109."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit

from framework.schemas.invocation_context import InvocationContext
from framework.secrets.base import SecretProvider
from framework.secrets.context import current_secrets

try:
    from framework.utils.config_loader import load_agent_config
except ImportError:  # AgentCore 1.0.0 / 1.0.1 compatibility
    from framework.utils.config_loader import load_config

    def load_agent_config(agent_dir: Path) -> dict[str, Any]:
        config_path = agent_dir / "config" / "config.yaml"
        return load_config(str(config_path)) if config_path.exists() else {}


try:
    from shared.bootstrap import run_agent_marketplace
except ModuleNotFoundError as exc:  # AgentCore 1.0.0 / 1.0.1 has no Marketplace extra
    if exc.name != "shared.bootstrap":
        raise
    _BOOTSTRAP_IMPORT_ERROR: ModuleNotFoundError | None = exc

    def run_agent_marketplace(*args: Any, **kwargs: Any) -> None:
        del args, kwargs
        raise RuntimeError(
            "The installed AgentCore wheel does not provide Marketplace bootstrap support"
        ) from _BOOTSTRAP_IMPORT_ERROR
else:
    _BOOTSTRAP_IMPORT_ERROR = None


try:
    from shared.secrets.env_provider import EnvProvider
except ModuleNotFoundError as exc:  # AgentCore 1.0.0 / 1.0.1 compatibility
    if exc.name != "shared.secrets.env_provider":
        raise

    class EnvProvider(SecretProvider):  # type: ignore[no-redef]
        def __init__(self, namespace: str, agent_name: str) -> None:
            self._namespace = namespace
            self._agent_name = agent_name

        def get(self, key: str, default: str | None = None) -> str | None:
            return os.environ.get(key, default)


from shared.services.llm.base_llm import BaseLLM

from src.graph.graph import CustomsDocValidatorGraph

_NAMESPACE = "log"
_AGENT_NAME = "LOG-C2-109"
_PROVIDER_AGENT_NAME = "log_c2_109"

_CALLER_ID_ENV = "STG_DEFAULT_CALLER_ID"
_CUSTOMER_ID_ENV = "STG_DEFAULT_CUSTOMER_ID"
_AZURE_OPENAI_SECRET_KEYS = (
    "AZURE_OPENAI_API_KEY",
    "AZURE_OPENAI_ENDPOINT",
    "AZURE_OPENAI_DEPLOYMENT",
)
_DEPLOYMENT_CONTEXT_KEYS = (_CALLER_ID_ENV, _CUSTOMER_ID_ENV)
_REQUIRED_SECRET_KEYS = (*_AZURE_OPENAI_SECRET_KEYS, *_DEPLOYMENT_CONTEXT_KEYS)
_ALLOWED_SECRET_KEYS = frozenset(_REQUIRED_SECRET_KEYS)
_MARKETPLACE_REJECTION_MODE = "marketplace_mode_not_available"


class _AllowlistedEnvironmentProvider(EnvProvider):  # type: ignore[misc]
    """Expose only this single-agent container's declared environment values."""

    def __init__(self) -> None:
        super().__init__(namespace=_NAMESPACE, agent_name=_PROVIDER_AGENT_NAME)

    def get(self, key: str, default: str | None = None) -> str | None:
        if key not in _ALLOWED_SECRET_KEYS:
            return default
        value = super().get(key)
        return value if value else default


def _require_clean(provider: SecretProvider, key: str) -> str:
    """Resolve one required deployment value without repairing it."""

    value = provider.require(key)
    if not value or any(character.isspace() for character in value):
        raise ValueError(f"Marketplace environment variable {key!r} must be non-empty and contain no whitespace")
    return value


def _require_azure_endpoint(provider: SecretProvider) -> str:
    """Reject ``AZURE_OPENAI_ENDPOINT`` values that are not a bare Azure resource endpoint.

    ``AzureOpenAIClient`` appends its own ``/openai/v1/`` path, so anything
    beyond a scheme and host silently produces a broken request URL, and the
    failure surfaces inside ``complete()``. A node that swallows that exception
    degrades quietly while the invocation still reports ``status: success``,
    which is why this has to be a startup check. The value is reported here,
    never repaired.
    """

    endpoint = _require_clean(provider, "AZURE_OPENAI_ENDPOINT")
    parts = urlsplit(endpoint)
    try:
        port = parts.port
    except ValueError:
        port = -1
    if (
        parts.scheme not in ("http", "https")
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or port == -1
        or parts.path.strip("/")
        or parts.query
        or parts.fragment
        or any(character.isspace() for character in endpoint)
    ):
        raise ValueError(
            "Marketplace environment variable 'AZURE_OPENAI_ENDPOINT' must be a bare Azure "
            "resource endpoint -- scheme and host only, with no path (such as "
            "'/openai'), query or fragment"
        )
    return endpoint


@dataclass(frozen=True)
class _DeploymentContext:
    caller_id: str
    customer_id: str


def _deployment_context(provider: SecretProvider) -> _DeploymentContext:
    """Build the caller/customer binding solely from deployment-owned values."""

    return _DeploymentContext(
        caller_id=_require_clean(provider, _CALLER_ID_ENV),
        customer_id=_require_clean(provider, _CUSTOMER_ID_ENV),
    )


class _MarketplaceReadOnlyCustomerKB:
    """Dedicated empty retrieval connection; Marketplace never exposes writes."""

    def search(self, query: str, top_k: int = 5) -> list[dict[str, Any]]:
        del query, top_k
        return []

    def upsert(self, documents: list[dict[str, Any]]) -> None:
        del documents
        raise RuntimeError("Marketplace customer KB writes are not available")

    def delete(self, ids: list[str]) -> None:
        del ids
        raise RuntimeError("Marketplace customer KB writes are not available")


class _AzureMarketplaceLLM(BaseLLM):  # type: ignore[misc]
    """Create an Azure client per operation and retain no connection values."""

    def __init__(self, *, tools: tuple[Any, ...] = ()) -> None:
        if tools:
            self._tools = tools

    def _new_client(self) -> BaseLLM:
        provider = current_secrets()
        values = {key: _require_clean(provider, key) for key in _AZURE_OPENAI_SECRET_KEYS}

        # Azure is an optional SDK extra and is deliberately the only import
        # performed on the invocation path.
        from shared.services.llm.azure_openai_client import AzureOpenAIClient

        client: BaseLLM = AzureOpenAIClient(
            {
                "api_key": values["AZURE_OPENAI_API_KEY"],
                "azure_endpoint": values["AZURE_OPENAI_ENDPOINT"],
                "azure_deployment": values["AZURE_OPENAI_DEPLOYMENT"],
            }
        )
        tools = getattr(self, "_tools", ())
        return client.bind_tools(list(tools)) if tools else client

    def complete(self, messages: list[Any]) -> dict[str, Any]:
        return cast(dict[str, Any], self._new_client().complete(messages))

    def stream(self, messages: list[Any]) -> Iterator[str]:
        yield from self._new_client().stream(messages)

    def bind_tools(self, tools: list[Any]) -> "_AzureMarketplaceLLM":
        return type(self)(tools=tuple(tools))


def _marketplace_user_input(user_input: str, customer_id: str) -> str:
    """Apply trusted customer scope and reject every non-read mode."""

    try:
        packet = json.loads(user_input)
    except (json.JSONDecodeError, TypeError):
        return user_input
    if not isinstance(packet, dict):
        return user_input

    mode = packet.pop("mode", "invoke")
    if mode != "invoke":
        return json.dumps({"mode": _MARKETPLACE_REJECTION_MODE})

    # customer_id is authorization scope, not caller-controlled payload data.
    packet["customer_id"] = customer_id
    return json.dumps(packet, ensure_ascii=False)


class MarketplaceGraph(CustomsDocValidatorGraph):
    """Marketplace-only scope, read posture, HITL, secret, and Azure wiring."""

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        # The runner compiles before provision_secrets(). Resolve credential-free
        # dependencies and the deployment binding before nodes capture config.
        deployment = _deployment_context(_AllowlistedEnvironmentProvider())
        marketplace_config = dict(config or {})
        configured_connections = marketplace_config.get("kb_connections")
        configured_connections = configured_connections if isinstance(configured_connections, dict) else {}
        customer_kb = configured_connections.get(deployment.customer_id)
        if customer_kb is None:
            customer_kb = _MarketplaceReadOnlyCustomerKB()
        marketplace_config.update(
            {
                "llm": _AzureMarketplaceLLM(),
                "caller_customer_map": {deployment.caller_id: deployment.customer_id},
                "kb_connections": {deployment.customer_id: customer_kb},
            }
        )
        super().__init__(config=marketplace_config)

    def invoke(
        self,
        user_input: str,
        session_id: str = "",
        ctx: InvocationContext | None = None,
        input_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        # Runner history is untrusted free text outside this AG's S-2 scan.
        # Discard every runner key and rebuild only deployment-owned scope.
        del input_context
        deployment = _deployment_context(_AllowlistedEnvironmentProvider())
        adapted_context = {"customer_id": deployment.customer_id}
        marketplace_ctx = replace(ctx, caller_id=deployment.caller_id, hitl_allowed=False) if ctx is not None else None
        return cast(
            dict[str, Any],
            super().invoke(
                _marketplace_user_input(user_input, deployment.customer_id),
                session_id=session_id,
                ctx=marketplace_ctx,
                input_context=adapted_context,
            ),
        )

    def provision_secrets(self, provider: SecretProvider) -> None:
        # Ignore the runner's broader/dotenv-capable provider boundary.
        del provider
        environment_provider = _AllowlistedEnvironmentProvider()
        for key in _REQUIRED_SECRET_KEYS:
            _require_clean(environment_provider, key)
        _deployment_context(environment_provider)
        _require_azure_endpoint(environment_provider)
        super().provision_secrets(environment_provider)


def main() -> None:
    run_agent_marketplace(
        MarketplaceGraph,
        agent_name=_AGENT_NAME,
        namespace=_NAMESPACE,
        config=load_agent_config(Path(__file__).resolve().parent),
    )


if __name__ == "__main__":
    main()
