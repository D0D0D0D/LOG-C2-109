"""PB-5: real checkpointers never persist raw customs packet sentinels."""

from __future__ import annotations

import json

from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from langgraph.checkpoint.memory import InMemorySaver
from payload_store import PayloadStore

from src.graph.graph import CustomsDocValidatorGraph


class FakeKB:
    def search(self, query, top_k=5):
        return []


def _assert_no_raw_sentinels(saver, sentinels):
    tuples = list(saver.list(None))
    assert tuples
    for checkpoint_tuple in tuples:
        persisted = json.dumps(
            {"checkpoint": checkpoint_tuple.checkpoint, "pending_writes": checkpoint_tuple.pending_writes},
            ensure_ascii=False,
            default=str,
        )
        assert all(sentinel not in persisted for sentinel in sentinels)


def test_pb5_raw_packet_never_enters_outer_or_inner_checkpoint_through_suspend_and_resume():
    store = PayloadStore(scope_keys=("customer_id",), ttl_seconds=300)
    config = {
        "max_retry": 3, "memory_enabled": True, "hitl": {"enabled": True, "max_hitl": 8}, "top_k": 5,
        "caller_customer_map": {"broker": "customer_a"}, "kb_connections": {"customer_a": FakeKB()},
        "approved_checklists": [{"owner": "trade", "version": "1", "effective_from": "2026-01-01", "source": "checklist_1", "required_documents": ["invoice"]}],
        "payload_store": store,
    }
    sentinels = ("supplier-sentinel-ACME", "unit-price-sentinel-98765", "origin-sentinel-Vietnam")
    raw_payload = {
        "document_packet": [{
            "source_id": "doc_1", "document_type": "invoice", "extraction_confidence": 0.95,
            "supplier": sentinels[0], "unit_price": sentinels[1], "origin": sentinels[2],
        }],
        "sku_refs": [{"sku": "sku_1", "quantity": 1, "unit": "piece", "description": sentinels[0], "price": sentinels[1], "country_of_origin": sentinels[2]}],
    }
    ctx = InvocationContext(caller_id="broker", caller_trust_level=TrustLevel.VERIFIED_EXTERNAL, thread_id="payload-safety")
    payload_ref = store.put(raw_payload, scope={"customer_id": "customer_a"}, session_id=ctx.thread_id)
    safe_input = json.dumps({
        "customer_id": "customer_a", "shipment_case_id": "case_1", "payload_ref": payload_ref,
        "document_packet": [{"source_id": "doc_1", "document_type": "invoice", "extraction_confidence": 0.95}],
    })
    assert all(sentinel not in safe_input for sentinel in sentinels)

    direct_saver = InMemorySaver()
    direct_graph = CustomsDocValidatorGraph(config)
    direct_graph.compile(checkpointer=direct_saver)
    raw_input = json.dumps({"customer_id": "customer_a", "shipment_case_id": "case_1", **raw_payload})
    assert direct_graph.invoke(raw_input, ctx=ctx)["status"] == "error"
    assert list(direct_saver.list(None)) == []

    outer_saver = InMemorySaver()
    graph = CustomsDocValidatorGraph(config)
    graph.compile(checkpointer=outer_saver)
    pending = graph.invoke(safe_input, ctx=ctx)
    assert pending["status"] == "awaiting_human"
    inner_saver = graph._nodes["main"]._checkpointer
    _assert_no_raw_sentinels(outer_saver, sentinels)
    _assert_no_raw_sentinels(inner_saver, sentinels)

    completed = graph.resume("payload-safety", {"action": "approve"})
    assert completed["status"] == "success"
    _assert_no_raw_sentinels(outer_saver, sentinels)
    _assert_no_raw_sentinels(inner_saver, sentinels)
