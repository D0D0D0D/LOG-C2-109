"""Stage 0: deterministic tenancy and source-record intake."""

from __future__ import annotations

import json
import re
from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.payload_store import PayloadStoreError
from src.services.service import resolve_customer_for_caller


_SAFE_ID = re.compile(r"^[0-9A-Za-z_-]+$")
_DOCUMENT_TYPES = frozenset({"invoice", "packing_list", "bill_of_lading", "certificate_of_origin", "other"})


class IntakeNode(FunctionNode):
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, *, config: dict[str, Any] | None = None) -> None:
        super().__init__()
        self._config = dict(config or {})

    def execute(self, state: dict) -> dict:
        emit_trace_event("customs_intake", {"caller_id_present": bool(state.get("caller_id"))}, state)
        if state.get("status") == AgentStatus.ERROR.value:
            return {}
        try:
            envelope = json.loads(state.get("user_input", "{}"))
            packet = envelope.get("packet", {})
        except (TypeError, json.JSONDecodeError):
            return {"status": AgentStatus.ERROR.value, "error_log": ["intake: invalid envelope"]}
        if not isinstance(packet, dict):
            return {"status": AgentStatus.ERROR.value, "error_log": ["intake: packet is not an object"]}
        customer = packet.get("customer_id")
        authorised = resolve_customer_for_caller(self._config, str(state.get("caller_id", "")))
        if not isinstance(customer, str) or customer != authorised:
            return {"status": AgentStatus.ERROR.value, "error_log": ["intake: customer binding is not authorised"]}
        documents = packet.get("document_packet")
        case_id = packet.get("shipment_case_id")
        if not isinstance(case_id, str) or not case_id or not isinstance(documents, list):
            return {"status": AgentStatus.ERROR.value, "error_log": ["intake: case/document packet is missing"]}
        payload_ref = packet.get("payload_ref", "")
        if payload_ref:
            try:
                # Validate the scoped ref before the next stage consumes it.
                # The raw value is local-only and is never returned to State.
                payload = self._resolve_payload(payload_ref, customer, state.get("thread_id", ""), consume=False)
                if not isinstance(payload, dict) or not isinstance(payload.get("document_packet"), list):
                    raise PayloadStoreError("payload_ref_unknown")
            except PayloadStoreError as error:
                return {"status": AgentStatus.ERROR.value, "error_log": [f"intake: {error}"]}
        records: list[dict] = []
        unknowns: list[dict] = []
        safe_documents: list[dict] = []
        safe_sku_refs: list[dict] = []
        for item in documents:
            if not isinstance(item, dict) or not isinstance(item.get("source_id"), str) or not _SAFE_ID.fullmatch(item["source_id"]):
                unknowns.append({"kind": "original_source", "detail": "原本 source ID を確認してください。"})
                continue
            document_type = item.get("document_type")
            document_type = document_type if isinstance(document_type, str) and document_type in _DOCUMENT_TYPES else "other"
            confidence = item.get("extraction_confidence")
            safe_documents.append({
                "source_id": item["source_id"],
                "document_type": document_type,
                "extraction_confidence": confidence if isinstance(confidence, (int, float)) and not isinstance(confidence, bool) else None,
            })
            records.append({"id": item["source_id"], "source_id": item["source_id"], "source_tier": "primary", "metadata": {"document_type": document_type}})
        for item in packet.get("sku_refs", []):
            if isinstance(item, dict):
                safe_sku_refs.append({key: item[key] for key in ("sku", "quantity", "unit", "currency", "incoterm") if key in item})
        return {
            "mode": "invoke",
            "customer_id": customer,
            "shipment_case_id": case_id,
            "payload_ref": payload_ref,
            "document_packet": safe_documents,
            "sku_refs": safe_sku_refs,
            "retrieved_records": records,
            "unknowns": unknowns,
            "requires_human_review": bool(unknowns),
            "status": AgentStatus.SUCCESS.value,
        }

    def _resolve_payload(self, ref: str, customer_id: str, thread_id: str, *, consume: bool):
        store = self._config.get("payload_store")
        if store is None:
            raise PayloadStoreError("payload_ref_unknown")
        return store.resolve(ref, scope={"customer_id": customer_id}, session_id=thread_id, consume=consume)
