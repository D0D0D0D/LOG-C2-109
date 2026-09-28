"""Inner Stage 0–5 workflow for the customs primary check."""

from __future__ import annotations

from langgraph.graph import END, START

from framework.graph.base_graph import BaseGraph
from framework.schemas.agent_status import AgentStatus

from src.nodes.assemble_node import AssembleNode
from src.nodes.consistency_node import ConsistencyNode
from src.nodes.extract_validate_node import ExtractValidateNode
from src.nodes.intake_node import IntakeNode
from src.nodes.recall_node import RecallNode
from src.nodes.rule_match_node import RuleMatchNode
from src.schemas.state import State


class CustomsWorkflowGraph(BaseGraph):
    """Cached inner graph; source schema annotations prevent field projection."""

    @property
    def name(self) -> str:
        return "customs_document_primary_check"

    @property
    def state_schema(self) -> type:
        return State

    def _validate_config(self) -> None:
        top_k = self.config.get("top_k", 5)
        if not isinstance(top_k, int) or isinstance(top_k, bool) or top_k < 1:
            raise ValueError("CustomsWorkflowGraph: top_k must be a positive integer")

    def register_nodes(self) -> None:
        self._nodes = {
            "intake": IntakeNode(config=self.config),
            "extract_validate": ExtractValidateNode(config=self.config),
            "rule_match": RuleMatchNode(config=self.config),
            "consistency": ConsistencyNode(config=self.config),
            "recall": RecallNode(config=self.config),
            "assemble": AssembleNode(config=self.config),
        }

    def add_edges(self) -> None:
        self._sg.add_edge(START, "intake")
        self._sg.add_edge("intake", "extract_validate")
        self._sg.add_edge("extract_validate", "rule_match")
        self._sg.add_edge("rule_match", "consistency")
        self._sg.add_edge("consistency", "recall")
        self._sg.add_edge("recall", "assemble")
        self._sg.add_edge("assemble", END)

    def route(self, state: State) -> str:
        return END if state.get("status") == AgentStatus.ERROR.value else "assemble"

    def get_output(self, state: State) -> dict:
        if state.get("status") == AgentStatus.ERROR.value:
            return {"status": AgentStatus.ERROR.value, "error_log": state.get("error_log", [])}
        output = {
            "document_completeness": state.get("document_completeness", {}),
            "consistency_findings": state.get("consistency_findings", []),
            "unknowns": state.get("unknowns", []),
            "similar_return_reasons": state.get("similar_return_reasons", []),
            "broker_questions": state.get("broker_questions", []),
        }
        return {
            "status": state.get("status", AgentStatus.SUCCESS.value),
            "mode": state.get("mode", "invoke"),
            "output": output,
            **output,
            "cited_source_ids": state.get("cited_source_ids", []),
            "retrieved_records": state.get("retrieved_records", []),
            "evidence_map": state.get("evidence_map", []),
            "requires_human_review": state.get("requires_human_review", False),
            "degradation_reason": state.get("degradation_reason", ""),
            "hitl_draft": state.get("hitl_draft"),
            "hitl_feedback": state.get("hitl_feedback"),
            "hitl_status": state.get("hitl_status"),
        }
