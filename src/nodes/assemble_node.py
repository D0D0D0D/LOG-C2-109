"""Stage 5: output assembly and D6 HITL suspension."""

from __future__ import annotations

import json
from typing import Any, ClassVar

from framework.errors import SecurityViolationError
from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.hitl_status import HitlStatus
from framework.schemas.trust_level import TrustLevel
from langgraph.types import interrupt
from shared.utils.audit_logger import emit_trace_event


class AssembleNode(FunctionNode):
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, *, config: dict[str, Any] | None = None) -> None:
        super().__init__()
        self._config = dict(config or {})

    def execute(self, state: dict) -> dict:
        emit_trace_event("customs_primary_check_assembled", {"review_required": bool(state.get("requires_human_review"))}, state)
        if state.get("status") == AgentStatus.ERROR.value:
            return {}
        draft = state.get("hitl_draft")
        if not isinstance(draft, dict):
            draft = self._build_draft(state)
            llm = self._config.get("llm")
            if llm is not None:
                try:
                    candidate = llm.complete([{"role": "user", "content": "Return only the five customs primary-check sections as JSON."}])
                    parsed = json.loads(candidate.get("content", "")) if isinstance(candidate, dict) else {}
                    if isinstance(parsed, dict) and set(parsed).issubset(draft) and set(parsed) == set(draft):
                        draft = {key: parsed[key] for key in draft}
                except Exception:
                    # The deterministic, source-bounded draft remains the safe fallback.
                    pass
        if state.get("hitl_allowed", True):
            feedback = interrupt({"draft": draft, "review_type": "licensed_customs_broker", "case_id": state.get("shipment_case_id", "")})
            hitl_status = HitlStatus.from_feedback(feedback)
            safe_feedback = self._safe_feedback(hitl_status)
            if hitl_status is HitlStatus.CORRECTED:
                draft = self._apply_correction(draft, feedback)
                self._revalidate_correction(draft, state)
            if hitl_status is HitlStatus.REJECTED:
                return {"hitl_draft": draft, "hitl_feedback": safe_feedback, "hitl_status": hitl_status.value, "requires_human_review": True, "status": AgentStatus.ERROR.value, "error_log": ["broker rejected primary check"]}
            return {**draft, "hitl_draft": draft, "hitl_feedback": safe_feedback, "hitl_status": hitl_status.value, "requires_human_review": True, "status": AgentStatus.SUCCESS.value}
        return {**draft, "hitl_draft": draft, "requires_human_review": True, "status": AgentStatus.SUCCESS.value}

    @staticmethod
    def _build_draft(state: dict) -> dict[str, Any]:
        unknowns = list(state.get("unknowns", []))
        questions = list(state.get("broker_questions", []))
        if unknowns:
            questions.append({"question": "不明点と不足説明資料について通関士確認をお願いします。"})
        return {
            "document_completeness": dict(state.get("document_completeness", {})),
            "consistency_findings": list(state.get("consistency_findings", [])),
            "unknowns": unknowns,
            "similar_return_reasons": list(state.get("similar_return_reasons", [])),
            "broker_questions": questions,
        }

    @staticmethod
    def _apply_correction(draft: dict[str, Any], feedback: Any) -> dict[str, Any]:
        amended = dict(draft)
        questions = list(amended.get("broker_questions", []))
        questions.append({"question": "通関士訂正事項を原本と照合してください。"})
        amended["broker_questions"] = questions
        return amended

    @staticmethod
    def _safe_feedback(status: HitlStatus) -> dict[str, str]:
        """Keep only a status enum in checkpointed HITL feedback state."""

        return {"action": status.value}

    @staticmethod
    def _revalidate_correction(draft: dict[str, Any], state: dict) -> None:
        """Revalidate a corrected draft before it can leave the HITL boundary."""

        required = {"document_completeness", "consistency_findings", "unknowns", "similar_return_reasons", "broker_questions"}
        if set(draft) != required or not isinstance(draft["consistency_findings"], list):
            raise SecurityViolationError("corrected HITL draft violates the five-section contract")
        record_ids = {item.get("source_id") for item in state.get("retrieved_records", []) if isinstance(item, dict)}
        for finding in draft["consistency_findings"]:
            ids = finding.get("source_ids") if isinstance(finding, dict) else None
            if not isinstance(ids, list) or not ids or any(source not in record_ids for source in ids):
                raise SecurityViolationError("corrected HITL draft has an unverified source ID")
