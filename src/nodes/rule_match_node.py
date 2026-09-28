"""Stage 2: approved pre-review checklist matching."""

from __future__ import annotations

from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event


class RuleMatchNode(FunctionNode):
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, *, config: dict[str, Any] | None = None) -> None:
        super().__init__()
        self._config = dict(config or {})

    def execute(self, state: dict) -> dict:
        emit_trace_event("customs_checklist_matched", {"case": state.get("shipment_case_id", "")}, state)
        if state.get("status") == AgentStatus.ERROR.value:
            return {}
        try:
            records = self._load_records(state)
        except Exception:
            return {"status": AgentStatus.ERROR.value, "error_log": ["rule_match: approved checklist lookup failed"]}
        valid: list[dict] = []
        unknowns = list(state.get("unknowns", []))
        for record in records:
            if not isinstance(record, dict) or not all(record.get(key) for key in ("owner", "version", "effective_from", "source")):
                unknowns.append({"kind": "checklist_governance", "detail": "承認済み checklist の owner/version/source を確認してください。"})
                continue
            valid.append({key: record.get(key) for key in ("owner", "version", "effective_from", "effective_to", "source", "required_documents", "required_explanations")})
        present = {str(item.get("document_type", "")) for item in state.get("document_packet", []) if isinstance(item, dict)}
        required = sorted({str(name) for record in valid for name in (record.get("required_documents") or []) if isinstance(name, str)})
        missing = [name for name in required if name not in present]
        if not valid:
            unknowns.append({"kind": "checklist", "detail": "適用可能な承認済み checklist を確認してください。"})
        completeness = {"required_documents": required, "present_documents": sorted(present), "missing_documents": missing, "checklist_sources": [item["source"] for item in valid]}
        return {"checklist_matches": valid, "document_completeness": completeness, "unknowns": unknowns, "requires_human_review": bool(unknowns or missing) or bool(state.get("requires_human_review")), "status": AgentStatus.SUCCESS.value}

    def _load_records(self, state: dict) -> list[dict]:
        configured = self._config.get("approved_checklists")
        if isinstance(configured, list):
            return configured
        db = self._config.get("db_client")
        if db is None:
            return []
        return db.fetch_many("customs_pre_review_checklist", {"customer_id": state.get("customer_id", "")})
