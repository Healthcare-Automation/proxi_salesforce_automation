"""
Parse Salesforce REST error messages into a structured error class and the set of
offending field API names.

Pure functions; table-driven. Add a new row to ``_RULES`` to handle a new error
pattern. Kept deliberately small so the rule table is the full behavior spec.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal, Optional

ErrorClass = Literal[
    "too_large",
    "required_missing",
    "bad_picklist",
    "worksite_deleted",
    "transient",
    "unknown",
]


@dataclass(frozen=True)
class ClassifiedError:
    error_class: ErrorClass
    offending_fields: tuple[str, ...]
    max_length: Optional[int] = None
    raw: str = ""


def _match_too_large(text: str, label_to_api: dict[str, str]) -> Optional[ClassifiedError]:
    """
    Parse SF ``data value too large`` errors.

    The bad value between the label and the final ``(max length=N)`` can contain
    arbitrary characters including nested parens like ``(no flights/rental car)``,
    so we anchor on the trailing ``(max length=N)`` first and then look backward
    for the ``<Label>: data value too large`` marker.
    """
    tail = re.search(r"\(max length=(\d+)\)\s*$", text.strip())
    if not tail:
        return None
    max_len = int(tail.group(1))
    head = text[: tail.start()]
    lbl = re.search(
        r"([A-Za-z][A-Za-z0-9 _\-]+?):\s*data value too large",
        head,
    )
    if not lbl:
        return None
    label = lbl.group(1).strip()
    api = _resolve_label_to_api(label, label_to_api)
    return ClassifiedError(
        error_class="too_large",
        offending_fields=(api,) if api else (),
        max_length=max_len,
        raw=text,
    )


def _match_required_missing(text: str, _labels: dict[str, str]) -> Optional[ClassifiedError]:
    # Example: "Required fields are missing: [Job_Ranking__c]"
    m = re.search(r"Required fields are missing:\s*\[([^\]]+)\]", text)
    if not m:
        return None
    fields = tuple(s.strip() for s in m.group(1).split(",") if s.strip())
    return ClassifiedError(
        error_class="required_missing",
        offending_fields=fields,
        raw=text,
    )


def _match_bad_picklist(text: str, _labels: dict[str, str]) -> Optional[ClassifiedError]:
    # Example: "INVALID_OR_NULL_FOR_RESTRICTED_PICKLIST:bad value for restricted picklist field: Job_Status__c"
    if "INVALID_OR_NULL_FOR_RESTRICTED_PICKLIST" not in text:
        return None
    # Scan for the first __c API name anywhere after the marker.
    tail = text.split("INVALID_OR_NULL_FOR_RESTRICTED_PICKLIST", 1)[1]
    m = re.search(r"([A-Za-z_][A-Za-z0-9_]*__c)", tail)
    field = m.group(1) if m else ""
    return ClassifiedError(
        error_class="bad_picklist",
        offending_fields=(field,) if field else (),
        raw=text,
    )


def _match_worksite_deleted(text: str, _labels: dict[str, str]) -> Optional[ClassifiedError]:
    if "entity is deleted" in text.lower() and "Job_Worksite_Location_1__c" in text:
        return ClassifiedError(
            error_class="worksite_deleted",
            offending_fields=("Job_Worksite_Location_1__c",),
            raw=text,
        )
    return None


_TRANSIENT_SIGNALS = (
    "HTTP 500",
    "HTTP 502",
    "HTTP 503",
    "HTTP 504",
    "Name or service not known",
    "urlopen error",
    "Connection reset",
    "timed out",
    "Read timed out",
    "temporarily unavailable",
    "INVALID_SESSION_ID",
    "HTTP 401",
)


def _match_transient(text: str, _labels: dict[str, str]) -> Optional[ClassifiedError]:
    if any(sig in text for sig in _TRANSIENT_SIGNALS):
        return ClassifiedError(error_class="transient", offending_fields=(), raw=text)
    return None


_MATCHERS = (
    _match_too_large,
    _match_required_missing,
    _match_bad_picklist,
    _match_worksite_deleted,
    _match_transient,
)


def classify_sf_error(
    error_text: str,
    *,
    label_to_api: Optional[dict[str, str]] = None,
) -> ClassifiedError:
    """
    Classify a Salesforce error string into (class, offending_fields).

    ``label_to_api`` maps human labels from SF error text (e.g. "Volume") to the
    API name (e.g. "Job_Volume__c"). Usually derived from ``describe`` of the
    target sObject. Omitting it means ``too_large`` errors report an empty
    offending_fields tuple and the caller must resolve the label separately.
    """
    text = (error_text or "").strip()
    if not text:
        return ClassifiedError(error_class="unknown", offending_fields=(), raw="")
    labels = label_to_api or {}
    for fn in _MATCHERS:
        found = fn(text, labels)
        if found is not None:
            return found
    return ClassifiedError(error_class="unknown", offending_fields=(), raw=text)


def _resolve_label_to_api(label: str, label_to_api: dict[str, str]) -> Optional[str]:
    """
    Resolve a human label from an SF error ("Volume") to the API name
    ("Job_Volume__c") using a lookup built from ``describe``.

    Tolerant to small mismatches: tries exact, lowercase, and a "strip
    Job_ prefix / __c suffix" heuristic so "Job_Volume__c" in describe matches
    "Volume" in the error text.
    """
    if not label:
        return None
    if label in label_to_api:
        return label_to_api[label]
    lc = label.lower()
    for k, v in label_to_api.items():
        if k.lower() == lc:
            return v
    for k, v in label_to_api.items():
        stripped = re.sub(r"^Job_", "", re.sub(r"__c$", "", k)).replace("_", " ").lower()
        if stripped == lc:
            return v
    return None


def build_label_to_api_from_describe(describe: dict) -> dict[str, str]:
    """
    Given a Salesforce describe-sobject dict, return a mapping of label → API name.
    SF errors use the field Label; we need the API name to act on the payload.
    """
    out: dict[str, str] = {}
    fields = (describe or {}).get("fields") or []
    for f in fields:
        if not isinstance(f, dict):
            continue
        api = str(f.get("name") or "").strip()
        label = str(f.get("label") or "").strip()
        if api and label:
            out[label] = api
    return out
