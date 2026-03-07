"""
Salesforce REST API helpers — READ-ONLY (queries and describe only).
Supports OAuth 2.0 Client Credentials flow (ECA) and username-password flow.
No create, update, or delete operations.
"""

import json
import urllib.error
import urllib.parse
import urllib.request

# Default API version for REST
DEFAULT_API_VERSION = "v59.0"


def _request_token(url: str, body: dict) -> dict:
    """POST to token endpoint and return JSON; raise with body message on HTTP error."""
    data = urllib.parse.urlencode(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body_bytes = e.fp.read() if e.fp else b""
        try:
            err_body = json.loads(body_bytes.decode())
            msg = err_body.get("error_description") or err_body.get("error") or str(err_body)
        except Exception:
            msg = body_bytes.decode("utf-8", errors="replace") or e.reason
        hint = ""
        if "no valid scopes defined" in (msg or "").lower():
            hint = " Add OAuth scopes in Salesforce: Setup → External Client App Manager → Edit app → OAuth Scopes (e.g. api, refresh_token, offline_access) → Save."
        raise RuntimeError(f"Salesforce OAuth failed ({e.code}): {msg}.{hint}") from e


def get_token_client_credentials(
    consumer_key: str,
    consumer_secret: str,
    *,
    token_url: str | None = None,
    use_sandbox: bool = False,
) -> dict:
    """
    Get OAuth access token via Client Credentials flow (External Client App / ECA).
    No username or password. Requires "Run As" configured in the ECA's OAuth Policies.
    Returns dict with access_token, instance_url, etc.
    """
    if token_url:
        url = token_url.strip().rstrip("/")
        if "oauth2/token" not in url:
            url = f"{url}/services/oauth2/token"
    else:
        domain = "test.salesforce.com" if use_sandbox else "login.salesforce.com"
        url = f"https://{domain}/services/oauth2/token"

    body = {
        "grant_type": "client_credentials",
        "client_id": (consumer_key or "").strip(),
        "client_secret": (consumer_secret or "").strip(),
    }
    return _request_token(url, body)


def get_token(
    consumer_key: str,
    consumer_secret: str,
    username: str,
    password: str,
    *,
    security_token: str | None = None,
    use_sandbox: bool = False,
) -> dict:
    """
    Get OAuth access token via username-password flow (classic Connected App).
    Returns dict with access_token, instance_url, and optionally id.
    """
    domain = "test.salesforce.com" if use_sandbox else "login.salesforce.com"
    url = f"https://{domain}/services/oauth2/token"

    pwd = (password or "")
    if security_token:
        pwd = f"{pwd}{security_token}"

    body = {
        "grant_type": "password",
        "client_id": (consumer_key or "").strip(),
        "client_secret": (consumer_secret or "").strip(),
        "username": (username or "").strip(),
        "password": pwd,
    }
    return _request_token(url, body)


def _api_request(
    instance_url: str,
    access_token: str,
    path: str,
    *,
    method: str = "GET",
    api_version: str = DEFAULT_API_VERSION,
) -> dict:
    """Execute a single REST API request (GET). Used only for read operations."""
    base = instance_url.rstrip("/")
    if path.startswith("/"):
        path = path.lstrip("/")
    if not path.startswith("services/"):
        path = f"services/data/{api_version}/{path}"
    url = f"{base}/{path}"
    req = urllib.request.Request(url, method=method)
    req.add_header("Authorization", f"Bearer {access_token}")
    req.add_header("Accept", "application/json")
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode())


def describe_sobject(
    instance_url: str,
    access_token: str,
    sobject_name: str,
    api_version: str = DEFAULT_API_VERSION,
) -> dict:
    """Describe an sobject (metadata). READ-ONLY."""
    path = f"sobjects/{sobject_name}/describe"
    return _api_request(instance_url, access_token, path, api_version=api_version)


def sobject_queryable_fields(describe_response: dict) -> list[str]:
    """Return list of field names from describe (excludes compound types like address)."""
    return [
        f["name"]
        for f in describe_response.get("fields", [])
        if f.get("type") not in ("address", "location")
    ]


def query_all(
    instance_url: str,
    access_token: str,
    soql: str,
    api_version: str = DEFAULT_API_VERSION,
) -> list[dict]:
    """
    Run a SOQL query and follow nextRecordsUrl until done. READ-ONLY.
    Returns a single list of all records (each record is a dict with field keys; attributes stripped).
    """
    all_records = []
    base = instance_url.rstrip("/")
    path = f"query?q={urllib.parse.quote(soql)}"
    full_url = f"{base}/services/data/{api_version}/{path}"

    while full_url:
        req = urllib.request.Request(full_url, method="GET")
        req.add_header("Authorization", f"Bearer {access_token}")
        req.add_header("Accept", "application/json")
        with urllib.request.urlopen(req) as resp:
            result = json.loads(resp.read().decode())

        records = result.get("records", [])
        for rec in records:
            row = {k: v for k, v in rec.items() if k != "attributes"}
            all_records.append(row)

        next_path = result.get("nextRecordsUrl")
        full_url = f"{base}{next_path}" if next_path else None

    return all_records


def pull_all_jobs(
    consumer_key: str,
    consumer_secret: str,
    username: str | None = None,
    password: str | None = None,
    *,
    use_client_credentials: bool = True,
    token_url: str | None = None,
    security_token: str | None = None,
    use_sandbox: bool = False,
    job_object_name: str = "Job__c",
    api_version: str = DEFAULT_API_VERSION,
) -> list[dict]:
    """
    Authenticate and pull all records from the given job sobject. READ-ONLY.
    Uses describe to get queryable fields, then queries with pagination.

    By default uses Client Credentials flow (ECA); set use_client_credentials=False
    and pass username/password for username-password flow.
    """
    if use_client_credentials:
        token_data = get_token_client_credentials(
            consumer_key,
            consumer_secret,
            token_url=token_url,
            use_sandbox=use_sandbox,
        )
    else:
        token_data = get_token(
            consumer_key,
            consumer_secret,
            username or "",
            password or "",
            security_token=security_token,
            use_sandbox=use_sandbox,
        )
    instance_url = token_data["instance_url"]
    access_token = token_data["access_token"]

    try:
        describe = describe_sobject(instance_url, access_token, job_object_name, api_version)
        fields = sobject_queryable_fields(describe)
    except Exception:
        fields = ["Id", "Name"]

    if not fields:
        return []

    fields_str = ", ".join(fields)
    soql = f"SELECT {fields_str} FROM {job_object_name}"
    return query_all(instance_url, access_token, soql, api_version)
