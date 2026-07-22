"""API source — Jira Cloud REST v3.

The client here is real: it builds the URL, paginates, handles the status errors
and maps the response into `RawIssue`. The only piece swapped for a stub is the
**transport** — the layer that actually speaks HTTP. That means the parsing and
pagination path already runs for real today, against a fixture with the exact
shape the API returns; going to production is swapping the transport, not
rewriting the client.

To go live:
    1. [jira.api] transport = "http"   in config/config.toml
    2. export JIRA_API_TOKEN=...       (the token never lives in the config)

Endpoint: GET /rest/api/3/search/jql — the classic `/rest/api/3/search` was
deprecated by Atlassian; the new one paginates by `nextPageToken`, not `startAt`.
"""

from __future__ import annotations

import base64
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Protocol

from ..config import Config
from ..jira import JiraError, RawIssue

SEARCH_PATH = "/rest/api/3/search/jql"
MAX_PAGES = 100  # guard against a pagination loop


# --------------------------------------------------------------------------- #
# Transport — the only piece that changes between stub and production
# --------------------------------------------------------------------------- #


class Transport(Protocol):
    def get(self, url: str, headers: dict[str, str], timeout: float) -> dict: ...


class StubTransport:
    """Returns a fixture in the API's exact shape, paginating for real.

    Pagination is simulated by slicing the fixture into pages of `page_size` and
    emitting `nextPageToken`/`isLast` the way the real API does — it is the part
    of the client that breaks most when going to production, so it runs from day
    one.
    """

    def __init__(self, fixture: Path, page_size: int):
        if not fixture.exists():
            raise JiraError(
                f"stub fixture not found: {fixture}\n"
                f"  Point [jira.api] stub_file at a valid JSON, or use transport = \"http\"."
            )
        try:
            payload = json.loads(fixture.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise JiraError(f"stub fixture is not valid JSON ({fixture}): {exc}") from None
        self.issues = payload.get("issues", [])
        self.page_size = max(1, page_size)
        self.fixture = fixture
        self.calls = 0

    def get(self, url: str, headers: dict[str, str], timeout: float) -> dict:
        self.calls += 1
        query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        token = query.get("nextPageToken", ["0"])[0]
        try:
            offset = int(token)
        except ValueError:
            raise JiraError(f"stub got an invalid nextPageToken: {token!r}") from None

        page = self.issues[offset:offset + self.page_size]
        next_offset = offset + len(page)
        is_last = next_offset >= len(self.issues)
        body: dict = {"issues": page, "isLast": is_last}
        if not is_last:
            body["nextPageToken"] = str(next_offset)
        return body


class HttpTransport:
    """Production transport. Basic auth with the Atlassian API token."""

    def __init__(self, email: str, token: str, retries: int = 3):
        self.email = email
        self.token = token
        self.retries = retries

    def _auth_header(self) -> str:
        pair = f"{self.email}:{self.token}".encode()
        return "Basic " + base64.b64encode(pair).decode()

    def get(self, url: str, headers: dict[str, str], timeout: float) -> dict:
        request = urllib.request.Request(url, method="GET")
        for name, value in {**headers, "Authorization": self._auth_header()}.items():
            request.add_header(name, value)

        last_error: Exception | None = None
        for attempt in range(self.retries):
            try:
                with urllib.request.urlopen(request, timeout=timeout) as response:
                    return json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                detail = _http_error_detail(exc)
                if exc.code == 401:
                    raise JiraError(
                        "Jira refused the credentials (401). Check the e-mail in "
                        "[jira.api] and whether the token in the env var is still valid."
                    ) from None
                if exc.code == 403:
                    raise JiraError(
                        f"Jira denied access (403) — the account cannot see this "
                        f"project. {detail}"
                    ) from None
                if exc.code == 400:
                    raise JiraError(f"Jira rejected the JQL (400). {detail}") from None
                if exc.code == 429 or exc.code >= 500:
                    # Rate limit and server errors are transient: wait and try again.
                    last_error = exc
                    wait = float(exc.headers.get("Retry-After") or 2 ** attempt)
                    if attempt < self.retries - 1:
                        time.sleep(min(wait, 30))
                        continue
                raise JiraError(f"Jira returned HTTP {exc.code}. {detail}") from None
            except urllib.error.URLError as exc:
                last_error = exc
                if attempt < self.retries - 1:
                    time.sleep(2 ** attempt)
                    continue
                raise JiraError(f"could not talk to Jira: {exc.reason}") from None
            except json.JSONDecodeError as exc:
                raise JiraError(f"Jira response is not valid JSON: {exc}") from None

        raise JiraError(f"gave up after {self.retries} attempts: {last_error}")


def _http_error_detail(exc: urllib.error.HTTPError) -> str:
    try:
        body = json.loads(exc.read().decode("utf-8"))
    except Exception:
        return ""
    messages = body.get("errorMessages") or []
    errors = body.get("errors") or {}
    parts = list(messages) + [f"{k}: {v}" for k, v in errors.items()]
    return " ".join(parts)


# --------------------------------------------------------------------------- #
# Client
# --------------------------------------------------------------------------- #


def build_transport(cfg: Config) -> Transport:
    api = cfg.jira.api
    if api.transport == "stub":
        return StubTransport(cfg.resolve(api.stub_file), api.page_size)

    token = os.environ.get(api.token_env, "").strip()
    if not token:
        raise JiraError(
            f"transport = \"http\" but the environment variable {api.token_env} is empty.\n"
            f"  Get a token at https://id.atlassian.com/manage-profile/security/api-tokens\n"
            f"  and run:  export {api.token_env}='your-token'"
        )
    if not api.email:
        raise JiraError('transport = "http" requires [jira.api] email to be filled in')
    if not api.base_url:
        raise JiraError('transport = "http" requires [jira.api] base_url to be filled in')
    return HttpTransport(api.email, token)


def _fields(cfg: Config) -> list[str]:
    base = ["summary", "status", "assignee", "priority", "labels", "duedate"]
    extra = cfg.jira.api.fields.get("estimate")
    if extra:
        base.append(extra)
    return base


def fetch_raw(cfg: Config, transport: Transport | None = None) -> list[dict]:
    """Paginates the search and returns the raw issues, as the API delivers them."""
    api = cfg.jira.api
    transport = transport or build_transport(cfg)
    headers = {"Accept": "application/json", "User-Agent": "dpe-capacity/0.1"}

    issues: list[dict] = []
    next_token: str | None = None
    for page in range(MAX_PAGES):
        params = {
            "jql": api.jql,
            "maxResults": str(api.page_size),
            "fields": ",".join(_fields(cfg)),
        }
        if next_token is not None:
            params["nextPageToken"] = next_token
        url = f"{api.base_url.rstrip('/')}{SEARCH_PATH}?{urllib.parse.urlencode(params)}"

        body = transport.get(url, headers, api.timeout_seconds)
        batch = body.get("issues")
        if batch is None:
            raise JiraError(
                f"Jira response without the 'issues' field (page {page + 1}). "
                f"Keys received: {sorted(body)}"
            )
        issues.extend(batch)

        if body.get("isLast", True):
            break
        next_token = body.get("nextPageToken")
        if not next_token:
            break
    else:
        raise JiraError(
            f"pagination went past {MAX_PAGES} pages — the JQL is returning too "
            f"much, or the transport never signals isLast."
        )

    return issues


def to_raw_issue(cfg: Config, issue: dict) -> RawIssue:
    """Maps an issue from the v3 API into the neutral format."""
    key = issue.get("key")
    if not key:
        raise JiraError(f"issue without 'key' in the API response: {json.dumps(issue)[:200]}")
    fields = issue.get("fields") or {}

    def nested(name: str, attr: str) -> str | None:
        value = fields.get(name)
        return value.get(attr) if isinstance(value, dict) else None

    estimate_field = cfg.jira.api.fields.get("estimate")
    estimate = fields.get(estimate_field) if estimate_field else None

    labels = fields.get("labels") or []
    if not isinstance(labels, list):
        labels = []

    return RawIssue(
        key=key,
        summary=fields.get("summary") or "",
        status=nested("status", "name") or "",
        assignee=nested("assignee", "displayName"),
        priority=nested("priority", "name") or "",
        estimate=None if estimate is None else str(estimate),
        labels=[str(lb) for lb in labels],
        due=fields.get("duedate"),
        origin=f"API {cfg.jira.api.base_url}",
    )


def fetch(cfg: Config, transport: Transport | None = None) -> list[RawIssue]:
    return [to_raw_issue(cfg, issue) for issue in fetch_raw(cfg, transport)]


def describe(cfg: Config) -> str:
    api = cfg.jira.api
    if api.transport == "stub":
        return f'API (stub: {api.stub_file}) · JQL "{api.jql}"'
    return f'API {api.base_url} · JQL "{api.jql}"'
