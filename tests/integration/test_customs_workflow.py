"""Integration coverage for the compiled outer/inner Cat2 workflow."""

from __future__ import annotations

import json

from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from langgraph.checkpoint.memory import InMemorySaver

from src.graph.graph import CustomsDocValidatorGraph


class FakeCustomerKB:
    def search(self, query, top_k=5):
        return [{"id": "return_1", "source_id": "return_1", "source_tier": "reviewed", "metadata": {"review_point": "原本の数量と価格を確認してください。"}}]


def test_compiled_workflow_returns_five_sections_without_network():
    config = {
        "max_retry": 3, "memory_enabled": True, "hitl": {"enabled": True, "max_hitl": 8}, "top_k": 5,
        "caller_customer_map": {"broker": "customer"}, "kb_connections": {"customer": FakeCustomerKB()},
        "approved_checklists": [{"owner": "trade", "version": "1", "effective_from": "2026-01-01", "source": "approved_1", "required_documents": ["invoice"]}],
    }
    graph = CustomsDocValidatorGraph(config)
    graph.compile(checkpointer=InMemorySaver())
    packet = json.dumps({"customer_id": "customer", "shipment_case_id": "case_1", "document_packet": [{"source_id": "doc_1", "document_type": "invoice", "extraction_confidence": 1.0}]})
    result = graph.invoke(packet, ctx=InvocationContext(caller_id="broker", caller_trust_level=TrustLevel.VERIFIED_EXTERNAL, thread_id="integration", hitl_allowed=False))
    assert result["status"] == "success"
    assert {"document_completeness", "consistency_findings", "unknowns", "similar_return_reasons", "broker_questions"} <= set(result["output"])
