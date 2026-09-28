from __future__ import annotations

import json

import pytest
from framework.errors import SecurityViolationError
from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from langgraph.checkpoint.memory import InMemorySaver

from src.graph.graph import CustomsDocValidatorGraph, load_runtime_config
from src.nodes.post_process_node import PostProcessNode
from src.schemas.state import State


class FakeKB:
    def __init__(self, hits=None):
        self.hits = hits if hits is not None else []
        self.calls = 0

    def search(self, query, top_k=5):
        self.calls += 1
        return self.hits


def _config(kb=None):
    return {
        "max_retry": 3,
        "memory_enabled": True,
        "hitl": {"enabled": True, "max_hitl": 8},
        "top_k": 5,
        "caller_customer_map": {"broker_1": "customer_a"},
        "kb_connections": {"customer_a": kb or FakeKB()},
        "approved_checklists": [{
            "owner": "trade_ops", "version": "1", "effective_from": "2026-01-01", "source": "checklist_1",
            "required_documents": ["invoice", "packing_list"],
        }],
    }


def _packet(customer_id="customer_a"):
    return json.dumps({
        "customer_id": customer_id,
        "shipment_case_id": "case_1",
        "document_packet": [{"source_id": "doc_1", "document_type": "invoice", "extraction_confidence": 0.95}],
        "sku_refs": [{"sku": "sku_1", "quantity": 1, "unit": "piece"}],
    })


def _graph(config=None):
    graph = CustomsDocValidatorGraph(config or _config())
    graph.compile(checkpointer=InMemorySaver())
    return graph


def _ctx(thread_id="workflow-1", allowed=True):
    return InvocationContext(caller_id="broker_1", caller_trust_level=TrustLevel.VERIFIED_EXTERNAL, thread_id=thread_id, hitl_allowed=allowed)


def test_tc01_state_contract_has_domain_fields_and_no_credentials():
    annotations = State.__annotations__
    assert {"customer_id", "shipment_case_id", "document_packet", "primary_check_result"} <= set(annotations)
    assert not any("secret" in name or "token" in name or "credential" in name for name in annotations)


def test_tc02_s3_rejects_nested_professional_boundary_key():
    node = PostProcessNode()
    result = {
        "status": "success", "gate_kind": "customs_primary_check", "cited_source_ids": [], "retrieved_records": [], "evidence_map": [],
        "formatted_output": {"mode": "invoke", "gate_kind": "customs_primary_check", "document_completeness": {}, "consistency_findings": [], "unknowns": [], "similar_return_reasons": [{"hidden": {"hs_classification": "x"}}], "broker_questions": []},
    }
    with pytest.raises(SecurityViolationError):
        node._extra_security_gate_output(result)


def test_customer_packet_mismatch_fails_closed_without_kb_connection():
    kb = FakeKB()
    output = _graph(_config(kb)).invoke(_packet("other_customer"), ctx=_ctx())
    assert output["status"] == "error"
    assert kb.calls == 0


def test_unmapped_caller_fails_closed_without_kb_connection():
    kb = FakeKB()
    graph = _graph(_config(kb))
    output = graph.invoke(_packet(), ctx=InvocationContext(caller_id="unknown", caller_trust_level=TrustLevel.VERIFIED_EXTERNAL, thread_id="workflow-2"))
    assert output["status"] == "error"
    assert kb.calls == 0


def test_hitl_resume_reaches_success_and_preserves_five_sections():
    graph = _graph()
    pending = graph.invoke(_packet(), ctx=_ctx("workflow-resume"))
    assert pending["status"] == "awaiting_human"
    done = graph.resume("workflow-resume", {"action": "approve"})
    assert done["status"] == "success"
    assert set(done["output"]) >= {"document_completeness", "consistency_findings", "unknowns", "similar_return_reasons", "broker_questions"}


def test_hitl_allowed_false_skips_interrupt_but_keeps_review_requirement():
    done = _graph().invoke(_packet(), ctx=_ctx("workflow-no-hitl", allowed=False))
    assert done["status"] == "success"
    assert done["output"]["mode"] == "invoke"


def test_consistency_claims_have_original_source_ids_after_resume():
    graph = _graph()
    graph.invoke(_packet(), ctx=_ctx("workflow-sources"))
    done = graph.resume("workflow-sources", {"action": "approve"})
    assert all(item["source_ids"] == ["doc_1"] for item in done["output"]["consistency_findings"])


def test_hitl_corrected_resume_revalidates_before_success():
    graph = _graph()
    graph.invoke(_packet(), ctx=_ctx("workflow-corrected"))
    done = graph.resume("workflow-corrected", {"action": "correct", "comment": "確認済み"})
    assert done["status"] == "success"
    assert done["output"]["broker_questions"][-1]["question"] == "通関士訂正事項を原本と照合してください。"


def test_normal_output_has_no_professional_boundary_keys_or_external_urls():
    graph = _graph()
    graph.invoke(_packet(), ctx=_ctx("workflow-clean-output"))
    output = graph.resume("workflow-clean-output", {"action": "approve"})["output"]

    def walk(value):
        if isinstance(value, dict):
            for key, item in value.items():
                assert key not in {"hs_classification", "license_final_decision", "customs_declaration", "external_send"}
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)
        elif isinstance(value, str):
            assert "http://" not in value.casefold() and "https://" not in value.casefold()

    walk(output)


def test_production_config_declares_memory_and_hitl_gate():
    config = load_runtime_config()
    assert config["memory_enabled"] is True
    assert config["hitl"] == {"enabled": True, "max_hitl": 8}
