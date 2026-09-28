"""PB-7: the actual Stage 5 D6 node propagates and honours hitl_allowed."""

from __future__ import annotations

import pytest
import pathlib
import warnings
import json

from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from langgraph.checkpoint.memory import InMemorySaver

from src.graph.graph import CustomsDocValidatorGraph
from src.nodes.assemble_node import AssembleNode


_CONFIG_PATH = pathlib.Path(__file__).parents[2] / "config" / "config.yaml"


def _hitl_enabled() -> bool:
    """Return True when config/config.yaml declares hitl.enabled: true.

    An absent or unreadable config.yaml warns instead of skipping silently —
    either case is ambiguous (never shipped / broken
    vs. genuinely non-HITL) and should not look like a clean auto-waiver.
    """
    if not _CONFIG_PATH.exists():
        warnings.warn(
            f"{_CONFIG_PATH} not found — PB-7 skipped without verifying hitl.enabled. "
            "If this template calls interrupt(), ship config/config.yaml before release.",
            stacklevel=2,
        )
        return False
    try:
        import yaml  # pyyaml==6.0.1 — pinned in scaffold/pyproject.toml dependencies

        data = yaml.safe_load(_CONFIG_PATH.read_text())
    except Exception as exc:
        warnings.warn(
            f"{_CONFIG_PATH} could not be read as YAML ({exc}) — PB-7 skipped without "
            "verifying hitl.enabled. If this template calls interrupt(), fix config/config.yaml "
            "before release.",
            stacklevel=2,
        )
        return False
    hitl = (data or {}).get("hitl", {}) if isinstance(data, dict) else None
    if not isinstance(hitl, dict):
        warnings.warn(
            f"{_CONFIG_PATH} does not have the expected 'hitl:' mapping shape — PB-7 skipped "
            "without verifying hitl.enabled. If this template calls interrupt(), fix "
            "config/config.yaml before release.",
            stacklevel=2,
        )
        return False
    return bool(hitl.get("enabled", False))


pytestmark = pytest.mark.skipif(
    not _hitl_enabled(),
    reason="config/config.yaml does not set hitl.enabled: true — PB-7 not applicable",
)



def _state(**overrides):
    state = {
        "caller_trust_level": TrustLevel.VERIFIED_EXTERNAL.value,
        "correlation_id": "pb7-correlation", "session_id": "pb7-session", "thread_id": "pb7-thread", "trace_id": "pb7-trace",
        "hitl_allowed": True,
        "document_completeness": {}, "consistency_findings": [], "unknowns": [], "similar_return_reasons": [], "broker_questions": [],
    }
    state.update(overrides)
    return state


class _KB:
    def search(self, query, top_k=5):
        return []


def _graph():
    graph = CustomsDocValidatorGraph({
        "max_retry": 3, "memory_enabled": True, "hitl": {"enabled": True, "max_hitl": 8}, "top_k": 5,
        "caller_customer_map": {"broker": "customer"}, "kb_connections": {"customer": _KB()}, "approved_checklists": [],
    })
    graph.compile(checkpointer=InMemorySaver())
    return graph


def test_pb7_hitl_interrupt_propagates():
    graph = _graph()
    packet = json.dumps({"customer_id": "customer", "shipment_case_id": "case", "document_packet": [{"source_id": "doc", "document_type": "invoice", "extraction_confidence": 1.0}]})
    result = graph.invoke(packet, ctx=InvocationContext(caller_id="broker", caller_trust_level=TrustLevel.VERIFIED_EXTERNAL, thread_id="pb7"))
    assert result["status"] == "awaiting_human"
    assert result["output"] == ""


def test_pb7_hitl_allowed_false_skips_interrupt():
    result = AssembleNode()(_state(hitl_allowed=False))
    assert result["status"] == "success"
    assert result["requires_human_review"] is True
