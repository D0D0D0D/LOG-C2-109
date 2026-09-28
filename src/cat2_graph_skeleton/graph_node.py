# PART: cat2-graph-skeleton v0.1.2 (parts@cf658e9)
"""Generic Cat2 main-slot GraphNode skeleton.

This part preserves the review-hardened contracts found in the source
templates:

- the inner subgraph is cached and never rebuilt per execute/resume cycle;
- the inner checkpointer is a distinct instance from the outer graph's
  checkpointer;
- GraphNode security defaults are not trusted: required_trust_level is
  declared explicitly;
- merge_output returns only the fields produced by the mode that actually ran;
- HITL fields are propagated only when present, never None-filled;
- upstream pre_process errors are passed through without invoking the subgraph.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable, ClassVar

from framework.nodes.graph_node import GraphNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from langgraph.checkpoint.memory import InMemorySaver


SubgraphFactory = Callable[[dict[str, Any]], Any]
CheckpointerFactory = Callable[[], Any]


@dataclass(frozen=True)
class ModeSpec:
    """Mode-specific merge contract.

    `fields` maps outer-state key -> sub_result key. Only keys that are present
    in sub_result and whose values are not None are returned.
    """

    gate_kind: str
    fields: dict[str, str] = field(default_factory=dict)
    required_fields: tuple[str, ...] = ()


class DomainWorkflowGraphNode(GraphNode):
    """Main-slot GraphNode for Cat2 templates.

    Templates should subclass this or instantiate it with `subgraph_factory`.
    Keep this class thin: domain logic belongs in the inner graph/nodes.
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL
    error_strategy: ClassVar[str] = "propagate"
    propagate_hitl: ClassVar[bool] = True

    hitl_output_keys: ClassVar[tuple[str, ...]] = (
        "hitl_draft",
        "hitl_feedback",
        "hitl_status",
        "refine_action",
    )

    mode_specs: ClassVar[dict[str, ModeSpec]] = {
        "ingest": ModeSpec(
            gate_kind="ingest",
            fields={"ingest_summary": "ingest_summary", "error_log": "error_log"},
        ),
        "feedback": ModeSpec(
            gate_kind="feedback",
            fields={
                "feedback_summary": "feedback_summary",
                "feedback_result": "feedback_result",
                "error_log": "error_log",
            },
        ),
        "invoke": ModeSpec(
            gate_kind="draft",
            fields={
                "result": "output",
                "evidence_map": "evidence_map",
                "cited_source_ids": "cited_source_ids",
                "retrieved_records": "retrieved_records",
                "missing_data_flags": "missing_data_flags",
                "requires_human_review": "requires_human_review",
                "error_log": "error_log",
            },
        ),
    }

    def __init__(
        self,
        *,
        config: dict[str, Any] | None = None,
        subgraph_factory: SubgraphFactory | None = None,
        checkpointer_factory: CheckpointerFactory | None = None,
        compile_subgraph: bool = True,
        use_inner_checkpointer: bool = True,
    ) -> None:
        super().__init__()
        self._config = dict(config or {})
        self._subgraph_factory = subgraph_factory
        self._checkpointer_factory = checkpointer_factory or InMemorySaver
        self._compile_subgraph = compile_subgraph
        self._use_inner_checkpointer = use_inner_checkpointer
        self._checkpointer = None
        self._subgraph = None

    def get_subgraph(self):
        """Return the cached inner graph instance.

        A fresh inner graph per call loses suspended HITL checkpoints and can
        re-enter the interrupt forever. Do not remove the cache.
        """

        if self._subgraph is None:
            subgraph = self._build_subgraph()
            if self._compile_subgraph and hasattr(subgraph, "compile"):
                if self._use_inner_checkpointer:
                    self._checkpointer = self._checkpointer_factory()
                    subgraph.compile(checkpointer=self._checkpointer)
                else:
                    subgraph.compile()
            self._subgraph = subgraph
        return self._subgraph

    def _build_subgraph(self):
        if self._subgraph_factory is None:
            raise NotImplementedError(
                "DomainWorkflowGraphNode requires subgraph_factory or a subclass _build_subgraph()."
            )
        return self._subgraph_factory(self._parent_config())

    def _parent_config(self) -> dict[str, Any]:
        """Return config forwarded to the inner graph.

        HITL/memory are enabled by default because a Cat2 GraphNode that can
        suspend must compile its inner graph with a checkpointer-aware config.
        """

        forwarded = dict(self._config)
        forwarded.setdefault("memory_enabled", True)
        forwarded.setdefault("hitl", {"enabled": self.propagate_hitl})
        return forwarded

    def execute(self, state: AgentState) -> dict:
        if state.get("status") == AgentStatus.ERROR.value:
            return {}
        return super().execute(state)

    def extract_input(self, state: AgentState) -> str:
        if state.get("validated_input"):
            return state["validated_input"]
        if state.get("user_input"):
            return state["user_input"]
        return json.dumps(self._default_input_envelope(state), ensure_ascii=False, default=str, sort_keys=True)

    def _default_input_envelope(self, state: AgentState) -> dict[str, Any]:
        keys = ("mode", "tenant_id", "org_id", "payload_ref", "request_params")
        return {key: state.get(key) for key in keys if key in state}

    def merge_output(self, state: AgentState, sub_result: dict) -> dict:
        mode = state.get("mode") or sub_result.get("mode")
        spec = self.mode_specs.get(mode)
        if spec is None:
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": [f"DomainWorkflowGraphNode: unknown mode {mode!r}"],
            }

        delta: dict[str, Any] = {
            "gate_kind": spec.gate_kind,
            "status": sub_result.get("status", AgentStatus.SUCCESS.value),
        }

        for outer_key, inner_key in spec.fields.items():
            if inner_key in sub_result and sub_result[inner_key] is not None:
                delta[outer_key] = sub_result[inner_key]

        for field in spec.required_fields:
            if field not in delta and field in sub_result:
                delta[field] = sub_result[field]

        if sub_result.get("degradation_reason"):
            delta["degradation_reason"] = sub_result["degradation_reason"]

        for key in self.hitl_output_keys:
            if sub_result.get(key) is not None:
                delta[key] = sub_result[key]

        return delta

    def on_subgraph_error(self, state: AgentState, error: Exception) -> dict:
        return {
            "status": AgentStatus.ERROR.value,
            "error_log": [f"DomainWorkflowGraphNode: subgraph error: {error}"],
        }
