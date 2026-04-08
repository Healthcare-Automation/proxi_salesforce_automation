"""
Expand US state abbreviations to full names for Salesforce (e.g. TX → Texas).

Parser output often keeps 2-letter codes; Job_State__c and client-facing copy use full names.
"""

from __future__ import annotations

from typing import Optional

# All 50 states + DC; values are canonical display strings for push rules.
US_STATE_CODE_TO_NAME: dict[str, str] = {
    "AL": "Alabama",
    "AK": "Alaska",
    "AZ": "Arizona",
    "AR": "Arkansas",
    "CA": "California",
    "CO": "Colorado",
    "CT": "Connecticut",
    "DE": "Delaware",
    "DC": "District of Columbia",
    "FL": "Florida",
    "GA": "Georgia",
    "HI": "Hawaii",
    "ID": "Idaho",
    "IL": "Illinois",
    "IN": "Indiana",
    "IA": "Iowa",
    "KS": "Kansas",
    "KY": "Kentucky",
    "LA": "Louisiana",
    "ME": "Maine",
    "MD": "Maryland",
    "MA": "Massachusetts",
    "MI": "Michigan",
    "MN": "Minnesota",
    "MS": "Mississippi",
    "MO": "Missouri",
    "MT": "Montana",
    "NE": "Nebraska",
    "NV": "Nevada",
    "NH": "New Hampshire",
    "NJ": "New Jersey",
    "NM": "New Mexico",
    "NY": "New York",
    "NC": "North Carolina",
    "ND": "North Dakota",
    "OH": "Ohio",
    "OK": "Oklahoma",
    "OR": "Oregon",
    "PA": "Pennsylvania",
    "RI": "Rhode Island",
    "SC": "South Carolina",
    "SD": "South Dakota",
    "TN": "Tennessee",
    "TX": "Texas",
    "UT": "Utah",
    "VT": "Vermont",
    "VA": "Virginia",
    "WA": "Washington",
    "WV": "West Virginia",
    "WI": "Wisconsin",
    "WY": "Wyoming",
}


def state_name_for_salesforce(state: Optional[str]) -> str:
    """
    Return full state name when input is a 2-letter code; otherwise return stripped text.

    If the value already looks like a full name (not exactly 2 letters), return it trimmed.
    """
    s = (state or "").strip()
    if not s:
        return ""
    if len(s) == 2 and s.isalpha():
        return US_STATE_CODE_TO_NAME.get(s.upper(), s.upper())
    return s
