"""JSON-serialisable state for the Customs Shipment Document validator."""

from __future__ import annotations

from typing import NotRequired

from framework.schemas.agent_state import AgentState


class State(AgentState, total=False):
    """Flat state additions owned by this template.

    Credentials, connection objects, and InvocationContext deliberately do not
    appear here: those belong to injected services and framework context.
    """

    mode: str
    payload_ref: NotRequired[str]
    customer_id: str
    shipment_case_id: str
    document_packet: list[dict]
    sku_refs: NotRequired[list[dict]]
    checklist_matches: NotRequired[list[dict]]
    document_completeness: NotRequired[dict]
    consistency_findings: NotRequired[list[dict]]
    unknowns: NotRequired[list[dict]]
    similar_return_reasons: NotRequired[list[dict]]
    broker_questions: NotRequired[list[dict]]
    requires_human_review: NotRequired[bool]
    primary_check_result: NotRequired[dict]
    cited_source_ids: NotRequired[list[str]]
    retrieved_records: NotRequired[list[dict]]
    evidence_map: NotRequired[list[dict]]
    degradation_reason: NotRequired[str]
