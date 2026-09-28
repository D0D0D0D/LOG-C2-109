"""Short-lived scoped payload storage for keeping raw bodies out of graph state.

Scope contract (v0.3.0): the store is constructed with the template's own KB
partition key names via ``scope_keys`` (e.g. ``("production_company_id",
"project_id")``). Every ``put`` / ``resolve`` / ``delete`` call must supply a
``scope`` mapping containing exactly those keys with clean non-empty string
values, plus a ``session_id``. Anything else fails closed.

Reference format (v0.4.0): references are letter-only strings, never ``uuid4``
text -- see ``mint_reference``.
"""

from __future__ import annotations

import re
import time
from collections.abc import Mapping, Sequence as SequenceABC
from dataclasses import dataclass
from secrets import token_bytes
from typing import Any, Callable, Sequence


#: Alphabet for :func:`mint_reference` -- one letter per nibble, so a reference
#: is 32 characters drawn from ``a``..``p`` and contains no digit at all.
_NIBBLE_ALPHABET = "abcdefghijklmnop"

#: Number of random bytes behind one reference (16 bytes = 32 nibbles = 32 chars).
_REFERENCE_BYTES = 16

#: Shape of a reference minted by :func:`mint_reference`.  Exported so callers
#: that must *validate* a reference (e.g. an S-3 gate proving a value is
#: agent-minted before exempting it from a content scan) share one definition
#: instead of each hand-rolling a slightly different regex.
REFERENCE_PATTERN = re.compile(r"\A[a-p]{32}\Z")


def is_reference(value: object) -> bool:
    """Return True when ``value`` has the shape minted by :func:`mint_reference`."""

    return isinstance(value, str) and bool(REFERENCE_PATTERN.fullmatch(value))


def mint_reference() -> str:
    """Mint an opaque reference that cannot be mistaken for PII.

    The obvious choice, ``str(uuid4())``, is unsafe *as a reference* in this
    framework: its digit-and-hyphen shape collides with the ``detect_pii``
    ``phone_jp`` / ``credit_card`` / ``my_number_jp`` patterns for ~0.55% of
    generated values.  Because the S-2 input gate (``FunctionNode``'s ``@final``
    ``_security_gate_input``) *masks* what it matches, a colliding reference is
    silently rewritten to ``d1b78867-[MASKED]-0b14aba6e3cf`` inside the canonical
    envelope, and every later :meth:`PayloadStore.resolve` then fails closed with
    ``payload_ref_unknown``.  That surfaced as an unreproducible ~0.55%/envelope
    error rate in production and as intermittent CI failures.

    Encoding 16 CSPRNG bytes one nibble per letter makes the reference
    *lexically disjoint* from the digit-based PII patterns: with no digit
    present, no digit-run rule can match.  (Inside a JSON envelope the quoting
    also keeps it clear of the ``email`` / ``name`` patterns; the guarantee this
    format gives on its own is against the digit-based rules.)  This is a
    property of the reference format, not a workaround layered on the scanner --
    the scanner keeps full strength on real content.

    ``token_bytes`` rather than ``uuid4`` deliberately: uuid4 spends 6 of its
    128 bits on version/variant markers, which would both lower the entropy to
    122 bits and stamp every reference with a constant letter at two fixed
    positions.  Here all 128 bits are random.
    """

    return "".join(
        _NIBBLE_ALPHABET[nibble]
        for byte in token_bytes(_REFERENCE_BYTES)
        for nibble in (byte >> 4, byte & 0x0F)
    )


class PayloadStoreError(ValueError):
    """Raised when an opaque payload reference cannot be used safely."""


@dataclass
class _Payload:
    value: Any
    scope: dict[str, str]
    session_id: str
    expires_at: float
    used: bool = False


class PayloadStore:
    """Short-lived out-of-state payload store with scope and replay checks."""

    def __init__(
        self,
        *,
        scope_keys: Sequence[str],
        ttl_seconds: int = 300,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._scope_keys = _validate_scope_keys(scope_keys)
        self._ttl_seconds = ttl_seconds
        self._clock = clock
        self._items: dict[str, _Payload] = {}

    def _require_scope(self, scope: Mapping[str, str] | None, session_id: str) -> dict[str, str]:
        """Validate the caller-supplied scope; fail closed on any deviation."""
        if not isinstance(scope, Mapping) or set(scope) != set(self._scope_keys):
            raise PayloadStoreError("payload_ref_scope_missing")
        if any(not _valid_scope_value(scope[key]) for key in self._scope_keys):
            raise PayloadStoreError("payload_ref_scope_missing")
        if not _valid_scope_value(session_id):
            raise PayloadStoreError("payload_ref_scope_missing")
        return {key: scope[key] for key in self._scope_keys}

    def _sweep_expired(self, *, keep: str | None = None) -> None:
        """Drop expired items' raw values now, not lazily on next resolve.

        Without this, a payload that is never resolved again (e.g. the caller
        crashed before its ``finally: delete()``) keeps its raw body in memory
        until process exit even though the TTL has passed. Sweeping on every
        ``put`` / ``resolve`` bounds that residual window to one store operation.

        ``keep`` is the ref the current ``resolve`` is about to inspect: it is
        left in place so ``resolve`` can still distinguish an expired ref
        (``payload_ref_expired``) from an unknown one (``payload_ref_unknown``).
        Both are fail-closed; preserving the distinction keeps the error
        contract stable for callers that branch on it.
        """
        now = self._clock()
        expired = [ref for ref, item in self._items.items() if now >= item.expires_at and ref != keep]
        for ref in expired:
            self._items.pop(ref, None)

    def put(self, value: Any, *, scope: Mapping[str, str], session_id: str) -> str:
        bound_scope = self._require_scope(scope, session_id)
        self._sweep_expired()
        ref = mint_reference()
        self._items[ref] = _Payload(
            value=value,
            scope=bound_scope,
            session_id=session_id,
            expires_at=self._clock() + self._ttl_seconds,
        )
        return ref

    def resolve(
        self, ref: str | None, *, scope: Mapping[str, str], session_id: str, consume: bool = False
    ) -> Any:
        bound_scope = self._require_scope(scope, session_id)
        self._sweep_expired(keep=ref or "")
        item = self._items.get(ref or "")
        if item is None:
            raise PayloadStoreError("payload_ref_unknown")
        if self._clock() >= item.expires_at:
            self._items.pop(ref or "", None)
            raise PayloadStoreError("payload_ref_expired")
        if item.used:
            raise PayloadStoreError("payload_ref_replayed")
        if (item.scope, item.session_id) != (bound_scope, session_id):
            raise PayloadStoreError("payload_ref_scope_mismatch")
        value = item.value
        if consume:
            # Tombstone: keep the item so a replay still fail-closes with
            # payload_ref_replayed, but drop the raw body so a forgotten
            # delete() cannot leave the confidential value resident in memory.
            item.value = None
            item.used = True
        return value

    def delete(self, ref: str | None, *, scope: Mapping[str, str], session_id: str) -> None:
        bound_scope = self._require_scope(scope, session_id)
        item = self._items.get(ref or "")
        if item is None:
            return
        if (item.scope, item.session_id) != (bound_scope, session_id):
            raise PayloadStoreError("payload_ref_scope_mismatch")
        self._items.pop(ref or "", None)


def _validate_scope_keys(scope_keys: Sequence[str]) -> tuple[str, ...]:
    if not isinstance(scope_keys, SequenceABC) or isinstance(scope_keys, (str, bytes)):
        raise ValueError("PayloadStore: scope_keys must be a sequence of key names (tuple/list), not a bare string")
    keys = tuple(scope_keys)
    if not keys:
        raise ValueError(
            "PayloadStore: scope_keys must declare at least one key. "
            "If this domain has no partition inside one customer's KB, do not use scope-bound storage."
        )
    for key in keys:
        if not isinstance(key, str) or not key or key != key.strip() or not key.isidentifier():
            raise ValueError(f"PayloadStore: scope_keys entries must be non-empty identifier strings: {list(keys)!r}")
    if len(set(keys)) != len(keys):
        raise ValueError(f"PayloadStore: scope_keys contains duplicates: {list(keys)}")
    return keys


def _valid_scope_value(value: Any) -> bool:
    return (
        isinstance(value, str)
        and bool(value.strip())
        and value == value.strip()
        and "\n" not in value
        and "\r" not in value
    )
