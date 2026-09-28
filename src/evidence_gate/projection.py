# PART: evidence-gate v0.3.2 (parts@09b50d1)
"""Typed allowlist projection for LLM-authored finding dictionaries.

The helpers in this module deliberately validate one level only.  A field's
value type is checked and, for list fields with ``item_type``, each direct list
item is checked.  Nested dictionaries and lists are not walked recursively;
their inner contracts remain the integrating template's responsibility.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from framework.errors import SecurityViolationError


_TypeDeclaration = type | tuple[type, ...]


@dataclass(frozen=True)
class FindingField:
    """Declare one allowed field in an LLM-authored finding.

    ``required`` defaults to ``False`` so templates can adopt the helper
    without making every category-specific field mandatory.  ``item_type``
    checks direct list elements only; it does not recurse into those elements.
    ``allowed_values`` implements string enums and fixed values after the type
    check has succeeded.
    """

    name: str
    expected_type: _TypeDeclaration
    required: bool = False
    item_type: _TypeDeclaration | None = None
    allowed_values: tuple[Any, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name or self.name.strip() != self.name:
            raise ValueError("finding field name must be a non-empty unpadded string")
        _declared_types(self.expected_type, label="expected_type")
        if not isinstance(self.required, bool):
            raise TypeError("finding field required must be bool")
        if self.item_type is not None:
            _declared_types(self.item_type, label="item_type")
            if _declared_types(self.expected_type, label="expected_type") != (list,):
                raise ValueError("finding field item_type requires expected_type=list")
        if not isinstance(self.allowed_values, tuple):
            raise TypeError("finding field allowed_values must be a tuple")
        for value in self.allowed_values:
            if not _matches_declared_type(value, self.expected_type):
                raise ValueError("finding field allowed_values must match expected_type")


def project_typed_finding(
    value: Any,
    fields: Iterable[FindingField],
) -> dict[str, Any]:
    """Fail closed, then project one finding to its declared fields."""

    declared = _validated_fields(fields)
    return _project_typed_finding(value, declared)


def project_typed_findings(
    values: Any,
    fields: Iterable[FindingField],
) -> list[dict[str, Any]]:
    """Fail closed, then project a list of findings to a shared contract."""

    declared = _validated_fields(fields)
    if not isinstance(values, list):
        raise SecurityViolationError(
            "S-3 finding projection: findings must be list; "
            f"got {type(values).__name__}"
        )
    return [_project_typed_finding(value, declared) for value in values]


def _project_typed_finding(
    value: Any,
    fields: tuple[FindingField, ...],
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SecurityViolationError(
            "S-3 finding projection: finding must be dict; "
            f"got {type(value).__name__}"
        )
    allowed = {field.name for field in fields}
    if set(value) - allowed:
        # Unknown key names are LLM-authored and may themselves contain
        # sensitive material.  Do not echo them in an exception or trace.
        raise SecurityViolationError("S-3 finding projection: finding contains undeclared fields")

    projected: dict[str, Any] = {}
    for field in fields:
        if field.name not in value:
            if field.required:
                raise SecurityViolationError(
                    f"S-3 finding projection: required field '{field.name}' is missing"
                )
            continue
        item = value[field.name]
        if not _matches_declared_type(item, field.expected_type):
            raise SecurityViolationError(
                f"S-3 finding projection: field '{field.name}' must be "
                f"{_type_label(field.expected_type)}; got {type(item).__name__}"
            )
        if field.item_type is not None:
            invalid_item_type = next(
                (
                    type(list_item).__name__
                    for list_item in item
                    if not _matches_declared_type(list_item, field.item_type)
                ),
                None,
            )
            if invalid_item_type is not None:
                raise SecurityViolationError(
                    f"S-3 finding projection: field '{field.name}' items must be "
                    f"{_type_label(field.item_type)}; got {invalid_item_type}"
                )
        if field.allowed_values and item not in field.allowed_values:
            raise SecurityViolationError(
                f"S-3 finding projection: field '{field.name}' is outside its declared enum"
            )
        projected[field.name] = item
    return projected


def _validated_fields(fields: Iterable[FindingField]) -> tuple[FindingField, ...]:
    try:
        declared = tuple(fields)
    except TypeError as exc:
        raise TypeError("finding fields must be iterable") from exc
    if not declared:
        raise ValueError("finding projection requires at least one field")
    if any(not isinstance(field, FindingField) for field in declared):
        raise TypeError("finding fields must contain FindingField values")
    names = [field.name for field in declared]
    if len(names) != len(set(names)):
        raise ValueError("finding projection field names must be unique")
    return declared


def _declared_types(value: _TypeDeclaration, *, label: str) -> tuple[type, ...]:
    declared = value if isinstance(value, tuple) else (value,)
    if not declared or any(not isinstance(item, type) for item in declared):
        raise TypeError(f"finding field {label} must be a type or non-empty tuple of types")
    return declared


def _matches_declared_type(value: Any, declared: _TypeDeclaration) -> bool:
    types = _declared_types(declared, label="type declaration")
    # bool is an int subclass in Python, but a JSON boolean must not satisfy an
    # integer/number contract unless bool was explicitly declared.
    if isinstance(value, bool) and bool not in types and any(item in (int, float) for item in types):
        return False
    return isinstance(value, types)


def _type_label(declared: _TypeDeclaration) -> str:
    return " | ".join(item.__name__ for item in _declared_types(declared, label="type declaration"))
