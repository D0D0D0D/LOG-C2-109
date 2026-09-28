"""Outer output envelope plus customs S-3 professional-boundary gate."""

from __future__ import annotations

import re
import unicodedata
from typing import Any, ClassVar

from framework.errors import SecurityViolationError
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.evidence_gate import ProvenanceContract, StrictCoveragePolicy, find_uncited_numeric_or_comparative_claims, validate_provenance
from src.output_envelope import OutputEnvelopeNode


class PostProcessNode(OutputEnvelopeNode):
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL
    allowed_modes: ClassVar[tuple[str, ...]] = ("invoke",)
    gate_kind_by_mode: ClassVar[dict[str, str]] = {"invoke": "customs_primary_check"}
    required_fields_by_mode: ClassVar[dict[str, tuple[str, ...]]] = {
        "invoke": (
            "mode", "gate_kind", "document_completeness", "consistency_findings", "unknowns",
            "similar_return_reasons", "broker_questions",
        )
    }
    _provenance = ProvenanceContract(
        gate_kind="customs_primary_check",
        cited_ids_key="cited_source_ids",
        retrieved_records_key="retrieved_records",
        evidence_map_key="evidence_map",
        claim_source_keys=("source_ids",),
        passthrough_keys=(
            "document_completeness", "consistency_findings", "unknowns", "similar_return_reasons",
            "broker_questions", "degradation_reason", "requires_human_review",
        ),
    )
    _coverage = StrictCoveragePolicy(
        free_text_paths=(
            ("consistency_text",), ("unknown_text",), ("similar_text",), ("broker_text",),
        ),
        claim_text_keys=("claim_text", "claim"),
    )
    _allowed_output_keys = frozenset({
        "mode", "gate_kind", "document_completeness", "consistency_findings", "unknowns",
        "similar_return_reasons", "broker_questions", "degradation_reason", "validation_errors", "missing_data_flags",
    })
    _prohibited_key_tokens = frozenset({
        "hs_classification", "hsclassification", "license_final_decision", "licensefinaldecision",
        "customs_declaration", "customsdeclaration", "external_send", "externalsend", "payload_ref",
        "naccs", "declaration_url", "declarationurl",
    })
    _prohibited_text_tokens = (
        "hs classification", "hs分類", "hs code", "許認可", "license final decision", "customs declaration",
        "税関申告", "naccs", "http://", "https://", "external send", "外部送信",
    )

    def execute(self, state: dict) -> dict:
        emit_trace_event("customs_output_formatted", {"mode": state.get("mode", "invoke")}, state)
        result = super().execute(state)
        if result.get("status") == AgentStatus.ERROR.value:
            return result
        result.update({
            "gate_kind": "customs_primary_check",
            "cited_source_ids": state.get("cited_source_ids", []),
            "retrieved_records": state.get("retrieved_records", []),
            "evidence_map": state.get("evidence_map", []),
        })
        return result

    def format_invoke(self, state: dict) -> dict[str, Any]:
        source = state.get("primary_check_result")
        if not isinstance(source, dict):
            source = state
        return {key: source.get(key, [] if key != "document_completeness" else {}) for key in (
            "document_completeness", "consistency_findings", "unknowns", "similar_return_reasons", "broker_questions",
        )}

    def _extra_security_gate_output(self, result: dict) -> dict:
        if result.get("status") == AgentStatus.ERROR.value:
            return super()._extra_security_gate_output(result)
        validate_provenance(result, self._provenance)
        gated = super()._extra_security_gate_output(result)
        formatted = gated["formatted_output"]
        self._assert_professional_boundary(formatted)
        record_ids = {item.get("source_id") for item in result.get("retrieved_records", []) if isinstance(item, dict)}
        for finding in formatted.get("consistency_findings", []):
            source_ids = finding.get("source_ids") if isinstance(finding, dict) else None
            if not isinstance(source_ids, list) or not source_ids or any(source not in record_ids for source in source_ids):
                raise SecurityViolationError("S-3 provenance gate: each consistency finding needs existing original source IDs")
        coverage_payload = {
            "consistency_text": [item.get("claim", "") for item in formatted.get("consistency_findings", []) if isinstance(item, dict)],
            "unknown_text": [item.get("detail", "") for item in formatted.get("unknowns", []) if isinstance(item, dict)],
            "similar_text": [item.get("review_point", "") for item in formatted.get("similar_return_reasons", []) if isinstance(item, dict)],
            "broker_text": [item.get("question", "") for item in formatted.get("broker_questions", []) if isinstance(item, dict)],
        }
        flags = find_uncited_numeric_or_comparative_claims(coverage_payload, result.get("evidence_map", []), policy=self._coverage)
        if flags:
            raise SecurityViolationError("S-3 coverage gate: uncited numeric or comparative statement")
        return gated

    def _sanitize_client_echoes(self, formatted: dict[str, Any]) -> dict[str, Any]:
        self._assert_professional_boundary(formatted)
        return super()._sanitize_client_echoes(formatted)

    def _assert_professional_boundary(self, value: Any, path: str = "output") -> None:
        if isinstance(value, dict):
            nested_allowlists = {
                "output.document_completeness": {"required_documents", "present_documents", "missing_documents", "checklist_sources"},
                "output.consistency_findings[]": {"claim", "source_ids", "status"},
                "output.unknowns[]": {"kind", "detail", "source_id"},
                "output.similar_return_reasons[]": {"source_id", "review_point", "source_tier"},
                "output.broker_questions[]": {"question"},
            }
            wildcard_path = re.sub(r"\[\d+\]", "[]", path)
            permitted = nested_allowlists.get(wildcard_path)
            for key, item in value.items():
                folded = unicodedata.normalize("NFKC", str(key)).casefold().replace("-", "_")
                collapsed = folded.replace("_", "")
                if path == "output" and key not in self._allowed_output_keys:
                    raise SecurityViolationError(f"S-3 boundary: undeclared output key {key!r}")
                if permitted is not None and key not in permitted:
                    raise SecurityViolationError(f"S-3 boundary: undeclared nested output key {key!r}")
                if folded in self._prohibited_key_tokens or collapsed in self._prohibited_key_tokens or "url" in folded:
                    raise SecurityViolationError(f"S-3 boundary: prohibited professional/external key {key!r}")
                self._assert_professional_boundary(item, f"{path}.{key}")
        elif isinstance(value, list):
            for index, item in enumerate(value):
                self._assert_professional_boundary(item, f"{path}[{index}]")
        elif isinstance(value, str):
            folded = unicodedata.normalize("NFKC", value).casefold()
            if any(token in folded for token in self._prohibited_text_tokens):
                raise SecurityViolationError("S-3 boundary: prohibited professional conclusion or external payload")
