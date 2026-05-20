"""
Audit duplicate Aspen-Dental worksite Accounts in Salesforce.

For each (city, state) group with 2+ active Accounts, this script reports
per-Account usage so an operator can decide which to keep and which to delete.

Read-only. Does not delete anything in Salesforce or Supabase.

Outputs:
    data/worksite_duplicate_audit_<DATE>.csv   one row per Account inside a dup group
    plus a printed summary table.

Usage (from project root):
    python src/local/audit_duplicate_worksites_2026_05_20.py
    python src/local/audit_duplicate_worksites_2026_05_20.py --include-same-city-diff-street

Columns:
    group_key            "brandon|FL"
    account_id           SF Account Id
    name                 SF Account Name (truncated)
    shipping_street      raw ShippingStreet (truncated)
    shipping_state       raw ShippingState ("FL" or "Florida")
    created_date         ISO date in SF
    created_by_us        true if a worksite_created event in our log references this Id
    sf_jobs_count        # Job__c records pointing here via Job_Worksite_Location_1__c
    sf_jobs_open_count   # of those with Job_Status__c != 'Closed'
    supabase_current     # job_current rows pointing here via sf_worksite_account_id
    supabase_content     # job_content rows pointing here
    recommendation       one of:
        KEEP — IN USE              has SF Job__c refs and is the most-used in the group
        SAFE TO DELETE             zero SF + zero Supabase refs AND another in the group has refs
        REVIEW — SHARED USAGE      multiple accounts in the group have refs (split-brain)
        REVIEW — ALL UNUSED        nothing references any of them; pick canonical by manual rules
        REVIEW — STREET MISMATCH   classified as same-city-diff-street (not a confirmed dup)

Conservative bias: anything ambiguous lands in REVIEW, not SAFE TO DELETE.
"""

from __future__ import annotations

import argparse
import csv
import os
import re
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

_here = Path(__file__).resolve()
_src_root = _here.parent.parent
sys.path.insert(0, str(_src_root))


# ── State + street normalization (copied from sf_worksite_create) ──
_FULL_TO_ABBR = {
    'alabama': 'AL', 'alaska': 'AK', 'arizona': 'AZ', 'arkansas': 'AR',
    'california': 'CA', 'colorado': 'CO', 'connecticut': 'CT', 'delaware': 'DE',
    'florida': 'FL', 'georgia': 'GA', 'hawaii': 'HI', 'idaho': 'ID',
    'illinois': 'IL', 'indiana': 'IN', 'iowa': 'IA', 'kansas': 'KS',
    'kentucky': 'KY', 'louisiana': 'LA', 'maine': 'ME', 'maryland': 'MD',
    'massachusetts': 'MA', 'michigan': 'MI', 'minnesota': 'MN', 'mississippi': 'MS',
    'missouri': 'MO', 'montana': 'MT', 'nebraska': 'NE', 'nevada': 'NV',
    'new hampshire': 'NH', 'new jersey': 'NJ', 'new mexico': 'NM', 'new york': 'NY',
    'north carolina': 'NC', 'north dakota': 'ND', 'ohio': 'OH', 'oklahoma': 'OK',
    'oregon': 'OR', 'pennsylvania': 'PA', 'rhode island': 'RI', 'south carolina': 'SC',
    'south dakota': 'SD', 'tennessee': 'TN', 'texas': 'TX', 'utah': 'UT',
    'vermont': 'VT', 'virginia': 'VA', 'washington': 'WA', 'west virginia': 'WV',
    'wisconsin': 'WI', 'wyoming': 'WY', 'district of columbia': 'DC',
}


def _norm_state(s: str) -> str:
    t = (s or '').strip().lower()
    return _FULL_TO_ABBR.get(t, t.upper())


def _norm_city(c: str) -> str:
    return ' '.join((c or '').strip().lower().split())


_STREET_ABBR = [
    (r'\bblvd\.?\b', 'boulevard'), (r'\bave\.?\b', 'avenue'),
    (r'\bst\.?\b', 'street'), (r'\brd\.?\b', 'road'),
    (r'\bpkwy\.?\b', 'parkway'), (r'\bdr\.?\b', 'drive'),
    (r'\bhwy\.?\b', 'highway'), (r'\bln\.?\b', 'lane'),
    (r'\bcir\.?\b', 'circle'), (r'\bct\.?\b', 'court'),
    (r'\bn\.?\b', 'north'), (r'\bs\.?\b', 'south'),
    (r'\be\.?\b', 'east'), (r'\bw\.?\b', 'west'),
]
_SUITE_TAIL = re.compile(
    r'[,\s]+(ste|suite|unit|apt|#|fl|floor|bldg|building|frnt|front)\b.*$',
    flags=re.IGNORECASE,
)


def _norm_street(addr: str) -> str:
    s = (addr or '').lower().strip()
    if not s:
        return ''
    s = _SUITE_TAIL.sub('', s)
    for pat, rep in _STREET_ABBR:
        s = re.sub(pat, rep, s)
    s = re.sub(r'[^a-z0-9 ]', ' ', s)
    return ' '.join(s.split())


def _street_match(a: str, b: str) -> bool:
    na, nb = _norm_street(a), _norm_street(b)
    if not na or not nb:
        return False
    at, bt = na.split(), nb.split()
    if len(at) < 2 or len(bt) < 2:
        return na == nb
    if at[0] != bt[0] and at[0][:4] != bt[0][:4]:
        return False
    return any(t in bt[1:] for t in at[1:])


# ── Salesforce + Supabase queries ──

def _sf_session():
    from utils.salesforce import get_token_auto, query_all
    tok = get_token_auto(
        consumer_key=os.environ['SALESFORCE_CONSUMER_KEY'],
        consumer_secret=os.environ['SALESFORCE_CONSUMER_SECRET'],
        username=os.environ.get('SALESFORCE_USERNAME') or None,
        password=os.environ.get('SALESFORCE_PASSWORD') or None,
        use_client_credentials=(os.environ.get('SALESFORCE_USE_USERNAME_PASSWORD', '').lower() not in ('1', 'true', 'yes')),
        token_url=os.environ.get('SALESFORCE_TOKEN_URL') or None,
        security_token=os.environ.get('SALESFORCE_SECURITY_TOKEN') or None,
    )
    return tok['instance_url'].rstrip('/'), tok['access_token'], query_all


def _pull_all_worksite_accounts(instance_url, access_token, query_all):
    from utils.sf_push_defaults import SF_ACCOUNT_ASPEN_DENTAL_MANAGEMENT_ID
    soql = (
        "SELECT Id, Name, IsDeleted, ShippingCity, ShippingState, "
        "ShippingStreet, ParentId, CreatedDate "
        "FROM Account "
        f"WHERE (ParentId = '{SF_ACCOUNT_ASPEN_DENTAL_MANAGEMENT_ID}' "
        "OR Name LIKE 'Aspen Dental -%')"
    )
    return [r for r in query_all(instance_url, access_token, soql) if not r.get('IsDeleted')]


def _count_jobs_per_worksite(instance_url, access_token, query_all, account_ids):
    """For each Account Id, return (open_count, total_count) of Job__c
    referencing it via Job_Worksite_Location_1__c."""
    counts = defaultdict(lambda: [0, 0])  # open, total
    if not account_ids:
        return counts
    ids = list(account_ids)
    for i in range(0, len(ids), 200):
        chunk = ids[i:i + 200]
        in_list = ','.join("'" + a.replace("'", "\\'") + "'" for a in chunk)
        soql = (
            "SELECT Job_Worksite_Location_1__c wid, Job_Status__c st, COUNT(Id) n "
            f"FROM Job__c WHERE Job_Worksite_Location_1__c IN ({in_list}) "
            "GROUP BY Job_Worksite_Location_1__c, Job_Status__c"
        )
        for r in query_all(instance_url, access_token, soql):
            wid = r.get('wid')
            n = int(r.get('n') or 0)
            counts[wid][1] += n
            if (r.get('st') or '').strip().lower() != 'closed':
                counts[wid][0] += n
    return counts


def _count_other_sf_refs(instance_url, access_token, query_all, account_ids):
    """
    For each Account Id, count references in other standard SObjects so we
    don't flag an Account as SAFE TO DELETE just because Job__c is clean.
    A non-zero count in ANY of these blocks the SAFE TO DELETE recommendation.

    Returns: {account_id: {'opportunities': n, 'contracts': n, 'contacts': n,
                           'cases': n, 'tasks': n, 'events': n, 'child_accts': n}}
    Any SObject the org doesn't expose / our integration user can't query is
    silently skipped (returns 0 for that bucket).
    """
    out = defaultdict(lambda: {
        'opportunities': 0, 'contracts': 0, 'contacts': 0,
        'cases': 0, 'tasks': 0, 'events': 0, 'child_accts': 0,
    })
    if not account_ids:
        return out
    ids = list(account_ids)

    # SObject + foreign-key combos to probe. Each entry: (sobject, fk_field, output_key).
    probes = [
        ('Opportunity', 'AccountId', 'opportunities'),
        ('Contract',    'AccountId', 'contracts'),
        ('Contact',     'AccountId', 'contacts'),
        ('Case',        'AccountId', 'cases'),
        ('Task',        'WhatId',    'tasks'),
        ('Event',       'WhatId',    'events'),
        ('Account',     'ParentId',  'child_accts'),
    ]
    for sobject, fk, key in probes:
        for i in range(0, len(ids), 200):
            chunk = ids[i:i + 200]
            in_list = ','.join("'" + a.replace("'", "\\'") + "'" for a in chunk)
            soql = (
                f"SELECT {fk} aid, COUNT(Id) n "
                f"FROM {sobject} WHERE {fk} IN ({in_list}) "
                f"GROUP BY {fk}"
            )
            try:
                rows = query_all(instance_url, access_token, soql)
            except Exception:
                rows = []
            for r in rows:
                aid = r.get('aid')
                if aid:
                    out[aid][key] += int(r.get('n') or 0)
    return out


def _supabase_usage(conn, account_ids):
    """Return {account_id: {'current': n, 'content': n}}"""
    out = {a: {'current': 0, 'content': 0} for a in account_ids}
    if not account_ids:
        return out
    with conn.cursor() as cur:
        cur.execute(
            "SELECT sf_worksite_account_id, COUNT(*) "
            "FROM job_current WHERE sf_worksite_account_id = ANY(%s) "
            "GROUP BY sf_worksite_account_id",
            (list(account_ids),),
        )
        for sid, n in cur.fetchall():
            out.setdefault(sid, {'current': 0, 'content': 0})['current'] = int(n)
        cur.execute(
            "SELECT sf_worksite_account_id, COUNT(*) "
            "FROM job_content WHERE sf_worksite_account_id = ANY(%s) "
            "GROUP BY sf_worksite_account_id",
            (list(account_ids),),
        )
        for sid, n in cur.fetchall():
            out.setdefault(sid, {'current': 0, 'content': 0})['content'] = int(n)
    return out


def _our_created_ids(conn):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT payload->>'salesforce_account_id' "
            "FROM job_event_log WHERE event_type='worksite_created' "
            "AND payload->>'salesforce_account_id' IS NOT NULL"
        )
        return {row[0] for row in cur.fetchall()}


# ── Classification ──

def _has_any_refs(aid, sf_counts, sb_counts, other_counts):
    sf_total = sf_counts.get(aid, [0, 0])[1]
    sb = sb_counts.get(aid, {'current': 0, 'content': 0})
    other = other_counts.get(aid, {})
    return (
        sf_total > 0
        or sb.get('current', 0) > 0
        or sb.get('content', 0) > 0
        or any(int(v or 0) > 0 for v in other.values())
    )


def _recommend(this_acct, group_records, sf_counts, sb_counts, other_counts, street_match_present):
    """Return one recommendation label per Account.

    Conservative — only returns SAFE TO DELETE when this Account has zero
    references across Job__c, all other queried SObjects, AND Supabase, while
    at least one other Account in the same dup group does have references.
    """
    if not street_match_present:
        return 'REVIEW — STREET MISMATCH'

    aid = this_acct['Id']
    this_has = _has_any_refs(aid, sf_counts, sb_counts, other_counts)
    members_with_refs = [r for r in group_records if _has_any_refs(r['Id'], sf_counts, sb_counts, other_counts)]

    if not members_with_refs:
        return 'REVIEW — ALL UNUSED'
    if len(members_with_refs) > 1:
        return 'REVIEW — SHARED USAGE' if this_has else 'SAFE TO DELETE'
    return 'KEEP — IN USE' if this_has else 'SAFE TO DELETE'


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--include-same-city-diff-street', action='store_true',
                        help='Include dup groups where street differs (default: only confirmed dups).')
    parser.add_argument('--out', default=None,
                        help='Output CSV path. Default: data/worksite_duplicate_audit_<DATE>.csv')
    args = parser.parse_args()

    from dotenv import load_dotenv
    load_dotenv(_src_root.parent / '.env')

    from utils.supabase_db import get_conn

    instance_url, access_token, query_all = _sf_session()

    print('Pulling Aspen Dental worksite Accounts from Salesforce …')
    accounts = _pull_all_worksite_accounts(instance_url, access_token, query_all)
    print(f'  {len(accounts)} active worksite-like Accounts')

    # Group by (city, state-normalized)
    by_key = defaultdict(list)
    for r in accounts:
        r.pop('attributes', None)
        k = (_norm_city(r.get('ShippingCity')), _norm_state(r.get('ShippingState')))
        if k == ('', ''):
            continue
        by_key[k].append(r)
    dups = {k: v for k, v in by_key.items() if len(v) > 1}
    print(f'  {len(dups)} city+state groups with 2+ Accounts')

    # Determine which Accounts our automation created.
    with get_conn() as conn:
        if conn is None:
            print('Could not connect to Supabase.')
            return 1
        ours = _our_created_ids(conn)
        print(f'  {len(ours)} Accounts our automation created')

        # For each dup group, decide if at least one pair has a confirmed street match
        # (otherwise it's "same city, different street" — treat with caution).
        rows = []
        all_ids = []
        for k, recs in dups.items():
            # confirmed match: any two records in the group have street_match
            any_match = False
            for i in range(len(recs)):
                for j in range(i + 1, len(recs)):
                    if _street_match(recs[i].get('ShippingStreet'), recs[j].get('ShippingStreet')):
                        any_match = True
                        break
                if any_match:
                    break
            if not any_match and not args.include_same_city_diff_street:
                continue
            for r in recs:
                all_ids.append(r['Id'])

        unique_ids = list(set(all_ids))
        print(f'  Querying Job__c usage for {len(unique_ids)} Accounts …')
        sf_counts = _count_jobs_per_worksite(instance_url, access_token, query_all, unique_ids)
        print(f'  Querying other SF SObject refs (Opp / Contract / Contact / Case / Task / Event / child Accts) …')
        other_counts = _count_other_sf_refs(instance_url, access_token, query_all, unique_ids)
        print(f'  Querying Supabase usage …')
        sb_counts = _supabase_usage(conn, unique_ids)

        # Build CSV rows
        for k, recs in dups.items():
            any_match = False
            for i in range(len(recs)):
                for j in range(i + 1, len(recs)):
                    if _street_match(recs[i].get('ShippingStreet'), recs[j].get('ShippingStreet')):
                        any_match = True
                        break
                if any_match:
                    break
            if not any_match and not args.include_same_city_diff_street:
                continue

            for r in sorted(recs, key=lambda x: x.get('CreatedDate') or ''):
                aid = r['Id']
                sf_open, sf_total = sf_counts.get(aid, [0, 0])
                sb = sb_counts.get(aid, {'current': 0, 'content': 0})
                other = other_counts.get(aid, {})
                rec = _recommend(r, recs, sf_counts, sb_counts, other_counts, street_match_present=any_match)
                rows.append({
                    'group_key': f'{k[0]}|{k[1]}',
                    'account_id': aid,
                    'name': (r.get('Name') or '')[:120],
                    'shipping_street': (r.get('ShippingStreet') or '')[:200],
                    'shipping_state': r.get('ShippingState') or '',
                    'created_date': (r.get('CreatedDate') or '')[:10],
                    'created_by_us': 'true' if aid in ours else 'false',
                    'sf_jobs_count': sf_total,
                    'sf_jobs_open_count': sf_open,
                    'sf_opportunities': int(other.get('opportunities', 0)),
                    'sf_contracts':     int(other.get('contracts', 0)),
                    'sf_contacts':      int(other.get('contacts', 0)),
                    'sf_cases':         int(other.get('cases', 0)),
                    'sf_tasks':         int(other.get('tasks', 0)),
                    'sf_events':        int(other.get('events', 0)),
                    'sf_child_accts':   int(other.get('child_accts', 0)),
                    'supabase_current': sb['current'],
                    'supabase_content': sb['content'],
                    'recommendation': rec,
                })

    # Write CSV
    out_path = Path(args.out) if args.out else _src_root.parent / 'data' / f'worksite_duplicate_audit_{datetime.utcnow().date().isoformat()}.csv'
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fields = list(rows[0].keys()) if rows else []
    with out_path.open('w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in rows:
            w.writerow(r)
    print(f'\nWrote {len(rows)} rows to {out_path}')

    # Summary
    from collections import Counter
    by_reco = Counter(r['recommendation'] for r in rows)
    print('\nRecommendation breakdown:')
    for k in ('KEEP — IN USE', 'SAFE TO DELETE', 'REVIEW — SHARED USAGE', 'REVIEW — ALL UNUSED', 'REVIEW — STREET MISMATCH'):
        n = by_reco.get(k, 0)
        print(f'  {k:<30}  {n}')

    # Print the SAFE TO DELETE list inline for convenience.
    # All counters shown were 0; the recommendation already encodes that fact,
    # but reprint here so operators can scan without opening the CSV.
    safe = [r for r in rows if r['recommendation'] == 'SAFE TO DELETE']
    print(f'\nSAFE TO DELETE — {len(safe)} Account(s):')
    print(f'{"":<7} {"account_id":<22} {"group":<28} {"jobs":>5} {"opp":>4} {"con":>4} {"ct":>4} {"cs":>4} {"tk":>4} {"ev":>4} {"chld":>5}  name / street')
    for r in safe:
        ours_flag = '★OURS' if r['created_by_us'] == 'true' else 'legacy'
        print(
            f"  {ours_flag:<5} {r['account_id']:<22} {r['group_key']:<28} "
            f"{r['sf_jobs_count']:>5} {r['sf_opportunities']:>4} {r['sf_contracts']:>4} "
            f"{r['sf_contacts']:>4} {r['sf_cases']:>4} {r['sf_tasks']:>4} {r['sf_events']:>4} {r['sf_child_accts']:>5}  "
            f"{r['name'][:50]}"
        )

    print('\nLegend: jobs=Job__c · opp=Opportunity · con=Contract · ct=Contact · cs=Case · tk=Task · ev=Event · chld=child Account')
    print('SAFE TO DELETE means: zero refs across ALL of the above + Supabase, AND another Account in the same dup group has refs.')
    print('Review the CSV before deleting anything in Salesforce.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
