"""PB-6: verify framework call order on representative template nodes."""

from __future__ import annotations

from framework.schemas.trust_level import TrustLevel

from src.nodes.assemble_node import AssembleNode
from src.nodes.extract_validate_node import ExtractValidateNode
from src.nodes.post_process_node import PostProcessNode
from typing import ClassVar
from framework.nodes.base_node import BaseNode


def _state():
    return {
        "caller_trust_level": TrustLevel.VERIFIED_EXTERNAL.value,
        "correlation_id": "pb6", "session_id": "pb6", "thread_id": "pb6", "trace_id": "pb6", "mode": "invoke",
        "hitl_allowed": False, "document_packet": [], "sku_refs": [], "unknowns": [], "retrieved_records": [],
        "document_completeness": {}, "consistency_findings": [], "similar_return_reasons": [], "broker_questions": [],
        "primary_check_result": {"document_completeness": {}, "consistency_findings": [], "unknowns": [], "similar_return_reasons": [], "broker_questions": []},
        "cited_source_ids": [], "evidence_map": [],
    }


# ── PB-6 S-1 negative boundary（2026-08-17 正本追従で移植）──────────────
# 正本 tests/proof_of_boundary/test_pb_invoke_order.py より。テンプレのノード構成に
# 依存しない常設フィクスチャで「不足 trust では execute() に到達しない」ことを証明する
# （TC-08 の要求。既存の全ノード走査テストは触っていない）。


class _PrivilegedTrustGateFixture(BaseNode):
    """Always-present privileged node used to prove the S-1 negative boundary."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def _security_gate_input(self, state):
        return state

    def execute(self, state):
        return {"status": "success"}

    def _security_gate_output(self, result):
        return result


def _trust_predecessor(required: TrustLevel) -> TrustLevel:
    """Return a lower valid trust level; fail loudly if the framework adds one."""
    predecessors = {
        TrustLevel.VERIFIED_EXTERNAL: TrustLevel.ANONYMOUS,
        TrustLevel.INTERNAL: TrustLevel.VERIFIED_EXTERNAL,
    }
    try:
        return predecessors[required]
    except KeyError as exc:
        raise AssertionError(f"no lower trust level defined for {required!r}") from exc


def test_pb6_framework_calls_security_gates_around_execute(monkeypatch):
    import framework.nodes.base_node as base

    for node in (ExtractValidateNode(), AssembleNode(), PostProcessNode()):
        order = []
        monkeypatch.setattr(base, "emit_trace_event", lambda event, payload, state, _o=order: _o.append(event))
        for method, label in (("_security_gate_input", "s2"), ("execute", "execute"), ("_security_gate_output", "s3")):
            original = getattr(type(node), method)
            def wrapped(self, arg, _original=original, _label=label, _order=order):
                _order.append(_label)
                return _original(self, arg)
            monkeypatch.setattr(type(node), method, wrapped)
        node(_state())
        assert order == ["node_start", "s2", "execute", "s3", "node_complete"]


def test_s1_denial_refuses_execution_before_execute(monkeypatch):
    """TC-08: an always-present privileged node proves the negative S-1 path."""
    import framework.nodes.base_node as base_node_module

    events: list[str] = []
    execute_calls: list[object] = []
    monkeypatch.setattr(
        base_node_module,
        "emit_trace_event",
        lambda event_type, _payload, _state: events.append(event_type),
    )
    original_execute = _PrivilegedTrustGateFixture.execute

    def spy_execute(self, state):
        execute_calls.append(state)
        return original_execute(self, state)

    monkeypatch.setattr(_PrivilegedTrustGateFixture, "execute", spy_execute)
    result = _PrivilegedTrustGateFixture()(
        {
            "caller_trust_level": _trust_predecessor(
                _PrivilegedTrustGateFixture.required_trust_level
            ).value,
            "correlation_id": "tc08-s1-denial",
        }
    )

    assert result["status"] == "error"
    assert "S-1 trust gate denied" in result["error_log"][0]
    assert events == ["s1_denied"]
    assert not execute_calls
