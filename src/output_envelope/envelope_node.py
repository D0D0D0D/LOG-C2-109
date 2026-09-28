# PART: output-envelope v0.1.2 (parts@cf658e9)
"""Generic post_process formatted_output envelope.

This part extracts the reusable envelope behavior from reviewed templates:

- `formatted_output` is always a dict;
- the output path is declared explicitly by `mode` and, when configured,
  `gate_kind`;
- custom fields such as `degradation_reason` are folded into
  `formatted_output` because the framework's default output projection does
  not reliably surface arbitrary state keys;
- missing path declarations or missing mode-required fields fail closed.

Domain-specific S-3 checks should be layered by subclassing the format and
sanitization hooks, not by changing the envelope contract.
"""

from __future__ import annotations

from typing import Any, ClassVar

from framework.errors import SecurityViolationError
from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event


class OutputEnvelopeNode(FunctionNode):
    """Mode-aware post_process envelope node."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    allowed_modes: ClassVar[tuple[str, ...]] = ("invoke", "ingest", "feedback")
    gate_kind_by_mode: ClassVar[dict[str, str]] = {
        "invoke": "draft",
        "ingest": "ingest",
        "feedback": "feedback",
    }
    required_fields_by_mode: ClassVar[dict[str, tuple[str, ...]]] = {
        "invoke": ("mode", "gate_kind"),
        "ingest": ("mode", "gate_kind", "ingest_summary"),
        "feedback": ("mode", "gate_kind"),
    }
    required_error_fields: ClassVar[tuple[str, ...]] = ("mode", "gate_kind", "status", "error_log")
    max_field_chars: ClassVar[int | None] = None

    def execute(self, state: dict) -> dict:
        if state.get("status") == AgentStatus.ERROR.value:
            formatted = {
                "mode": state.get("mode", ""),
                "gate_kind": "error",
                "status": AgentStatus.ERROR.value,
                "error_log": state.get("error_log", []),
            }
            return {"formatted_output": formatted, "status": AgentStatus.ERROR.value}

        mode = state.get("mode", "")
        if mode == "invoke":
            formatted = self.format_invoke(state)
        elif mode == "ingest":
            formatted = self.format_ingest(state)
        elif mode == "feedback":
            formatted = self.format_feedback(state)
        else:
            formatted = {"summary": "unknown mode"}

        if not isinstance(formatted, dict):
            raise SecurityViolationError("OutputEnvelopeNode: formatter returned non-dict formatted_output")

        formatted["mode"] = mode
        if mode in self.gate_kind_by_mode:
            formatted["gate_kind"] = self.gate_kind_by_mode[mode]

        self._fold_optional_state_fields(state, formatted)
        emit_trace_event("post_process_formatted", {"mode": mode, "gate_kind": formatted.get("gate_kind")}, state)

        result = {
            "formatted_output": formatted,
            "status": AgentStatus.SUCCESS.value,
            "_output_envelope_hook_state": state,
        }
        if state.get("degradation_reason"):
            result["degradation_reason"] = state["degradation_reason"]
        return result

    def format_invoke(self, state: dict) -> dict[str, Any]:
        value = state.get("result")
        if isinstance(value, dict):
            return dict(value)
        if value is not None:
            return {"result": value}
        draft = state.get("draft_output") or state.get("advisory_output") or {}
        return dict(draft) if isinstance(draft, dict) else {"result": draft}

    def format_ingest(self, state: dict) -> dict[str, Any]:
        return {"ingest_summary": state.get("ingest_summary", {}) or {}}

    def format_feedback(self, state: dict) -> dict[str, Any]:
        if "feedback_summary" in state:
            return {"feedback_summary": state.get("feedback_summary", {}) or {}}
        return {"feedback_result": state.get("feedback_result", {}) or {}}

    def on_gated_invoke_output(self, state: dict, formatted: dict[str, Any]) -> None:
        """Optional hook for persisting S-3-gated draft/advisory snapshots."""

    def _fold_optional_state_fields(self, state: dict, formatted: dict[str, Any]) -> None:
        for key in ("degradation_reason", "validation_errors", "missing_data_flags"):
            if state.get(key):
                formatted[key] = state[key]

    def _extra_security_gate_output(self, result: dict) -> dict:
        formatted = result.get("formatted_output")
        if not isinstance(formatted, dict):
            raise SecurityViolationError(
                "S-3 gate: formatted_output is missing or not a dict; output path cannot be verified"
            )

        if formatted.get("gate_kind") == "error":
            missing = [key for key in self.required_error_fields if key not in formatted]
            if missing:
                raise SecurityViolationError(
                    f"S-3 gate: error output is missing required field(s) {missing}; refusing fail-open output"
                )
            result["formatted_output"] = self._sanitize_client_echoes(formatted)
            result.pop("_output_envelope_hook_state", None)
            return result

        mode = formatted.get("mode")
        if mode not in self.allowed_modes:
            raise SecurityViolationError(
                f"S-3 gate: unknown or missing mode {mode!r}; refusing to emit an undeclared output path"
            )

        missing = [key for key in self.required_fields_by_mode.get(mode, ()) if key not in formatted]
        if missing:
            raise SecurityViolationError(
                f"S-3 gate: {mode} output is missing required field(s) {missing}; refusing fail-open output"
            )

        result["formatted_output"] = self._sanitize_client_echoes(formatted)
        hook_state = result.pop("_output_envelope_hook_state", None)
        if result["formatted_output"].get("mode") == "invoke":
            self.on_gated_invoke_output(hook_state or {}, result["formatted_output"])
        return result

    def _sanitize_client_echoes(self, formatted: dict[str, Any]) -> dict[str, Any]:
        """Basic structural sanitizer.

        This intentionally stays light. Domain parts should add PII,
        credential, provenance, or legal/compliance gates in subclasses.
        """

        return self._sanitize_node(formatted)

    def _sanitize_node(self, value: Any, depth: int = 0) -> Any:
        if depth >= 8 and isinstance(value, (dict, list)):
            return "[truncated: structure too deep]"
        if isinstance(value, dict):
            return {key: self._sanitize_node(item, depth + 1) for key, item in value.items()}
        if isinstance(value, list):
            return [self._sanitize_node(item, depth + 1) for item in value]
        if isinstance(value, str) and self.max_field_chars is not None and len(value) > self.max_field_chars:
            return value[: self.max_field_chars] + f"...[truncated, {len(value)} chars]"
        return value
