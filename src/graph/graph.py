"""Outer fixed backbone for LOG-C2-109."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml
from framework.graph.agent_base_graph import AgentBaseGraph

from src.nodes.main_node import CustomsWorkflowGraphNode
from src.nodes.post_process_node import PostProcessNode
from src.nodes.pre_process_node import PreProcessNode
from src.schemas.state import State


def load_runtime_config(path: Path | None = None) -> dict[str, Any]:
    config_path = path or Path(__file__).resolve().parents[2] / "config" / "config.yaml"
    loaded = yaml.safe_load(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}
    return loaded if isinstance(loaded, dict) else {}


class CustomsDocValidatorGraph(AgentBaseGraph):
    def __init__(self, config: dict[str, Any] | None = None) -> None:
        super().__init__(config=config if config is not None else load_runtime_config())

    @property
    def name(self) -> str:
        return "log_c2_109"

    @property
    def state_schema(self) -> type:
        return State

    def register_nodes(self) -> None:
        super().register_nodes()
        self._nodes["pre_process"] = PreProcessNode(config=self.config)
        self._nodes["main"] = CustomsWorkflowGraphNode(config=self.config)
        self._nodes["post_process"] = PostProcessNode()

    def invoke(self, user_input: str, session_id: str = "", ctx=None, input_context: dict | None = None) -> dict:
        """Refuse raw bodies before BaseGraph can checkpoint them.

        The HTTP adapter replaces raw document fields with a payload_ref. This
        preflight is a second boundary for programmatic callers: malformed or
        non-allowlisted packets never reach ``BaseGraph.invoke`` and therefore
        never become an initial checkpoint.
        """

        if not _is_checkpoint_safe_packet(user_input):
            return {"output": None, "status": "error", "error_log": ["invoke: raw or malformed packet was rejected"]}
        return super().invoke(user_input, session_id=session_id, ctx=ctx, input_context=input_context)


def _is_checkpoint_safe_packet(user_input: str) -> bool:
    try:
        packet = json.loads(user_input)
    except (TypeError, json.JSONDecodeError):
        return False
    if not isinstance(packet, dict) or set(packet) - {"customer_id", "shipment_case_id", "document_packet", "sku_refs", "payload_ref"}:
        return False
    if not isinstance(packet.get("customer_id"), str) or not isinstance(packet.get("shipment_case_id"), str):
        return False
    if not isinstance(packet.get("document_packet"), list):
        return False
    if "payload_ref" in packet and not isinstance(packet["payload_ref"], str):
        return False
    document_keys = {"source_id", "document_type", "extraction_confidence"}
    sku_keys = {"sku", "quantity", "unit", "currency", "incoterm"}
    return all(isinstance(document, dict) and set(document).issubset(document_keys) for document in packet["document_packet"]) and all(
        isinstance(sku, dict) and set(sku).issubset(sku_keys) for sku in packet.get("sku_refs", [])
    )
