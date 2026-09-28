"""Repo-specific inputs for the live LLM one-turn check."""

from __future__ import annotations

from typing import Any

import pytest

from src.graph.graph import CustomsDocValidatorGraph


class _LiveCustomerKB:
    """Same customer-scoped KB double as the integration happy path."""

    def search(self, query: str, top_k: int = 5) -> list[dict[str, Any]]:
        del query, top_k
        return [
            {
                "id": "return_1",
                "source_id": "return_1",
                "source_tier": "reviewed",
                "metadata": {
                    "review_point": "原本の数量と価格を確認してください。",
                },
            }
        ]


@pytest.fixture
def _live_non_resumable_parent(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mirror the happy path's ``InvocationContext(hitl_allowed=False)``.

    The generic live-check harness creates its own context with HITL enabled.
    This wrapper preserves the integration test's non-resumable parent-agent
    condition so the one-turn check can inspect the completed output.
    """

    original_invoke = CustomsDocValidatorGraph.invoke

    def invoke_without_hitl(self, user_input, *args, **kwargs):
        ctx = kwargs.get("ctx")
        if ctx is not None:
            ctx.hitl_allowed = False
        return original_invoke(self, user_input, *args, **kwargs)

    monkeypatch.setattr(CustomsDocValidatorGraph, "invoke", invoke_without_hitl)


@pytest.fixture
def agent_config(_live_non_resumable_parent: None) -> dict[str, Any]:
    """Additional construction dependencies from the integration happy path."""

    return {
        "caller_customer_map": {"live-check": "customer"},
        "kb_connections": {"customer": _LiveCustomerKB()},
        "approved_checklists": [
            {
                "owner": "trade",
                "version": "1",
                "effective_from": "2026-01-01",
                "source": "approved_1",
                "required_documents": ["invoice"],
            }
        ],
    }


@pytest.fixture
def case_payload() -> dict[str, Any]:
    """Invoke packet from tests/integration/test_customs_workflow.py:27."""

    return {
        "customer_id": "customer",
        "shipment_case_id": "case_1",
        "document_packet": [
            {
                "source_id": "doc_1",
                "document_type": "invoice",
                "extraction_confidence": 1.0,
            }
        ],
    }


@pytest.fixture
def live_input_context() -> dict[str, Any]:
    """The integration happy path supplies no additional input context."""

    return {}
