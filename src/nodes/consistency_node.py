"""Stage 3: LLM-assisted but evidence-bounded consistency review."""

from __future__ import annotations

import json
from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event


class ConsistencyNode(FunctionNode):
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, *, config: dict[str, Any] | None = None) -> None:
        super().__init__()
        self._config = dict(config or {})

    def execute(self, state: dict) -> dict:
        emit_trace_event("customs_consistency_reviewed", {"source_count": len(state.get("retrieved_records", []))}, state)
        if state.get("status") == AgentStatus.ERROR.value:
            return {}
        allowed = {record.get("source_id") for record in state.get("retrieved_records", []) if isinstance(record, dict)}
        findings = self._fallback(allowed)
        llm = self._config.get("llm")
        degradation = ""
        if llm is not None:
            try:
                candidate = self._parse(llm.complete([{"role": "user", "content": "Compare supplied customs document fields and return JSON findings with source_ids."}]))
                if candidate:
                    findings = candidate
            except Exception:
                degradation = "consistency_llm_unavailable"
        else:
            degradation = "consistency_llm_not_configured"
        safe: list[dict] = []
        for finding in findings:
            if not isinstance(finding, dict):
                continue
            source_ids = finding.get("source_ids")
            if not isinstance(source_ids, list) or not source_ids or any(source not in allowed for source in source_ids):
                continue
            safe.append({"claim": str(finding.get("claim", "書類間の確認が必要です。")), "source_ids": source_ids, "status": str(finding.get("status", "review"))})
        evidence = [{"claim_id": f"consistency_{index}", "claim_text": item["claim"], "source_ids": item["source_ids"]} for index, item in enumerate(safe, 1)]
        result: dict[str, Any] = {"consistency_findings": safe, "cited_source_ids": sorted({source for item in safe for source in item["source_ids"]}), "evidence_map": evidence, "status": AgentStatus.SUCCESS.value}
        if degradation:
            result["degradation_reason"] = degradation
        return result

    @staticmethod
    def _fallback(allowed: set[str]) -> list[dict]:
        source = next(iter(allowed), "")
        return [{"claim": "原本書類間の品名、数量、価格、原産地および取引条件を確認してください。", "source_ids": [source], "status": "review"}] if source else []

    @staticmethod
    def _parse(response: Any) -> list[dict]:
        content = response.get("content", "") if isinstance(response, dict) else ""
        parsed = json.loads(content) if isinstance(content, str) else []
        return parsed if isinstance(parsed, list) else []
