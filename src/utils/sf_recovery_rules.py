"""
Heuristics used by the SF push recovery engine to decide whether a bad field
value looks like a line-shift / parser contamination.

Pure predicates. Return a short string when a heuristic fires (used as the
``heuristic_fired`` value in ``sf_field_quarantined`` events) or ``None`` if
the value looks fine by that check.
"""

from __future__ import annotations

from typing import Any, Optional

# Short, strict markers that almost always indicate the value bled in from a
# free-text "Additional info" block rather than a structured field.
_NOISE_PREFIXES = (
    "***",
    "Additional requirements/",
    "Additional requirements:",
    "Additional info:",
)


def starts_with_noise_marker(value: Any) -> Optional[str]:
    s = _as_str(value).lstrip()
    if not s:
        return None
    for prefix in _NOISE_PREFIXES:
        if s.startswith(prefix):
            return f"starts_with:{prefix!r}"
    return None


def value_appears_in_sibling(
    value: Any,
    field: str,
    siblings: dict[str, Any],
) -> Optional[str]:
    """Bad value is contained (case-insensitive, ≥ 25 alnum chars) in another field.

    Strips leading non-alphanumeric noise characters (``*``, whitespace) before
    comparing so a value prefixed with ``***`` still matches when the sibling
    has the same content without the prefix.
    """
    raw = _as_str(value).strip()
    alnum_prefix = 0
    for i, ch in enumerate(raw):
        if ch.isalnum():
            alnum_prefix = i
            break
    core = raw[alnum_prefix:].strip()
    if len(core) < 25:
        return None
    needle = core.lower()
    for k, v in (siblings or {}).items():
        if k == field:
            continue
        other = _as_str(v).lower()
        if not other:
            continue
        if needle in other:
            return f"appears_in:{k}"
    return None


def exceeds_length(value: Any, max_length: Optional[int]) -> Optional[str]:
    if not max_length:
        return None
    s = _as_str(value)
    if len(s) > max_length:
        return f"exceeds_length:{max_length}"
    return None


def adjacent_empty(field: str, siblings: dict[str, Any], neighbors: tuple[str, ...]) -> Optional[str]:
    """
    True when all ``neighbors`` (same-family fields) are present-but-empty.

    Only fires when the neighbor key is explicitly present in ``siblings`` with
    an empty value. Absent keys mean "we don't know" and don't trigger — otherwise
    any row missing these keys from its attempt payload would be flagged.
    """
    if not neighbors:
        return None
    for n in neighbors:
        if n not in siblings:
            return None
        v = _as_str(siblings.get(n)).strip()
        if v:
            return None
    return f"neighbors_empty:{','.join(neighbors)}"


def _as_str(v: Any) -> str:
    if v is None:
        return ""
    return str(v)


# Short family map: if the value for this field is suspicious, these siblings
# being empty is also suspicious (line-shift signal).
FIELD_NEIGHBOR_MAP: dict[str, tuple[str, ...]] = {
    "Job_Volume__c": ("Job_Support_Staff__c", "Job_Types_of_Cases__c"),
    "Job_Ranking__c": ("External_Job_ID__c",),
}


def evaluate(
    *,
    field: str,
    value: Any,
    siblings: dict[str, Any],
    max_length: Optional[int] = None,
) -> Optional[str]:
    """
    Run all heuristics against one (field, value) pair. Return the first
    signal that fires, or ``None`` if none of them do.
    """
    for fn in (
        lambda: starts_with_noise_marker(value),
        lambda: value_appears_in_sibling(value, field, siblings),
        lambda: exceeds_length(value, max_length),
        lambda: adjacent_empty(field, siblings, FIELD_NEIGHBOR_MAP.get(field, ())),
    ):
        res = fn()
        if res:
            return res
    return None
