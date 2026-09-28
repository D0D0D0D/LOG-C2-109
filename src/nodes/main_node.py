"""Cat2 main-slot adapter for the cached customs inner workflow."""

from __future__ import annotations

import json
from typing import Any, ClassVar

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.cat2_graph_skeleton import DomainWorkflowGraphNode, ModeSpec


class CustomsWorkflowGraphNode(DomainWorkflowGraphNode):
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL
    error_strategy: ClassVar[str] = "propagate"
    propagate_hitl: ClassVar[bool] = True
    mode_specs: ClassVar[dict[str, ModeSpec]] = {
        "invoke": ModeSpec(
            gate_kind="customs_primary_check",
            fields={
                "primary_check_result": "output",
                "document_completeness": "document_completeness",
                "consistency_findings": "consistency_findings",
                "unknowns": "unknowns",
                "similar_return_reasons": "similar_return_reasons",
                "broker_questions": "broker_questions",
                "cited_source_ids": "cited_source_ids",
                "retrieved_records": "retrieved_records",
                "evidence_map": "evidence_map",
                "requires_human_review": "requires_human_review",
            },
        )
    }

    def __init__(self, *, config: dict[str, Any] | None = None) -> None:
        super().__init__(config=config)

    def _build_subgraph(self):
        from src.graph.domain_workflow_graph import CustomsWorkflowGraph

        return CustomsWorkflowGraph(config=self._parent_config())

    def _parent_config(self) -> dict[str, Any]:
        parent = super()._parent_config()
        allowed = {
            "llm", "db_client", "kb_connections", "caller_customer_map", "approved_checklists", "payload_store", "top_k",
            "memory_enabled", "hitl",
        }
        return {key: value for key, value in parent.items() if key in allowed}

    def execute(self, state: dict) -> dict:
        emit_trace_event("customs_workflow_invoked", {"mode": state.get("mode", "invoke")}, state)
        if state.get("status") == AgentStatus.ERROR.value:
            return {}
        return super().execute(state)

    def extract_input(self, state: dict) -> str:
        raw = state.get("validated_input", state.get("user_input", "{}"))
        try:
            packet = json.loads(raw) if isinstance(raw, str) else raw
        except json.JSONDecodeError:
            packet = {}
        return json.dumps({"mode": "invoke", "packet": packet}, ensure_ascii=False, default=str, sort_keys=True)

    def merge_output(self, state: dict, sub_result: dict) -> dict:
        delta = super().merge_output(state, sub_result)
        if delta.get("status") != AgentStatus.ERROR.value:
            # The outer pre-process does not own execution mode; declare it
            # explicitly for the post-process S-3 envelope.
            delta["mode"] = "invoke"
        return delta


MainNode = CustomsWorkflowGraphNode
