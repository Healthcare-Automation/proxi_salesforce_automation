"""
Salesforce REST API helpers — READ-ONLY (queries and describe only).
Supports OAuth 2.0 Client Credentials flow (ECA) and username-password flow.
No create, update, or delete operations.
"""

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional, Union

# Default API version for REST
DEFAULT_API_VERSION = "v59.0"


def _sf_http_error_message(exc: urllib.error.HTTPError) -> str:
    """Best-effort parse of Salesforce REST error JSON from an urllib HTTPError body."""
    try:
        raw = exc.read().decode("utf-8", errors="replace")
    except Exception:
        return exc.reason or str(exc)
    if not raw.strip():
        return exc.reason or f"HTTP {exc.code}"
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return raw[:1200]
    if isinstance(data, list):
        parts: list[str] = []
        for err in data:
            if isinstance(err, dict):
                parts.append(str(err.get("message") or err.get("errorCode") or err))
            else:
                parts.append(str(err))
        return "; ".join(parts) if parts else raw[:800]
    if isinstance(data, dict):
        return str(
            data.get("message")
            or data.get("error_description")
            or data.get("error")
            or raw[:800]
        )
    return raw[:800]


class SalesforceLoginError(RuntimeError):
    """
    Raised when username/password (and optional SOAP) login exhausts all attempts.
    ``attempts`` is a list of dicts: kind, url, outcome, detail (no passwords).
    """

    def __init__(
        self,
        summary: str,
        *,
        attempts: list[dict],
        diagnosis: str,
        context: dict,
    ) -> None:
        super().__init__(summary)
        self.attempts = attempts
        self.diagnosis = diagnosis
        self.context = context

    def __str__(self) -> str:
        lines = [
            str(self.args[0]) if self.args else "Salesforce login failed.",
            "",
            "--- Context (no secrets) ---",
        ]
        for k, v in self.context.items():
            lines.append(f"  {k}: {v}")
        lines.append("")
        lines.append("--- Attempt log ---")
        for i, a in enumerate(self.attempts, 1):
            lines.append(f"  {i}. [{a.get('kind')}] {a.get('url', '')}")
            lines.append(f"     {a.get('outcome')}: {a.get('detail', '')[:280]}")
        lines.append("")
        lines.append("--- Likely cause (heuristic) ---")
        lines.append(self.diagnosis)
        return "\n".join(lines)


def _mask_username(username: str) -> str:
    u = (username or "").strip()
    if not u:
        return "(empty)"
    if "@" in u:
        local, _, domain = u.partition("@")
        if not local:
            return f"***@{domain}"
        return f"{local[0]}***@{domain}"
    return f"{u[:2]}***" if len(u) > 2 else "***"


def _oauth_error_is_authentication_failure(msg: str) -> bool:
    return "authentication failure" in (msg or "").lower()


def _diagnose_login(
    attempts: list[dict],
    *,
    has_security_token: bool,
    use_sandbox: bool,
    my_domain_host: Optional[str],
) -> str:
    """Plain-language guess from attempt outcomes (not authoritative)."""
    details = " ".join((a.get("detail") or "") for a in attempts).lower()
    lines: list[str] = []

    if "invalid_login" in details or "invalid username, password, security token" in details:
        if not has_security_token:
            lines.append(
                "• SOAP/OAuth both rejected credentials. You are not sending a security token. "
                "Most API logins require password + token (concatenated, no space) unless your "
                "Salesforce profile has trusted IP ranges that include this machine."
            )
        else:
            lines.append(
                "• Token is set in env but login still fails: password may be wrong, token may be "
                "stale (reset token invalidates the old one), or this username is for the other "
                "environment (prod vs sandbox)."
            )

    if use_sandbox:
        lines.append(
            "• use_sandbox=True → auth hosts are test.salesforce.com first. "
            "If this user is production-only, set SALESFORCE_USE_SANDBOX=false."
        )
    else:
        lines.append(
            "• use_sandbox=False → auth hosts are login.salesforce.com first. "
            "If this user exists only in a sandbox, set SALESFORCE_USE_SANDBOX=true."
        )

    if my_domain_host:
        lines.append(
            f"• My Domain from SALESFORCE_TOKEN_URL is used as an extra host: {my_domain_host}. "
            "If unset, only login/test (and optional My Domain) are tried."
        )
    else:
        lines.append(
            "• No *.my.salesforce.com in SALESFORCE_TOKEN_URL — only global login/test hosts (and SOAP on those). "
            "You can set SALESFORCE_TOKEN_URL=https://YOURDOMAIN.my.salesforce.com to try your org login host."
        )

    if not lines:
        lines.append("• Compare username/password with a successful browser login for the same org (prod vs sandbox).")

    return "\n".join(lines)


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
        low = (msg or "").lower()
        if "no valid scopes defined" in low:
            hint = " Add OAuth scopes in Salesforce: Setup → External Client App Manager → Edit app → OAuth Scopes (e.g. api, refresh_token, offline_access) → Save."
        raise RuntimeError(f"Salesforce OAuth failed ({e.code}): {msg}.{hint}") from None


def get_token_client_credentials(
    consumer_key: str,
    consumer_secret: str,
    *,
    token_url: Optional[str] = None,
    use_sandbox: bool = False,
) -> dict:
    """
    Get OAuth access token via Client Credentials flow (External Client App / ECA).
    No username or password. Requires "Run As" configured in the ECA's OAuth Policies.
    Returns dict with access_token, instance_url, etc.

    Token POST always uses login.salesforce.com (production) or test.salesforce.com (sandbox).
    ``token_url`` is ignored (kept for call-site compatibility).
    """
    host = "test.salesforce.com" if use_sandbox else "login.salesforce.com"
    url = f"https://{host}/services/oauth2/token"

    body = {
        "grant_type": "client_credentials",
        "client_id": (consumer_key or "").strip(),
        "client_secret": (consumer_secret or "").strip(),
    }
    return _request_token(url, body)


def _soap_fault_message(xml_text: str) -> Optional[str]:
    m = re.search(r"<faultstring[^>]*>([^<]*)</faultstring>", xml_text, re.I)
    if m:
        s = (m.group(1) or "").strip()
        return s or None
    return None


# Appended when Salesforce returns INVALID_LOGIN / authentication failure (not a code defect).
_SALESFORCE_LOGIN_HELP = (
    " Fix: (1) Password in .env must match what you use at login.salesforce.com. "
    "(2) If API logins need it, append the user's security token — Salesforce → Settings → "
    "Reset My Security Token → set SALESFORCE_SECURITY_TOKEN (we concatenate it to the password). "
    "(3) Sandbox-only users: set SALESFORCE_USE_SANDBOX=true. "
    "(4) Check the user is not locked out in Setup."
)


def _xml_escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _my_domain_host_from_token_url(token_url: Optional[str]) -> Optional[str]:
    """If SALESFORCE_TOKEN_URL is a My Domain host, return hostname (e.g. acme.my.salesforce.com)."""
    if not (token_url and str(token_url).strip()):
        return None
    raw = str(token_url).strip().rstrip("/")
    if "oauth2/token" in raw:
        raw = raw.split("/services/oauth2")[0].rstrip("/")
    if not raw.lower().startswith("http"):
        raw = f"https://{raw.lstrip('/')}"
    host = (urllib.parse.urlparse(raw).hostname or "").lower()
    if host.endswith(".my.salesforce.com"):
        return host
    return None


def _oauth_password_urls(use_sandbox: bool, token_url: Optional[str]) -> list[str]:
    primary = "test.salesforce.com" if use_sandbox else "login.salesforce.com"
    secondary = "login.salesforce.com" if use_sandbox else "test.salesforce.com"
    urls: list[str] = []
    seen: set[str] = set()
    for host in (primary, secondary):
        u = f"https://{host}/services/oauth2/token"
        if u not in seen:
            seen.add(u)
            urls.append(u)
    md = _my_domain_host_from_token_url(token_url)
    if md and md not in (primary, secondary):
        u = f"https://{md}/services/oauth2/token"
        if u not in seen:
            seen.add(u)
            urls.append(u)
    return urls


def _soap_login_bases(use_sandbox: bool, token_url: Optional[str]) -> list[str]:
    primary = "test.salesforce.com" if use_sandbox else "login.salesforce.com"
    secondary = "login.salesforce.com" if use_sandbox else "test.salesforce.com"
    bases: list[str] = []
    seen: set[str] = set()
    for host in (primary, secondary):
        b = f"https://{host}"
        if b not in seen:
            seen.add(b)
            bases.append(b)
    md = _my_domain_host_from_token_url(token_url)
    if md:
        b = f"https://{md}"
        if b not in seen:
            bases.append(b)
    return bases


def _soap_partner_login(login_base: str, username: str, password: str, *, api_version: str = DEFAULT_API_VERSION) -> dict:
    """
    Partner WSDL login(); returns the same shape as OAuth (access_token + instance_url).
    Session id is accepted as Bearer for REST. See Salesforce SOAP API login.
    """
    uver = api_version.lstrip("vV")
    url = f"{login_base.rstrip('/')}/services/Soap/u/{uver}"
    envelope = f"""<?xml version="1.0" encoding="utf-8"?>
<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" xmlns:urn="urn:partner.soap.sforce.com">
<soapenv:Body>
  <urn:login>
    <urn:username>{_xml_escape(username)}</urn:username>
    <urn:password>{_xml_escape(password)}</urn:password>
  </urn:login>
</soapenv:Body>
</soapenv:Envelope>"""
    req = urllib.request.Request(url, data=envelope.encode("utf-8"), method="POST")
    req.add_header("Content-Type", "text/xml; charset=UTF-8")
    req.add_header("SOAPAction", "login")
    try:
        with urllib.request.urlopen(req) as resp:
            text = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        detail = e.fp.read().decode("utf-8", errors="replace") if e.fp else ""
        fault = _soap_fault_message(detail)
        if fault:
            raise RuntimeError(fault) from None
        raise RuntimeError(f"SOAP login HTTP {e.code}: {detail[:400]}") from None

    fault = _soap_fault_message(text)
    if fault:
        raise RuntimeError(fault)

    m_sid = re.search(r"<sessionId>([^<]+)</sessionId>", text)
    m_srv = re.search(r"<serverUrl>([^<]+)</serverUrl>", text)
    if not m_sid or not m_srv:
        raise RuntimeError("SOAP login: missing sessionId or serverUrl in response")

    session_id = m_sid.group(1).strip()
    server_url = m_srv.group(1).strip()
    parsed = urllib.parse.urlparse(server_url)
    instance_url = f"{parsed.scheme}://{parsed.netloc}".rstrip("/")
    return {"access_token": session_id, "instance_url": instance_url}


def get_token_auto(
    consumer_key: str,
    consumer_secret: str,
    username: Optional[str] = None,
    password: Optional[str] = None,
    *,
    use_client_credentials: bool = True,
    security_token: Optional[str] = None,
    use_sandbox: bool = False,
    token_url: Optional[str] = None,
) -> dict:
    """
    Obtain access_token + instance_url for API use.

    If ``use_client_credentials`` is False, only the username-password flow runs.

    If ``use_client_credentials`` is True **and** both username and password are non-empty,
    the **username-password** flow is used (same Connected App id/secret as the browser OAuth app).
    Client credentials are used only when no password is available (headless automation).

    ``https://*.lightning.force.com`` is the web UI, not the token endpoint; tokens POST to
    login.salesforce.com or test.salesforce.com.
    """
    has_user_pass = bool((username or "").strip() and (password or "").strip())

    if not use_client_credentials:
        if not has_user_pass:
            raise RuntimeError("Username and password required when use_client_credentials is False.")
        return get_token(
            consumer_key,
            consumer_secret,
            username or "",
            password or "",
            security_token=security_token,
            use_sandbox=use_sandbox,
            token_url=token_url,
        )

    if has_user_pass:
        return get_token(
            consumer_key,
            consumer_secret,
            username or "",
            password or "",
            security_token=security_token,
            use_sandbox=use_sandbox,
            token_url=token_url,
        )

    return get_token_client_credentials(
        consumer_key,
        consumer_secret,
        token_url=token_url,
        use_sandbox=use_sandbox,
    )


def get_token(
    consumer_key: str,
    consumer_secret: str,
    username: str,
    password: str,
    *,
    security_token: Optional[str] = None,
    use_sandbox: bool = False,
    token_url: Optional[str] = None,
) -> dict:
    """
    Username-password OAuth, then SOAP Partner ``login`` if every OAuth attempt returns
    ``authentication failure`` (e.g. External Client App with password grant disabled).

    SOAP returns a session id usable as REST ``Authorization: Bearer`` for the same org.

    On total failure raises `SalesforceLoginError` with an attempt log and heuristic diagnosis.
    """
    has_security_token = bool((security_token or "").strip())
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

    user = (username or "").strip()
    attempts: list[dict] = []
    my_domain_host = _my_domain_host_from_token_url(token_url)
    uver = DEFAULT_API_VERSION.lstrip("vV")
    context = {
        "username_masked": _mask_username(user),
        "use_sandbox": use_sandbox,
        "security_token_env_set": has_security_token,
        "consumer_key_chars": len((consumer_key or "").strip()),
        "consumer_secret_chars": len((consumer_secret or "").strip()),
        "my_domain_from_token_url": my_domain_host or "(none)",
    }

    last_oauth: Optional[RuntimeError] = None
    for oauth_u in _oauth_password_urls(use_sandbox, token_url):
        try:
            result = _request_token(oauth_u, body)
            attempts.append(
                {"kind": "oauth_password", "url": oauth_u, "outcome": "success", "detail": "access_token returned"}
            )
            return result
        except RuntimeError as e:
            detail = str(e)
            attempts.append(
                {"kind": "oauth_password", "url": oauth_u, "outcome": "error", "detail": detail[:600]}
            )
            last_oauth = e
            if not _oauth_error_is_authentication_failure(detail):
                diagnosis = (
                    "OAuth failed with something other than 'authentication failure' (see attempt detail). "
                    "Fix Connected App OAuth settings, client id/secret, or API access on the app."
                )
                raise SalesforceLoginError(
                    "Salesforce OAuth login aborted (see attempt log).",
                    attempts=attempts,
                    diagnosis=diagnosis,
                    context=context,
                ) from None

    last_soap: Optional[BaseException] = None
    for base in _soap_login_bases(use_sandbox, token_url):
        soap_url = f"{base.rstrip('/')}/services/Soap/u/{uver}"
        try:
            result = _soap_partner_login(base, user, pwd)
            attempts.append(
                {
                    "kind": "soap_partner_login",
                    "url": soap_url,
                    "outcome": "success",
                    "detail": "sessionId returned",
                }
            )
            return result
        except BaseException as e:
            detail = str(e)
            attempts.append(
                {"kind": "soap_partner_login", "url": soap_url, "outcome": "error", "detail": detail[:600]}
            )
            last_soap = e

    diagnosis = _diagnose_login(
        attempts,
        has_security_token=has_security_token,
        use_sandbox=use_sandbox,
        my_domain_host=my_domain_host,
    )
    diagnosis = f"{diagnosis}\n\n{_SALESFORCE_LOGIN_HELP.strip()}"

    summary = "Salesforce login failed: every OAuth password and SOAP login attempt was rejected."
    if last_soap is not None:
        raise SalesforceLoginError(
            summary,
            attempts=attempts,
            diagnosis=diagnosis,
            context=context,
        ) from None
    if last_oauth is not None:
        raise SalesforceLoginError(
            summary,
            attempts=attempts,
            diagnosis=diagnosis,
            context=context,
        ) from None
    raise SalesforceLoginError(
        summary,
        attempts=attempts,
        diagnosis=diagnosis,
        context=context,
    )


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
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        msg = _sf_http_error_message(e)
        raise RuntimeError(f"Salesforce REST GET failed (HTTP {e.code}): {msg}") from e


def list_sobjects(
    instance_url: str,
    access_token: str,
    api_version: str = DEFAULT_API_VERSION,
) -> list[dict]:
    """
    List all sobjects (standard + custom) available in the org. READ-ONLY.
    Returns list of dicts with name, label, custom, etc.
    """
    path = "sobjects"
    result = _api_request(instance_url, access_token, path, api_version=api_version)
    return result.get("sobjects", [])


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
    """
    Return field names safe to put in SOQL SELECT.

    Excludes compound address/location types (always non-queryable in SOQL).
    The field-level ``queryable`` attribute is not returned by all API versions,
    so we default to True when absent and only exclude if explicitly False.
    """
    return [
        f["name"]
        for f in describe_response.get("fields", [])
        if f.get("queryable", True) is not False
        and f.get("type") not in ("address", "location")
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
        try:
            with urllib.request.urlopen(req) as resp:
                result = json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            msg = _sf_http_error_message(e)
            raise RuntimeError(f"Salesforce SOQL query failed (HTTP {e.code}): {msg}") from e

        records = result.get("records", [])
        for rec in records:
            row = {k: v for k, v in rec.items() if k != "attributes"}
            all_records.append(row)

        next_path = result.get("nextRecordsUrl")
        full_url = f"{base}{next_path}" if next_path else None

    return all_records


def query_count(
    instance_url: str,
    access_token: str,
    sobject_name: str,
    api_version: str = DEFAULT_API_VERSION,
) -> int:
    """
    Return total record count for an sobject. Uses SELECT COUNT(Id) (one API call). READ-ONLY.
    """
    soql = f"SELECT COUNT(Id) FROM {sobject_name}"
    base = instance_url.rstrip("/")
    path = f"query?q={urllib.parse.quote(soql)}"
    url = f"{base}/services/data/{api_version}/{path}"
    req = urllib.request.Request(url, method="GET")
    req.add_header("Authorization", f"Bearer {access_token}")
    req.add_header("Accept", "application/json")
    try:
        with urllib.request.urlopen(req) as resp:
            result = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        msg = _sf_http_error_message(e)
        raise RuntimeError(f"Salesforce COUNT query failed (HTTP {e.code}): {msg}") from e
    records = result.get("records") or []
    if not records:
        return 0
    r0 = {k: v for k, v in records[0].items() if k != "attributes"}
    if len(r0) == 1:
        v = next(iter(r0.values()))
        if isinstance(v, (int, float)):
            return int(v)
    if len(r0) > 1 and "expr0" in r0:
        v = r0["expr0"]
        if isinstance(v, (int, float)):
            return int(v)
    return int(result.get("totalSize", 0))


# Minimal columns for Supabase ↔ Salesforce job id / practice matching (keep SOQL short and valid).
# LastModifiedDate is included so the resolver's ID-swap candidate picker can break ties
# deterministically when multiple SF Job__c records are valid swap targets.
_RESOLVER_JOB_FIELDS: tuple[str, ...] = (
    "Id",
    "Job_Client_Job_Id__c",
    "Job_Worksite_Location_1__c",
    "External_Job_ID__c",
    "LastModifiedDate",
)


def pull_jobs_for_id_resolve(
    consumer_key: str,
    consumer_secret: str,
    username: Optional[str] = None,
    password: Optional[str] = None,
    *,
    use_client_credentials: bool = True,
    token_url: Optional[str] = None,
    security_token: Optional[str] = None,
    use_sandbox: bool = False,
    job_object_name: str = "Job__c",
    api_version: str = DEFAULT_API_VERSION,
) -> list[dict]:
    """
    Pull only the Job__c fields needed to resolve ``sf_job_id`` / worksite in Supabase.

    Uses **queryable** fields from describe only (including non-queryable fields in SOQL makes the
    entire query fail). Avoids the huge ``SELECT * …`` style query from :func:`pull_all_jobs`.
    """
    token_data = get_token_auto(
        consumer_key,
        consumer_secret,
        username,
        password,
        use_client_credentials=use_client_credentials,
        security_token=security_token,
        use_sandbox=use_sandbox,
        token_url=token_url,
    )
    instance_url = token_data["instance_url"]
    access_token = token_data["access_token"]

    fields_str = ", ".join(_RESOLVER_JOB_FIELDS)
    soql = f"SELECT {fields_str} FROM {job_object_name}"
    return query_all(instance_url, access_token, soql, api_version)


def query_jobs_by_external_id_exact(
    instance_url: str,
    access_token: str,
    external_job_id: str,
    *,
    job_object_name: str = "Job__c",
    api_version: str = DEFAULT_API_VERSION,
) -> list[dict]:
    """
    Targeted SOQL for one ``External_Job_ID__c`` value (exact match, same truncation as push).

    Used before POSTing a new ``Job__c`` so we **re-link** Supabase to an existing Salesforce row
    after a DB reset, instead of failing on a duplicate external-id rule.

    ``job_object_name`` must be a simple API name (alphanumeric + underscore).
    """
    import re

    eid = (external_job_id or "").strip()
    if not eid:
        return []
    oname = (job_object_name or "Job__c").strip()
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9_]*$", oname):
        return []
    safe = eid.replace("\\", "\\\\").replace("'", "\\'")
    fields_str = ", ".join(_RESOLVER_JOB_FIELDS)
    soql = f"SELECT {fields_str} FROM {oname} WHERE External_Job_ID__c = '{safe}'"
    return query_all(instance_url, access_token, soql, api_version)


def query_jobs_by_worksite_id_exact(
    instance_url: str,
    access_token: str,
    worksite_account_id: str,
    *,
    job_object_name: str = "Job__c",
    api_version: str = DEFAULT_API_VERSION,
) -> list[dict]:
    """
    Targeted SOQL for ``Job_Worksite_Location_1__c = <Id>``.

    Used as the "one Job__c per worksite" safety net before POSTing a new ``Job__c``.
    Salesforce's data model treats ``Job_Client_Job_Id__c`` as unique-per-practice, so
    in practice each worksite Account should already have at most one Job__c — if we
    find one here, we should re-link instead of creating a duplicate.
    """
    import re

    wid = (worksite_account_id or "").strip()
    if not wid:
        return []
    oname = (job_object_name or "Job__c").strip()
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9_]*$", oname):
        return []
    safe = wid.replace("\\", "\\\\").replace("'", "\\'")
    fields_str = ", ".join(_RESOLVER_JOB_FIELDS)
    soql = (
        f"SELECT {fields_str} FROM {oname} "
        f"WHERE Job_Worksite_Location_1__c = '{safe}' "
        f"ORDER BY LastModifiedDate DESC"
    )
    return query_all(instance_url, access_token, soql, api_version)


def pull_all_jobs(
    consumer_key: str,
    consumer_secret: str,
    username: Optional[str] = None,
    password: Optional[str] = None,
    *,
    use_client_credentials: bool = True,
    token_url: Optional[str] = None,
    security_token: Optional[str] = None,
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
    token_data = get_token_auto(
        consumer_key,
        consumer_secret,
        username,
        password,
        use_client_credentials=use_client_credentials,
        security_token=security_token,
        use_sandbox=use_sandbox,
        token_url=token_url,
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
