"""HTTP adapter with the same authenticated caller boundary for invoke/resume."""

from __future__ import annotations

import json
import os
import secrets
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import BaseModel

from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from framework.secrets.context import bound_secrets
from src.payload_store import PayloadStore, PayloadStoreError
from shared.secrets import factory as secrets_factory

from src.graph.graph import CustomsDocValidatorGraph, load_runtime_config
from src.services.service import resolve_customer_for_caller, wire_services


app = FastAPI(title="Customs Shipment Document Completeness Validator")
runtime_config = wire_services(load_runtime_config())
# Adaptation of payload-store v0.3.0: a single customer scope axis binds a
# short-lived opaque ref to the authorised caller's customer and thread.
payload_store = PayloadStore(scope_keys=("customer_id",), ttl_seconds=300)
runtime_config["payload_store"] = payload_store
agent = CustomsDocValidatorGraph(config=runtime_config)
agent.compile(checkpointer=InMemorySaver())
agent.provision_secrets(secrets_factory(namespace="LOG", agent_name="log_c2_109"))


class InvokeRequest(BaseModel):
    input: str
    session_id: str = ""
    thread_id: str = ""


class ResumeRequest(BaseModel):
    thread_id: str
    feedback: str | int | float | bool | list | dict | None = None


def _context(request: Request, *, session_id: str = "", thread_id: str = "") -> InvocationContext:
    caller_id = getattr(request.state, "caller_id", "")
    trust = getattr(request.state, "trust_level", TrustLevel.ANONYMOUS)
    if not isinstance(trust, TrustLevel):
        trust = TrustLevel(trust)

    # Entry-point exception: a standalone/STG deployment has no upstream
    # AuthMiddleware, so an explicitly configured bearer token may establish
    # the minimum trust required by every domain node. Middleware-established
    # trust is intentionally never demoted or overwritten.
    expected = os.environ.get("INVOKE_AUTH_TOKEN")
    if expected and trust is TrustLevel.ANONYMOUS:
        supplied = request.headers.get("authorization", "")
        if not secrets.compare_digest(supplied.encode(), f"Bearer {expected}".encode()):
            raise HTTPException(status_code=401, detail="Token is invalid or expired.")
        trust = TrustLevel.VERIFIED_EXTERNAL
        caller_id = caller_id or "standalone_bearer"

    # Preserve scaffold behaviour when the optional standalone auth token is
    # not configured: framework S-1, rather than the HTTP adapter, decides
    # whether an anonymous caller may invoke a particular graph.
    return InvocationContext(
        session_id=session_id or str(uuid4()),
        thread_id=thread_id or str(uuid4()),
        caller_id=caller_id,
        caller_trust_level=trust,
    )


def _safe_document(document: object) -> dict:
    if not isinstance(document, dict):
        return {}
    document_type = str(document.get("document_type", "")).casefold()
    allowed_types = {"invoice", "packing_list", "bill_of_lading", "certificate_of_origin"}
    confidence = document.get("extraction_confidence")
    return {
        "source_id": document.get("source_id", "") if isinstance(document.get("source_id"), str) else "",
        "document_type": document_type if document_type in allowed_types else "other",
        "extraction_confidence": confidence if isinstance(confidence, (int, float)) and not isinstance(confidence, bool) else None,
    }


def _safe_rejection_packet() -> str:
    """Never pass malformed caller text into a checkpointed graph state."""

    return json.dumps({"customer_id": "", "shipment_case_id": "", "document_packet": []}, sort_keys=True)


def _prepare_invoke_input(raw_input: str, ctx: InvocationContext) -> str:
    """Store raw document bodies out-of-state and return a safe graph envelope."""

    try:
        packet = json.loads(raw_input)
    except (TypeError, json.JSONDecodeError):
        return _safe_rejection_packet()
    if not isinstance(packet, dict) or not isinstance(packet.get("customer_id"), str) or not isinstance(packet.get("shipment_case_id"), str) or not isinstance(packet.get("document_packet"), list):
        return _safe_rejection_packet()

    customer_id = packet["customer_id"]
    authorised_customer = resolve_customer_for_caller(runtime_config, ctx.caller_id)
    if not authorised_customer or customer_id != authorised_customer:
        raise HTTPException(status_code=403, detail="Caller is not authorised for this customer.")
    try:
        payload_ref = payload_store.put(
            {
                "document_packet": packet["document_packet"],
                "sku_refs": packet.get("sku_refs", []) if isinstance(packet.get("sku_refs", []), list) else [],
            },
            scope={"customer_id": authorised_customer},
            session_id=ctx.thread_id,
        )
    except PayloadStoreError:
        raise HTTPException(status_code=403, detail="Caller is not authorised for this customer.") from None
    safe_packet = {
        "customer_id": customer_id,
        "shipment_case_id": packet["shipment_case_id"],
        "document_packet": [_safe_document(document) for document in packet["document_packet"]],
        "payload_ref": payload_ref,
    }
    return json.dumps(safe_packet, ensure_ascii=False, sort_keys=True)


def _validate_resume_feedback(feedback: object) -> None:
    """Lightweight S-2-equivalent shape/size check for non-graph resume data."""

    try:
        encoded = json.dumps(feedback, ensure_ascii=False)
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail="Feedback is invalid.") from None
    action = feedback if isinstance(feedback, str) else feedback.get("action") if isinstance(feedback, dict) and set(feedback) == {"action"} else None
    if len(encoded) > 4096 or not _feedback_shape_is_safe(feedback) or not isinstance(action, str) or action.casefold() not in {"approve", "approved", "correct", "corrected", "reject", "rejected"}:
        raise HTTPException(status_code=422, detail="Feedback is invalid.")


def _feedback_shape_is_safe(value: object, depth: int = 0) -> bool:
    if depth > 5:
        return False
    if value is None or isinstance(value, (str, int, float, bool)):
        return True
    if isinstance(value, list):
        return all(_feedback_shape_is_safe(item, depth + 1) for item in value)
    if isinstance(value, dict):
        return all(isinstance(key, str) and _feedback_shape_is_safe(item, depth + 1) for key, item in value.items())
    return False


def _assert_resume_owner(thread_id: str, customer_id: str) -> None:
    compiled = agent._compiled
    if compiled is None:
        raise HTTPException(status_code=404, detail="Interrupted session was not found.")
    try:
        snapshot = compiled.get_state({"configurable": {"thread_id": thread_id}})
    except Exception:
        raise HTTPException(status_code=404, detail="Interrupted session was not found.") from None
    values = getattr(snapshot, "values", None)
    if not isinstance(values, dict) or not values:
        raise HTTPException(status_code=404, detail="Interrupted session was not found.")
    if values.get("customer_id") != customer_id:
        raise HTTPException(status_code=403, detail="Caller is not authorised for this interrupted session.")


@app.post("/invoke")
async def invoke(req: InvokeRequest, request: Request):
    ctx = _context(request, session_id=req.session_id, thread_id=req.thread_id)
    with bound_secrets(agent._secrets_provider):
        return agent.invoke(_prepare_invoke_input(req.input, ctx), ctx=ctx)


@app.post("/resume")
async def resume(req: ResumeRequest, request: Request):
    ctx = _context(request, thread_id=req.thread_id)
    customer_id = resolve_customer_for_caller(runtime_config, ctx.caller_id)
    if not customer_id:
        raise HTTPException(status_code=403, detail="Caller is not authorised for this customer.")
    _assert_resume_owner(req.thread_id, customer_id)
    _validate_resume_feedback(req.feedback)
    with bound_secrets(agent._secrets_provider):
        return agent.resume(req.thread_id, req.feedback)


@app.get("/health")
def health():
    return {"status": "ok", "agent": "log_c2_109"}
