from __future__ import annotations

import pytest

from framework.errors import SecurityViolationError
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from output_envelope import OutputEnvelopeNode


def test_required_trust_level_is_explicit_verified_external():
    assert OutputEnvelopeNode.required_trust_level == TrustLevel.VERIFIED_EXTERNAL


def test_invoke_formats_result_and_declares_mode_and_gate_kind():
    node = OutputEnvelopeNode()
    result = node.execute({"mode": "invoke", "result": {"body": "draft"}})

    assert result["status"] == AgentStatus.SUCCESS.value
    assert result["formatted_output"]["body"] == "draft"
    assert result["formatted_output"]["mode"] == "invoke"
    assert result["formatted_output"]["gate_kind"] == "draft"


def test_ingest_formats_summary_and_declares_path():
    node = OutputEnvelopeNode()
    result = node.execute({"mode": "ingest", "ingest_summary": {"written_count": 3}})

    assert result["formatted_output"]["ingest_summary"]["written_count"] == 3
    assert result["formatted_output"]["mode"] == "ingest"
    assert result["formatted_output"]["gate_kind"] == "ingest"


def test_feedback_formats_result_and_declares_path():
    node = OutputEnvelopeNode()
    result = node.execute({"mode": "feedback", "feedback_result": {"updated": True}})

    assert result["formatted_output"]["feedback_result"]["updated"] is True
    assert result["formatted_output"]["mode"] == "feedback"
    assert result["formatted_output"]["gate_kind"] == "feedback"


def test_degradation_reason_is_folded_into_formatted_output_and_top_level():
    node = OutputEnvelopeNode()
    result = node.execute(
        {"mode": "invoke", "result": {"body": "draft"}, "degradation_reason": "llm_unavailable"}
    )

    assert result["degradation_reason"] == "llm_unavailable"
    assert result["formatted_output"]["degradation_reason"] == "llm_unavailable"


def test_node_history_is_not_added_by_envelope():
    node = OutputEnvelopeNode()
    result = node.execute({"mode": "invoke", "result": {"body": "draft"}, "node_history": ["a"]})

    assert "node_history" not in result
    assert "node_history" not in result["formatted_output"]


def test_status_never_uses_degraded_string():
    node = OutputEnvelopeNode()
    result = node.execute(
        {"mode": "invoke", "result": {"body": "draft"}, "degradation_reason": "llm_unavailable"}
    )

    assert result["status"] == AgentStatus.SUCCESS.value
    assert result["status"] != "degraded"


def test_upstream_error_passthrough_is_structured():
    node = OutputEnvelopeNode()
    result = node.execute({"mode": "invoke", "status": AgentStatus.ERROR.value, "error_log": ["bad"]})

    assert result["status"] == AgentStatus.ERROR.value
    assert result["formatted_output"]["status"] == AgentStatus.ERROR.value
    assert result["formatted_output"]["error_log"] == ["bad"]


def test_error_envelope_passes_s3_gate_through_call_path_for_ingest():
    class ErrorEnvelopeNode(OutputEnvelopeNode):
        def execute(self, state):
            return super().execute({**state, "status": AgentStatus.ERROR.value, "error_log": ["upstream"]})

    node = ErrorEnvelopeNode()
    result = node({"mode": "ingest", "caller_trust_level": TrustLevel.VERIFIED_EXTERNAL.value})

    assert result["status"] == AgentStatus.ERROR.value
    assert result["formatted_output"]["mode"] == "ingest"
    assert result["formatted_output"]["gate_kind"] == "error"
    assert result["formatted_output"]["error_log"] == ["upstream"]


def test_error_envelope_without_mode_passes_s3_gate_through_call_path():
    class ErrorEnvelopeNode(OutputEnvelopeNode):
        def execute(self, state):
            return super().execute({**state, "status": AgentStatus.ERROR.value, "error_log": ["upstream"]})

    node = ErrorEnvelopeNode()
    result = node({"caller_trust_level": TrustLevel.VERIFIED_EXTERNAL.value})

    assert result["status"] == AgentStatus.ERROR.value
    assert result["formatted_output"]["mode"] == ""
    assert result["formatted_output"]["gate_kind"] == "error"
    assert result["formatted_output"]["error_log"] == ["upstream"]


def test_gate_accepts_declared_ingest_path():
    node = OutputEnvelopeNode()
    result = {"formatted_output": {"mode": "ingest", "gate_kind": "ingest", "ingest_summary": {}}}
    assert node._extra_security_gate_output(result) == result


def test_gate_rejects_missing_formatted_output():
    node = OutputEnvelopeNode()
    with pytest.raises(SecurityViolationError, match="formatted_output"):
        node._extra_security_gate_output({})


def test_gate_rejects_missing_mode_fail_closed():
    node = OutputEnvelopeNode()
    with pytest.raises(SecurityViolationError, match="unknown or missing mode"):
        node._extra_security_gate_output({"formatted_output": {"body": "draft"}})


def test_gate_rejects_missing_required_field_for_ingest():
    node = OutputEnvelopeNode()
    with pytest.raises(SecurityViolationError, match="missing required field"):
        node._extra_security_gate_output({"formatted_output": {"mode": "ingest", "gate_kind": "ingest"}})


def test_unknown_mode_execute_succeeds_but_gate_rejects_undeclared_path():
    node = OutputEnvelopeNode()
    result = node.execute({"mode": "bogus"})

    assert result["status"] == AgentStatus.SUCCESS.value
    with pytest.raises(SecurityViolationError, match="unknown or missing mode"):
        node._extra_security_gate_output(result)


def test_deep_structure_is_truncated_by_sanitizer():
    node = OutputEnvelopeNode()
    deep = current = {}
    for i in range(10):
        current["x"] = {}
        current = current["x"]
    result = node._extra_security_gate_output(
        {"formatted_output": {"mode": "invoke", "gate_kind": "draft", "payload": deep}}
    )
    assert "truncated" in str(result["formatted_output"])


def test_long_valid_draft_body_is_not_truncated_by_default():
    node = OutputEnvelopeNode()
    body = "x" * 5000
    result = node._extra_security_gate_output(
        {"formatted_output": {"mode": "invoke", "gate_kind": "draft", "body": body}}
    )
    assert result["formatted_output"]["body"] == body


def test_gated_invoke_hook_receives_sanitized_formatted_output():
    class PersistingNode(OutputEnvelopeNode):
        persisted = None

        def _sanitize_client_echoes(self, formatted):
            return {**formatted, "body": "sanitized"}

        def on_gated_invoke_output(self, state, formatted):
            self.persisted = dict(formatted)

    node = PersistingNode()
    result = node(
        {
            "mode": "invoke",
            "result": {"body": "raw"},
            "caller_trust_level": TrustLevel.VERIFIED_EXTERNAL.value,
        }
    )

    assert result["formatted_output"]["body"] == "sanitized"
    assert node.persisted["body"] == "sanitized"
