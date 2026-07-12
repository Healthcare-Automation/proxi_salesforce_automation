"""
One-off READ-ONLY audit: do any Job__c records the Kimedics automation touched
have an Owner that resolves to "Sean Yang"?

Touched jobs = distinct sf_job_id in job_current ∪ job_content (Supabase).
"""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

# Load .env
for line in (ROOT / ".env").read_text().splitlines():
    line = line.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    k, _, v = line.partition("=")
    os.environ.setdefault(k.strip(), v.strip())

from utils.supabase_db import get_conn  # noqa: E402
import json  # noqa: E402
import urllib.parse  # noqa: E402
import urllib.request  # noqa: E402

from utils.salesforce import query_all  # noqa: E402


def touched_job_ids() -> list[str]:
    ids: set[str] = set()
    with get_conn() as conn, conn.cursor() as cur:
        for tbl in ("job_current", "job_content"):
            try:
                cur.execute(
                    f"SELECT DISTINCT sf_job_id FROM public.{tbl} "
                    f"WHERE sf_job_id IS NOT NULL AND sf_job_id <> ''"
                )
                ids.update(r[0].strip() for r in cur.fetchall() if r[0])
            except Exception as e:  # table may not exist in some envs
                print(f"  (skip {tbl}: {e})")
                conn.rollback()
    return sorted(ids)


def sf_token() -> tuple[str, str]:
    # Org has password/SOAP disabled; client_credentials must POST to the My Domain token endpoint.
    ck = os.environ["SALESFORCE_CONSUMER_KEY"].strip()
    cs = os.environ["SALESFORCE_CONSUMER_SECRET"].strip()
    # client_credentials is only supported on the My Domain host (not login.salesforce.com,
    # which is what SALESFORCE_TOKEN_URL points at for the password flow).
    base = "https://proxi.my.salesforce.com/services/oauth2/token"
    body = urllib.parse.urlencode(
        {"grant_type": "client_credentials", "client_id": ck, "client_secret": cs}
    ).encode()
    req = urllib.request.Request(base, data=body, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    with urllib.request.urlopen(req) as resp:
        t = json.loads(resp.read().decode())
    return t["instance_url"], t["access_token"]


def chunks(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def main() -> int:
    job_ids = touched_job_ids()
    print(f"Touched Job__c ids in Supabase: {len(job_ids)}")
    if not job_ids:
        print("Nothing to check.")
        return 0

    inst, tok = sf_token()

    rows: list[dict] = []
    for batch in chunks(job_ids, 200):
        in_list = ", ".join("'" + j.replace("'", "\\'") + "'" for j in batch)
        soql = (
            "SELECT Id, Name, OwnerId, Owner.Name, Owner.Username, Owner.Type "
            f"FROM Job__c WHERE Id IN ({in_list})"
        )
        rows.extend(query_all(inst, tok, soql))

    found_in_sf = {r["Id"] for r in rows}
    missing = [j for j in job_ids if j not in found_in_sf]

    # Owner.Name comes back flattened by query_all's attribute stripping; relationship
    # fields land as nested dicts, so pull defensively.
    def owner_name(r: dict) -> str:
        o = r.get("Owner")
        if isinstance(o, dict):
            return (o.get("Name") or "").strip()
        return (r.get("Owner.Name") or "").strip()

    def is_sean(name: str) -> bool:
        n = name.lower().replace(",", " ").split()
        return "sean" in n and "yang" in n

    sean_hits = [r for r in rows if is_sean(owner_name(r))]

    print(f"Resolved in Salesforce: {len(rows)}  |  not found in SF: {len(missing)}")
    print("\nOwner breakdown (touched jobs):")
    counts: dict[str, int] = {}
    for r in rows:
        counts[owner_name(r) or "(blank)"] = counts.get(owner_name(r) or "(blank)", 0) + 1
    for name, c in sorted(counts.items(), key=lambda kv: -kv[1]):
        flag = "  <-- SEAN YANG" if is_sean(name) else ""
        print(f"  {c:>4}  {name}{flag}")

    print("\n=== RESULT ===")
    if sean_hits:
        print(f"⚠️  {len(sean_hits)} touched job(s) owned by Sean Yang:")
        for r in sean_hits:
            print(f"  {r['Id']}  {r.get('Name')}  owner={owner_name(r)}")
    else:
        print("✅ No touched Job__c is owned by Sean Yang.")

    if missing:
        print(f"\nNote: {len(missing)} touched sf_job_id(s) no longer resolve in SF "
              f"(deleted/merged); cannot inspect their owner. Sample: {missing[:5]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
