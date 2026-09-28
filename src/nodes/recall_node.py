"""Stage 4: customer-KB retrieval with safe projection and no default KB."""

from __future__ import annotations

from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.services.service import normalize_query, project_retrieved_record, resolve_customer_kb


class RecallNode(FunctionNode):
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, *, config: dict[str, Any] | None = None) -> None:
        super().__init__()
        self._config = dict(config or {})

    def execute(self, state: dict) -> dict:
        emit_trace_event("customs_return_reason_recalled", {"customer_bound": bool(state.get("customer_id"))}, state)
        if state.get("status") == AgentStatus.ERROR.value:
            return {}
        kb = resolve_customer_kb(self._config, str(state.get("customer_id", "")))
        if kb is None:
            return {"status": AgentStatus.ERROR.value, "error_log": ["recall: no provisioned customer KB connection"]}
        # Free-text descriptions are intentionally out-of-state. Retrieval uses
        # the fixed workflow purpose rather than reintroducing raw packet text.
        query = normalize_query("customs shipment document completeness")
        try:
            hits = kb.search(query, top_k=int(self._config.get("top_k", 5)))
        except Exception:
            return {"status": AgentStatus.ERROR.value, "error_log": ["recall: customer KB backend failed"]}
        safe = [project_retrieved_record(hit) for hit in hits if isinstance(hits, list)]
        records = [item for item in safe if item is not None]
        unknowns = list(state.get("unknowns", []))
        reason = ""
        if not records or len(records) != len(hits):
            reason = "similar_return_reason_unavailable"
            unknowns.append({"kind": "similar_return_reasons", "detail": "類似差し戻し理由の確認結果が不足しています。"})
        similar = [{"source_id": item["source_id"], "review_point": item["metadata"].get("review_point", "原本と説明資料を確認してください。"), "source_tier": item["source_tier"]} for item in records]
        result: dict[str, Any] = {"similar_return_reasons": similar, "unknowns": unknowns, "requires_human_review": bool(unknowns) or bool(state.get("requires_human_review")), "status": AgentStatus.SUCCESS.value}
        if reason:
            result["degradation_reason"] = reason
        return result
