"""Injected service helpers for the customs document workflow.

This module contains no network client construction.  Deployments inject the
already-authorised DB/KB/LLM clients into graph config; tests use small fakes.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any


_METADATA_KEY = re.compile(r"^[0-9A-Za-z_-]+$")


def normalize_query(value: str) -> str:
    """Use the same normalisation contract as KB writers and readers."""

    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def resolve_customer_for_caller(config: dict[str, Any], caller_id: str) -> str | None:
    mapping = config.get("caller_customer_map", {})
    if not caller_id or not isinstance(mapping, dict):
        return None
    customer_id = mapping.get(caller_id)
    return customer_id if isinstance(customer_id, str) and customer_id else None


def resolve_customer_kb(config: dict[str, Any], customer_id: str) -> Any | None:
    """Return only a pre-provisioned customer-specific KB, never a default."""

    connections = config.get("kb_connections", {})
    if not customer_id or not isinstance(connections, dict):
        return None
    return connections.get(customer_id)


def project_retrieved_record(record: Any) -> dict[str, Any] | None:
    """Project a KB hit into the narrow, client-safe retrieval contract."""

    if not isinstance(record, dict):
        return None
    record_id = record.get("id")
    source_id = record.get("source_id")
    tier = record.get("source_tier")
    if not all(isinstance(value, str) and value for value in (record_id, source_id, tier)):
        return None
    metadata = record.get("metadata", {})
    if not isinstance(metadata, dict):
        return None
    safe_metadata: dict[str, str] = {}
    for key in ("document_type", "reason_code", "review_point"):
        value = metadata.get(key)
        if isinstance(value, str) and len(value) <= 64 and "\n" not in value and "\r" not in value:
            safe_metadata[key] = value
    if not _METADATA_KEY.fullmatch(record_id) or not _METADATA_KEY.fullmatch(source_id):
        return None
    return {"id": record_id, "source_id": source_id, "source_tier": tier, "metadata": safe_metadata}


def wire_services(config: dict[str, Any]) -> dict[str, Any]:
    """Make explicitly injected service seams available to the graph.

    Real client creation is deployment-owned.  Keeping this function narrow
    prevents an accidental fallback to an unscoped/default KB connection.
    """

    return dict(config)
