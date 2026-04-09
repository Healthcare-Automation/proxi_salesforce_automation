"""
Thin compatibility layer for older tests and tooling.

The core deterministic parser lives in ``utils.job_content_parser``.
Historically, the project referenced ``job_content_ai`` for a pipeline wrapper
that could optionally run AI validation/fixes. In this repo snapshot we keep
the pipeline API but default to heuristic-only behavior.
"""

from __future__ import annotations

from typing import Any, Optional

from utils.job_content_parser import parse_job_content_txt


def combined_types_of_cases(row: dict[str, str]) -> str:
    """
    Join required procedures + additional requirements with a newline.
    This matches how we persist the combined field in downstream tables.
    """
    rp = (row.get("required_procedures") or "").strip()
    ar = (row.get("additional_requirements") or "").strip()
    if rp and ar:
        return f"{rp}\n{ar}"
    return rp or ar


def parse_with_pipeline(
    raw_text: str,
    *,
    run_validate: bool = False,
    run_fix: bool = False,
    job_post_id: Optional[str] = None,
) -> dict[str, Any]:
    """
    Parse ``raw_text`` into a structured row, returning a pipeline-shaped result.

    The current implementation is heuristic-only. The validate/fix toggles are
    accepted for API compatibility and reflected in ``stages``.
    """
    # parse_job_content_txt currently doesn't accept job_post_id; keep parameter for API compatibility.
    _ = job_post_id
    row = parse_job_content_txt(raw_text)
    return {
        "row": row,
        "validation": None,
        "stages": {
            "heuristic": True,
            "ai_validate": bool(run_validate),
            "ai_fix": bool(run_fix),
        },
    }

