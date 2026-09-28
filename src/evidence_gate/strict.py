# PART: evidence-gate v0.3.2 (parts@09b50d1)
r"""Optional strict coverage helpers.

The minimal gate proves cited IDs exist. These helpers flag body claims that
look numeric/comparative but are not covered by an evidence_map claim.

Scope of detection — deliberately narrow (decided 2026-08-07):

- Detected: decimal digit characters (`\d`, so NFKC-normalized full-width digits
  count, as do other Unicode decimal scripts) and the comparative terms listed
  below. ASCII comparatives match on word boundaries; CJK ones do not, since
  `\b` does not fire between two CJK characters.
- NOT detected: kanji numerals (`六十二`), English number words (`sixteen`,
  `twenty-two`), and vague quantifiers (`a dozen`, `several`).

This is not an oversight. The set of ways to express a quantity is unbounded
and cross-lingual: adding kanji numerals still leaves `sixteen`, `a dozen`, and
`several` undetected, so growing the vocabulary here buys maintenance cost and
false positives without closing the gap. An earlier iteration did match kanji
numerals and flagged ordinary idioms (`十分`, `一部`, `第三者`) as numeric
claims, which would drown the signal.

**These helpers are a review aid, not a gate.** They never raise; they return
flags for a human (or a later stage) to look at. Missed claims are expected.

`StrictCoveragePolicy` tunes *where* to look (`free_text_paths`,
`claim_text_keys`) and the token boundary (`additional_unit_terms`). It does
**not** expose a way to swap the detector, so a template that needs to catch
lexical quantities must write its own check instead of calling this helper —
do not widen the vocabulary here.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any


_DEFAULT_UNIT_TERMS = ("%", "件", "回", "日", "hour", "hours", "day", "days")
_COMPARATIVE_TERMS = (
    "greater",
    "fewer",
    "less",
    "more",
    "増加",
    "減少",
    "以上",
    "以下",
    "未満",
    "超",
)


@dataclass(frozen=True)
class StrictCoveragePolicy:
    free_text_paths: tuple[tuple[str, ...], ...] = (
        ("output", "investigation_report"),
        ("output", "capa_plan"),
        ("investigation_draft",),
        ("draft",),
        ("report",),
    )
    claim_text_keys: tuple[str, ...] = ("claim_text", "claim")
    additional_unit_terms: tuple[str, ...] = ()


def find_uncited_numeric_or_comparative_claims(
    payload: dict[str, Any],
    evidence_map: list[dict[str, Any]],
    *,
    policy: StrictCoveragePolicy | None = None,
) -> list[dict[str, str]]:
    """Find uncovered numeric/comparative tokens in configured free text.

    This review aid intentionally detects only Arabic digits and the configured
    comparative terms. It does not guarantee quantity-claim coverage: kanji
    numerals, English number words, and other lexical quantities are omitted
    because their cross-lingual expression set is unbounded.
    """
    policy = policy or StrictCoveragePolicy()
    token_pattern = _numeric_or_comparative_pattern(policy)
    covered_tokens = {
        match.group(0)
        for claim in evidence_map
        if isinstance(claim, dict) and _claim_has_source(claim)
        for key in policy.claim_text_keys
        for match in token_pattern.finditer(_normalize_text(str(claim.get(key, ""))))
    }
    flags: list[dict[str, str]] = []
    for path in policy.free_text_paths:
        for found_path, text in _iter_text_at_path(payload, path):
            for match in token_pattern.finditer(_normalize_text(text)):
                token = match.group(0)
                if token and token not in covered_tokens:
                    flags.append({"path": found_path, "text": token, "reason": "numeric_or_comparative_without_citation"})
    return flags


def _numeric_or_comparative_pattern(policy: StrictCoveragePolicy) -> re.Pattern[str]:
    normalized_units = {
        _normalize_text(unit)
        for unit in (*_DEFAULT_UNIT_TERMS, *policy.additional_unit_terms)
        if unit
    }
    unit_alternation = "|".join(re.escape(unit) for unit in sorted(normalized_units, key=len, reverse=True))
    # Arabic digits only. Units are optional and serve as a token boundary so a
    # covered claim's "62デシベル" matches the body's "62デシベル" rather than the
    # bare "62". Kanji numerals and English number words are out of scope by
    # design — see the module docstring.
    number = rf"\d+(?:\.\d+)?\s*(?:{unit_alternation})?"
    # ASCII comparatives need a word boundary, otherwise a cited claim containing
    # "Moreover" registers "More" as covered and swallows the body's real "More".
    # CJK terms get no boundary: \b does not fire between two CJK characters.
    ascii_terms = [t for t in _COMPARATIVE_TERMS if t.isascii()]
    cjk_terms = [t for t in _COMPARATIVE_TERMS if not t.isascii()]
    alternatives = [number]
    if ascii_terms:
        alternatives.append(rf"\b(?:{'|'.join(re.escape(t) for t in ascii_terms)})\b")
    if cjk_terms:
        alternatives.append("|".join(re.escape(t) for t in cjk_terms))
    return re.compile(rf"(?:{'|'.join(alternatives)})", re.IGNORECASE)


def _normalize_text(text: str) -> str:
    return unicodedata.normalize("NFKC", text)


def _claim_has_source(claim: dict[str, Any]) -> bool:
    return bool(claim.get("source_ids") or claim.get("supporting_source_ids"))


def _iter_text_at_path(payload: Any, path: tuple[str, ...]) -> list[tuple[str, str]]:
    node = payload
    for key in path:
        if isinstance(node, dict) and key in node:
            node = node[key]
        else:
            return []
    return list(_walk_text(node, ".".join(path)))


def _walk_text(node: Any, path: str):
    if isinstance(node, str):
        yield path, node
    elif isinstance(node, dict):
        for key, value in node.items():
            yield from _walk_text(value, f"{path}.{key}")
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from _walk_text(value, f"{path}[{index}]")
