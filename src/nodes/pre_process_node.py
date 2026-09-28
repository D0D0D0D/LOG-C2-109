"""Outer S-2 packet validation and caller/customer binding."""

from __future__ import annotations

import json
from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.services.service import resolve_customer_for_caller


class PreProcessNode(FunctionNode):
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL
    _packet_keys = frozenset({"customer_id", "shipment_case_id", "document_packet", "sku_refs", "payload_ref"})

    def __init__(self, *, config: dict[str, Any] | None = None) -> None:
        super().__init__()
        self._config = dict(config or {})

    def _extra_security_gate_input(self, state: dict) -> dict:
        raw = state.get("user_input", "")
        try:
            packet = json.loads(raw) if isinstance(raw, str) else None
        except json.JSONDecodeError:
            packet = None
        caller_customer = resolve_customer_for_caller(self._config, str(state.get("caller_id", "")))
        valid = (
            isinstance(packet, dict)
            and set(packet).issubset(self._packet_keys)
            and isinstance(packet.get("customer_id"), str)
            and bool(packet.get("customer_id"))
            and isinstance(packet.get("shipment_case_id"), str)
            and bool(packet.get("shipment_case_id"))
            and isinstance(packet.get("document_packet"), list)
            and (not packet.get("payload_ref") or isinstance(packet.get("payload_ref"), str))
            and caller_customer is not None
            and packet.get("customer_id") == caller_customer
        )
        if valid:
            return state
        rejected = dict(state)
        rejected["status"] = AgentStatus.ERROR.value
        rejected["error_log"] = ["pre_process: packet/caller customer binding was rejected"]
        return rejected

    def execute(self, state: dict) -> dict:
        emit_trace_event("customs_packet_validated", {"has_caller": bool(state.get("caller_id"))}, state)
        if state.get("status") == AgentStatus.ERROR.value:
            return {}
        packet = json.loads(state["user_input"])
        return {
            "validated_input": json.dumps(packet, ensure_ascii=False, default=str, sort_keys=True),
            "customer_id": packet["customer_id"],
            "shipment_case_id": packet["shipment_case_id"],
            "payload_ref": packet.get("payload_ref", ""),
            "status": AgentStatus.SUCCESS.value,
        }
