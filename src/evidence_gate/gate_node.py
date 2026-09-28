# PART: evidence-gate v0.3.2 (parts@09b50d1)
"""S-3 provenance gate for draft outputs."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, ClassVar

from framework.errors import SecurityViolationError
from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event


_SAFE_ID = re.compile(r"^[0-9A-Za-z_-]+$")


@dataclass(frozen=True)
class ProvenanceContract:
    gate_kind: str = "draft"
    cited_ids_key: str = "cited_source_ids"
    retrieved_records_key: str = "retrieved_records"
    evidence_map_key: str = "evidence_map"
    record_id_keys: tuple[str, ...] = ("source_id", "id")
    claim_source_keys: tuple[str, ...] = ("source_ids", "supporting_source_ids")
    passthrough_keys: tuple[str, ...] = ("result", "output", "draft", "degradation_reason", "requires_human_review")


class EvidenceGateNode(FunctionNode):
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, *, contract: ProvenanceContract | None = None) -> None:
        super().__init__()
        self._contract = contract or ProvenanceContract()

    def execute(self, state: dict) -> dict:
        # S-4: the node's own domain event. It records what this gate was handed
        # and narrowed down to, on every invocation -- the S-3 hook below only
        # emits `provenance_violation`, which fires solely when a check fails, so
        # without this an accepted draft left no domain trace at all.
        # Payload is structural only: the key names come from the contract this
        # node declares (never from state), and only list lengths are taken from
        # state -- no draft body, no source IDs, no LLM-authored values.
        contract = self._contract
        projected = project_provenance_state(state, contract)
        emit_trace_event(
            "provenance_gate_projected",
            {
                "gate_kind": _observed_gate_kind(state, contract),
                "projected_keys": sorted(projected),
                "cited_id_count": _list_len(projected.get(contract.cited_ids_key)),
                "retrieved_record_count": _list_len(projected.get(contract.retrieved_records_key)),
                "evidence_claim_count": _list_len(projected.get(contract.evidence_map_key)),
            },
            state,
        )
        return projected

    def _extra_security_gate_output(self, result: dict) -> dict:
        return validate_provenance(result, self._contract)


def validate_provenance(result: dict, contract: ProvenanceContract | None = None) -> dict:
    contract = contract or ProvenanceContract()
    if result.get("status") == AgentStatus.ERROR.value:
        return result
    kind = result.get("gate_kind")
    if kind in ("ingest", "feedback"):
        return result
    if kind != contract.gate_kind:
        raise SecurityViolationError("S-3 provenance gate: gate_kind is not declared as draft")

    cited = result.get(contract.cited_ids_key)
    records = result.get(contract.retrieved_records_key)
    evidence = result.get(contract.evidence_map_key)
    if cited is None or records is None or evidence is None:
        raise SecurityViolationError("S-3 provenance gate: draft output must carry evidence fields")
    if not isinstance(cited, list) or not isinstance(records, list) or not isinstance(evidence, list):
        raise SecurityViolationError("S-3 provenance gate: evidence fields must be lists")

    by_id: dict[str, dict[str, Any]] = {}
    for record in records:
        if not isinstance(record, dict):
            continue
        source_id = _record_id(record, contract)
        if source_id is None:
            continue
        _assert_safe_id(source_id)
        by_id[source_id] = record

    cited_ids = [str(sid) for sid in cited]
    for sid in cited_ids:
        _assert_safe_id(sid)
    unknown = set(cited_ids) - set(by_id)
    if unknown:
        emit_trace_event(
            "provenance_violation",
            {"unknown_ids": sorted(unknown)},
            result,
        )
        raise SecurityViolationError("LLM cited non-existent id")

    for claim in evidence:
        if not isinstance(claim, dict):
            raise SecurityViolationError(
                f"S-3 provenance gate: evidence_map entries must be dict; got {type(claim).__name__}"
            )
        for sid in _claim_source_ids(claim, contract):
            _assert_safe_id(sid, field_name="claim source id")
            if sid not in by_id:
                raise SecurityViolationError("evidence_map cites non-existent id")

        asserted_attrs: dict[Any, Any] = {}
        if "asserted_attrs" in claim:
            asserted_value = claim["asserted_attrs"]
            if not isinstance(asserted_value, dict):
                raise SecurityViolationError(
                    "S-3 provenance gate: evidence_map field 'asserted_attrs' must be dict; "
                    f"got {type(asserted_value).__name__}"
                )
            asserted_attrs = asserted_value

        for sid, asserted in asserted_attrs.items():
            if not isinstance(asserted, dict):
                raise SecurityViolationError(
                    "S-3 provenance gate: evidence_map field 'asserted_attrs' values must be dict; "
                    f"got {type(asserted).__name__}"
                )
            sid = str(sid)
            _assert_safe_id(sid, field_name="asserted_attrs source id")
            actual = by_id.get(sid)
            if actual is None:
                raise SecurityViolationError("evidence_map asserted_attrs cites non-existent id")
            mismatch_keys = sorted(str(key) for key, value in asserted.items() if actual.get(key) != value)
            if mismatch_keys:
                emit_trace_event(
                    "provenance_violation",
                    {"source_id": sid, "mismatched_keys": mismatch_keys},
                    result,
                )
                raise SecurityViolationError(
                    f"evidence_map asserted_attrs misattributes source record fields: {mismatch_keys}"
                )
    return result


def project_provenance_state(state: dict, contract: ProvenanceContract | None = None) -> dict:
    contract = contract or ProvenanceContract()
    keys = (
        "gate_kind",
        "status",
        contract.cited_ids_key,
        contract.retrieved_records_key,
        contract.evidence_map_key,
        *contract.passthrough_keys,
    )
    return {key: state[key] for key in keys if key in state}


def _observed_gate_kind(state: dict, contract: ProvenanceContract) -> str:
    """Fold the observed gate_kind into the bounded vocabulary this gate branches on.

    The value is reported because this node also runs on the ingest / feedback
    modes that ``validate_provenance()`` waives; an arbitrary state value is
    never copied into the audit record.
    """
    kind = state.get("gate_kind")
    return kind if kind in (contract.gate_kind, "ingest", "feedback") else "unknown"


def _list_len(value: Any) -> int | None:
    """Length of an evidence field, or None when it is absent or not a list.

    ``len()`` is deliberately not taken on a string: a character count would read
    as an item count in the audit record.
    """
    return len(value) if isinstance(value, list) else None


def _record_id(record: dict[str, Any], contract: ProvenanceContract) -> str | None:
    for key in contract.record_id_keys:
        if record.get(key):
            return str(record[key])
    return None


def _claim_source_ids(claim: dict[str, Any], contract: ProvenanceContract) -> list[str]:
    ids: list[str] = []
    for key in contract.claim_source_keys:
        if key not in claim:
            continue
        value = claim[key]
        if not isinstance(value, list):
            raise SecurityViolationError(
                f"S-3 provenance gate: evidence_map field '{key}' must be list[str]; "
                f"got {type(value).__name__}"
            )
        invalid_type = next((type(item).__name__ for item in value if not isinstance(item, str)), None)
        if invalid_type is not None:
            raise SecurityViolationError(
                f"S-3 provenance gate: evidence_map field '{key}' must be list[str]; "
                f"got item type {invalid_type}"
            )
        ids.extend(value)
    return ids


def _assert_safe_id(source_id: str, *, field_name: str = "source id") -> None:
    if not _SAFE_ID.fullmatch(source_id):
        raise SecurityViolationError(f"S-3 provenance gate: unsafe source id in {field_name}")
