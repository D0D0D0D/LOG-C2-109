"""Stage 1: deterministic OCR/extracted-field validation."""

from __future__ import annotations

from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.payload_store import PayloadStoreError


class ExtractValidateNode(FunctionNode):
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, *, config: dict[str, Any] | None = None) -> None:
        super().__init__()
        self._config = dict(config or {})

    def execute(self, state: dict) -> dict:
        emit_trace_event("customs_extraction_checked", {"document_count": len(state.get("document_packet", []))}, state)
        if state.get("status") == AgentStatus.ERROR.value:
            return {}
        unknowns = list(state.get("unknowns", []))
        raw_payload: dict | None = None
        payload_ref = state.get("payload_ref")
        if payload_ref:
            try:
                raw_payload = self._consume_payload(payload_ref, state)
            except PayloadStoreError as error:
                return {"status": AgentStatus.ERROR.value, "error_log": [f"extract_validate: {error}"]}
        sku_refs: list[dict] = []
        source_skus = raw_payload.get("sku_refs", []) if raw_payload is not None else state.get("sku_refs", [])
        for item in source_skus:
            if not isinstance(item, dict):
                unknowns.append({"kind": "sku", "detail": "SKU 抽出形式を確認してください。"})
                continue
            # Numeric/enum-derived fields are safe to checkpoint; price,
            # origin, supplier and free text stay in the consumed raw payload.
            safe = {key: item[key] for key in ("sku", "quantity", "unit", "currency", "incoterm") if key in item}
            if not isinstance(safe.get("sku"), str) or not safe.get("sku"):
                unknowns.append({"kind": "sku", "detail": "SKU が未確認です。"})
            if not isinstance(safe.get("unit"), str) or not safe.get("unit").strip():
                unknowns.append({"kind": "unit", "detail": "数量単位が不明です。"})
            sku_refs.append(safe)
        for document in state.get("document_packet", []):
            if not isinstance(document, dict):
                continue
            confidence = document.get("extraction_confidence")
            if not isinstance(confidence, (int, float)) or confidence < 0.8:
                unknowns.append({"kind": "ocr_confidence", "source_id": document.get("source_id", ""), "detail": "OCR 読取結果を原本で確認してください。"})
        return {"sku_refs": sku_refs, "unknowns": unknowns, "requires_human_review": bool(unknowns) or bool(state.get("requires_human_review")), "status": AgentStatus.SUCCESS.value}

    def _consume_payload(self, ref: str, state: dict) -> dict:
        store = self._config.get("payload_store")
        if store is None:
            raise PayloadStoreError("payload_ref_unknown")
        scope = {"customer_id": str(state.get("customer_id", ""))}
        thread_id = str(state.get("thread_id", ""))
        try:
            payload = store.resolve(ref, scope=scope, session_id=thread_id, consume=True)
            if not isinstance(payload, dict):
                raise PayloadStoreError("payload_ref_unknown")
            return payload
        finally:
            # A consumed tombstone is enough for fail-closed replay, but delete
            # also bounds metadata retention immediately after Stage 1.
            store.delete(ref, scope=scope, session_id=thread_id)
