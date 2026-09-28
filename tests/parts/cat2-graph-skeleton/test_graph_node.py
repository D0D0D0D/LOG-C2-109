from __future__ import annotations

from typing import NotRequired, TypedDict

from langgraph.graph import END, START

from framework.graph.base_graph import BaseGraph
from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from cat2_graph_skeleton import DomainWorkflowGraphNode


class FakeCheckpointer:
    pass


class FakeSubgraph:
    def __init__(self, config):
        self.config = config
        self.compile_calls = []

    def compile(self, **kwargs):
        self.compile_calls.append(kwargs)


class MinimalWorkflowState(AgentState, total=False):
    mode: str
    output: NotRequired[dict]
    evidence_map: NotRequired[list]
    cited_source_ids: NotRequired[list]
    retrieved_records: NotRequired[list]


class EchoInnerNode(FunctionNode):
    required_trust_level = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state):
        return {
            "status": AgentStatus.SUCCESS.value,
            "mode": "invoke",
            "output": {"body": state.get("user_input", "")},
            "evidence_map": [{"source_id": "s1"}],
            "cited_source_ids": ["s1"],
            "retrieved_records": [{"source_id": "s1"}],
        }


class MinimalInnerGraph(BaseGraph):
    @property
    def name(self):
        return "minimal_inner"

    @property
    def state_schema(self):
        return MinimalWorkflowState

    def _validate_config(self):
        pass

    def register_nodes(self):
        self._nodes["echo"] = EchoInnerNode()

    def add_edges(self):
        self._sg.add_edge(START, "echo")
        self._sg.add_edge("echo", END)

    def route(self, state):
        return "echo"

    def get_output(self, state):
        return {
            "status": state.get("status"),
            "mode": state.get("mode"),
            "output": state.get("output"),
            "evidence_map": state.get("evidence_map"),
            "cited_source_ids": state.get("cited_source_ids"),
            "retrieved_records": state.get("retrieved_records"),
            "error_log": state.get("error_log", []),
        }


def _node():
    created = []

    def factory(config):
        graph = FakeSubgraph(config)
        created.append(graph)
        return graph

    node = DomainWorkflowGraphNode(
        config={"llm": object()},
        subgraph_factory=factory,
        checkpointer_factory=FakeCheckpointer,
    )
    return node, created


def test_required_trust_level_is_explicit_verified_external():
    assert DomainWorkflowGraphNode.required_trust_level == TrustLevel.VERIFIED_EXTERNAL


def test_get_subgraph_is_lazy_cached_and_compiled_once_with_inner_checkpointer():
    node, created = _node()
    first = node.get_subgraph()
    second = node.get_subgraph()

    assert first is second
    assert len(created) == 1
    assert len(first.compile_calls) == 1
    assert isinstance(first.compile_calls[0]["checkpointer"], FakeCheckpointer)
    assert first.config["memory_enabled"] is True
    assert first.config["hitl"]["enabled"] is True


def test_upstream_error_passthrough_does_not_start_subgraph():
    node, created = _node()
    assert node.execute({"status": AgentStatus.ERROR.value}) == {}
    assert created == []


def test_execute_with_real_base_graph_reaches_merge_output_and_returns_mode_scoped_delta():
    node = DomainWorkflowGraphNode(
        config={"hitl": {"enabled": False}, "memory_enabled": False},
        subgraph_factory=lambda config: MinimalInnerGraph(config),
        use_inner_checkpointer=False,
    )

    delta = node.execute(
        {
            "mode": "invoke",
            "validated_input": "draft request",
            "caller_trust_level": TrustLevel.VERIFIED_EXTERNAL.value,
            "hitl_allowed": False,
            "session_id": "s-test",
            "correlation_id": "c-test",
            "trace_id": "t-test",
            "thread_id": "th-test",
        }
    )

    assert delta["status"] == AgentStatus.SUCCESS.value
    assert delta["gate_kind"] == "draft"
    assert delta["result"] == {"body": "draft request"}
    assert delta["evidence_map"] == [{"source_id": "s1"}]
    assert "ingest_summary" not in delta


def test_extract_input_prefers_validated_input():
    node, _ = _node()
    assert node.extract_input({"validated_input": "{\"mode\":\"invoke\"}", "user_input": "raw"}) == "{\"mode\":\"invoke\"}"


def test_merge_output_declares_gate_kind_per_mode():
    node, _ = _node()

    ingest = node.merge_output(
        {"mode": "ingest"},
        {"status": AgentStatus.SUCCESS.value, "ingest_summary": {"count": 1}},
    )
    feedback = node.merge_output(
        {"mode": "feedback"},
        {"status": AgentStatus.SUCCESS.value, "feedback_summary": {"recorded": True}},
    )
    invoke = node.merge_output(
        {"mode": "invoke"},
        {
            "status": AgentStatus.SUCCESS.value,
            "output": {"body": "draft"},
            "evidence_map": [],
            "cited_source_ids": [],
            "retrieved_records": [],
        },
    )

    assert ingest["gate_kind"] == "ingest"
    assert feedback["gate_kind"] == "feedback"
    assert invoke["gate_kind"] == "draft"


def test_merge_output_returns_mode_scoped_fields_without_none_fill():
    node, _ = _node()
    delta = node.merge_output(
        {"mode": "ingest"},
        {"status": AgentStatus.SUCCESS.value, "ingest_summary": {}},
    )

    assert "feedback_summary" not in delta
    assert "result" not in delta
    assert "degradation_reason" not in delta
    assert "hitl_draft" not in delta


def test_merge_output_does_not_surface_node_history():
    node, _ = _node()
    delta = node.merge_output(
        {"mode": "invoke"},
        {
            "status": AgentStatus.SUCCESS.value,
            "output": {"body": "draft"},
            "node_history": ["inner-a", "inner-b"],
        },
    )

    assert "node_history" not in delta


def test_merge_output_propagates_hitl_fields_only_when_present():
    node, _ = _node()
    delta = node.merge_output(
        {"mode": "invoke"},
        {"status": AgentStatus.SUCCESS.value, "hitl_status": "awaiting_human", "hitl_draft": {"x": 1}},
    )

    assert delta["hitl_status"] == "awaiting_human"
    assert delta["hitl_draft"] == {"x": 1}
    assert "hitl_feedback" not in delta


def test_unknown_mode_returns_structured_error():
    node, _ = _node()
    delta = node.merge_output({"mode": "unknown"}, {"status": AgentStatus.SUCCESS.value})
    assert delta["status"] == AgentStatus.ERROR.value
    assert "unknown mode" in delta["error_log"][0]
