"""
Normalize Kimedics ``practice_value`` and Salesforce ``Job_Client_Job_Id__c`` to one comparable key.

Must match the *intent* of the manual notebooks (strip noise, lower case, punctuation → spaces)
but also fold Unicode dash/minus variants so e.g. ``2067 - X`` and ``2067- X`` and ``2067– X`` collide.
"""

from __future__ import annotations

import re
import unicodedata

# Fold Unicode dash / minus characters to ASCII hyphen before the rest of the pipeline.
_DASH_TRANSLATION = str.maketrans(
    {
        "\u2010": "-",  # hyphen
        "\u2011": "-",  # non-breaking hyphen
        "\u2012": "-",  # figure dash
        "\u2013": "-",  # en dash
        "\u2014": "-",  # em dash
        "\u2015": "-",  # horizontal bar
        "\u2212": "-",  # minus sign
        "\uFE58": "-",  # small em dash
        "\uFE63": "-",  # small hyphen-minus
        "\uFF0D": "-",  # fullwidth hyphen-minus
    }
)


def practice_key(val: str | None) -> str:
    """
    Single key for practice-only Job__c matching (1:1 on this key → take Id + worksite).

    Same rules as ``sf_job_worksite_resolve`` / ``kimedics_sf_mapping`` notebooks, plus Unicode dash folding.
    """
    s = unicodedata.normalize("NFKC", (val or "").strip().lower())
    s = s.translate(_DASH_TRANSLATION)
    s = re.sub(r"\(.*?\)", "", s)
    s = re.sub(r"\s*-\s*(closed|closing)\s*$", "", s, flags=re.I)
    s = re.sub(r"[,.\-–]", " ", s)
    return re.sub(r"\s+", " ", s).strip()
