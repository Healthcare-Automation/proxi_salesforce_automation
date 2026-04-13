"""
Minimal Salesforce REST for Job__c writes: stdlib only.
Does not import utils.salesforce.

OAuth tokens must be requested from login.salesforce.com or test.salesforce.com.
Posting to *.my.salesforce.com/services/oauth2/token often returns
"request not supported on this domain".
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

DEFAULT_REST_VERSION = "v59.0"


class OAuthTokenHTTPError(RuntimeError):
    """OAuth /services/oauth2/token returned a non-success HTTP status (debuggable, no secrets in message)."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int,
        request_url: str,
        error_code: Optional[str] = None,
        error_description: Optional[str] = None,
        response_body: str = "",
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.request_url = request_url
        self.error_code = error_code
        self.error_description = error_description
        self.response_body = (response_body or "")[:4000]


def _http_form_post(url: str, form: dict) -> dict:
    data = urllib.parse.urlencode(form).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    req.add_header("Accept", "application/json")
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        raw = e.fp.read().decode("utf-8", errors="replace") if e.fp else ""
        err_code: Optional[str] = None
        err_desc: Optional[str] = None
        try:
            err = json.loads(raw)
            err_code = err.get("error")
            err_desc = err.get("error_description") or err.get("error")
            msg = err_desc or err_code or raw
        except json.JSONDecodeError:
            msg = raw or e.reason
        raise OAuthTokenHTTPError(
            f"OAuth HTTP {e.code}: {msg}",
            status_code=e.code,
            request_url=url,
            error_code=err_code if isinstance(err_code, str) else None,
            error_description=err_desc if isinstance(err_desc, str) else None,
            response_body=raw,
        ) from None


def oauth_client_credentials(login_base: str, client_id: str, client_secret: str) -> dict:
    """
    login_base: https://login.salesforce.com (prod) or https://test.salesforce.com (sandbox).
    """
    base = login_base.rstrip("/")
    url = f"{base}/services/oauth2/token"
    out = _http_form_post(
        url,
        {
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
        },
    )
    if "access_token" not in out or "instance_url" not in out:
        raise RuntimeError(f"Unexpected OAuth response keys: {list(out.keys())}")
    return out


def oauth_password(
    login_base: str,
    client_id: str,
    client_secret: str,
    username: str,
    password: str,
) -> dict:
    base = login_base.rstrip("/")
    url = f"{base}/services/oauth2/token"
    out = _http_form_post(
        url,
        {
            "grant_type": "password",
            "client_id": client_id,
            "client_secret": client_secret,
            "username": username,
            "password": password,
        },
    )
    if "access_token" not in out or "instance_url" not in out:
        raise RuntimeError(f"Unexpected OAuth response keys: {list(out.keys())}")
    return out


def rest_json(
    instance_url: str,
    access_token: str,
    method: str,
    path: str,
    *,
    body: Optional[dict] = None,
    api_version: str = DEFAULT_REST_VERSION,
) -> Optional[dict]:
    instance_url = instance_url.rstrip("/")
    if path.startswith("/"):
        path = path[1:]
    if not path.startswith("services/"):
        path = f"services/data/{api_version}/{path}"
    url = f"{instance_url}/{path}"
    payload = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=payload, method=method)
    req.add_header("Authorization", f"Bearer {access_token}")
    req.add_header("Accept", "application/json")
    if payload is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req) as resp:
            raw = resp.read()
            if not raw:
                return None
            return json.loads(raw.decode())
    except urllib.error.HTTPError as e:
        err_raw = e.fp.read().decode("utf-8", errors="replace") if e.fp else ""
        try:
            err = json.loads(err_raw)
            if isinstance(err, list):
                msg = "; ".join(str(x.get("message", x)) for x in err)
            else:
                msg = err.get("message") or err.get("error_description") or err_raw
        except json.JSONDecodeError:
            msg = err_raw or e.reason
        raise RuntimeError(f"Salesforce REST {method} HTTP {e.code}: {msg}") from None


def create_job_record(
    instance_url: str,
    access_token: str,
    job_object_name: str,
    fields: dict,
    *,
    api_version: str = DEFAULT_REST_VERSION,
) -> dict:
    """POST /sobjects/{name}/ — returns JSON including id."""
    out = rest_json(
        instance_url,
        access_token,
        "POST",
        f"sobjects/{job_object_name.strip()}",
        body=fields,
        api_version=api_version,
    )
    return out or {}


def create_account_record(
    instance_url: str,
    access_token: str,
    fields: dict,
    *,
    api_version: str = DEFAULT_REST_VERSION,
) -> dict:
    """POST /sobjects/Account/ — returns JSON including id."""
    out = rest_json(
        instance_url,
        access_token,
        "POST",
        "sobjects/Account",
        body=fields,
        api_version=api_version,
    )
    return out or {}


def describe_sobject(
    instance_url: str,
    access_token: str,
    sobject_name: str,
    *,
    api_version: str = DEFAULT_REST_VERSION,
) -> dict:
    out = rest_json(
        instance_url,
        access_token,
        "GET",
        f"sobjects/{sobject_name.strip()}/describe",
        api_version=api_version,
    )
    return out or {}


def filter_createable_fields(describe: dict, fields: dict) -> dict:
    creatable = {
        f["name"]
        for f in describe.get("fields", [])
        if f.get("createable") and not f.get("deprecatedAndHidden")
    }
    out: dict = {}
    skipped: list[str] = []
    for k, v in fields.items():
        if v is None or v == "":
            continue
        if k in creatable:
            out[k] = v
        else:
            skipped.append(k)
    if skipped:
        print("Skipped (not createable on object):", ", ".join(skipped))
    return out


def is_salesforce_deleted_entity_error(exc: BaseException) -> bool:
    """
    True when REST error text indicates a lookup target is deleted (recycle bin / hard delete).

    Typical message: ``entity is deleted`` on POST/PATCH when ``Job_Worksite_Location_1__c`` or
    ``Job_Account__c`` references an invalid Id.
    """
    msg = str(exc).lower()
    if "entity is deleted" in msg:
        return True
    compact = msg.replace(" ", "").replace("_", "")
    return "entityisdeleted" in compact or "isdeleted" in compact and "deleted" in msg


def filter_updateable_fields(describe: dict, fields: dict) -> dict:
    updateable = {
        f["name"]
        for f in describe.get("fields", [])
        if f.get("updateable") and not f.get("deprecatedAndHidden")
    }
    out: dict = {}
    skipped: list[str] = []
    for k, v in fields.items():
        if v is None or v == "":
            continue
        if k in updateable:
            out[k] = v
        else:
            skipped.append(k)
    if skipped:
        print("Skipped (not updateable on object):", ", ".join(skipped))
    return out


def update_job_record(
    instance_url: str,
    access_token: str,
    job_object_name: str,
    record_id: str,
    fields: dict,
    *,
    api_version: str = DEFAULT_REST_VERSION,
) -> dict:
    """PATCH /sobjects/{name}/{id}/ — returns empty dict on 204."""
    oid = job_object_name.strip()
    rid = record_id.strip()
    out = rest_json(
        instance_url,
        access_token,
        "PATCH",
        f"sobjects/{oid}/{rid}",
        body=fields,
        api_version=api_version,
    )
    return out or {}
