"""Relaunch72's operator tools, carried by this connector (docs/operator-api.md, "Relaunch72 tools").

Webinars, the brand CRMs and the follow-up drafts live in Relaunch72. So that Claude runs them from the Group HQ
connector, this server relays: it asks Relaunch72's own operator endpoint which tools it offers and passes a call
through, server to server. Relaunch72 stays the single owner of those tools' schemas, checks and refusals; nothing about
them is copied here except the list of names known to change nothing.

Trust. The bearer is derived from the secret the two servers already share for the sign-in handoff (R72_SSO_SECRET),
under a fixed label, so there is no new secret and the bearer can never be mistaken for a handoff token. Every request
names the signed-in admin; Relaunch72 accepts it only for a login its handoff map ties to its operator account, so a
second Group HQ admin is offered nothing. The relay is off unless GHQ_OPERATOR_RELAUNCH72 is "enabled" (and Relaunch72
has its own switch, R72_OPERATOR_GROUP_HQ).

Nothing here retries: a relayed write is sent once. The bearer, the arguments and the results are never logged.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import re
import threading
import time
from dataclasses import dataclass
from typing import Any

import httpx

from .. import config

log = logging.getLogger("grouphq.operator")

PATH = "/api/operator/mcp"
LABEL = b"group-hq-operator-bearer/v1\x00"
ADMIN_HEADER = "X-Group-HQ-Admin"
PREFIX = "Relaunch72 brand workspace (Property Predator or Martin Howard Property). "
UNAVAILABLE = "Relaunch72 could not be reached just now, so nothing was sent to it and nothing changed. Try again shortly."
UNKNOWN = ("Relaunch72 did not give a readable answer, so it is not known whether this call took effect. Read the record "
           "again before repeating it.")
REFUSED = "Relaunch72 does not accept calls from this Group HQ admin; nothing changed."
WRITES_DISABLED = "writes are disabled on this deployment (GHQ_OPERATOR_WRITES is not \"enabled\")"

# Relaunch72 tools known to change nothing: the only ones offered while Group HQ's writes are off. A tool Relaunch72
# adds later is treated as a write until it is named here.
READ_ONLY = frozenset({
    "workspace_overview", "crm_contacts", "crm_pipeline", "crm_tasks", "crm_lead_360",
    "webinars_list", "webinar_get", "webinar_registrants", "webinar_results", "webinar_messages",
    "followups_overview", "followup_sequence_get", "followup_sequence_check",
    "owner_emails_overview", "owner_email_get", "owner_email_preview",
})
# The one name both servers use. Relaunch72's is the fuller tool (it returns what webinar_get and webinar_update need),
# so it replaces Group HQ's own for an admin the relay serves. Any other clash keeps Group HQ's tool.
REPLACES_LOCAL = frozenset({"webinars_list"})

NAME = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
MAX_TOOLS = 100
MAX_DESCRIPTION = 2000
MAX_SCHEMA_BYTES = 20_000
MAX_LISTING_BYTES = 512 * 1024
MAX_RESULT_BYTES = 2 * 1024 * 1024
LIST_TTL = 300.0          # how long a good tool list is reused
RETRY_AFTER = 30.0        # how long a failed attempt is remembered, so an outage does not slow every tools/list
LIST_TIMEOUT = httpx.Timeout(6.0, connect=3.0)
CALL_TIMEOUT = httpx.Timeout(30.0, connect=5.0)


class RelayUnavailable(Exception):
    """Relaunch72 did not give a usable answer. sent is False only when the request provably never left (no connection),
    which is the one case where a write certainly did not happen."""

    def __init__(self, *, sent: bool = True):
        super().__init__()
        self.sent = sent


class RelayRefused(Exception):
    """Relaunch72 does not relay for this admin (or the relay is switched off there)."""


@dataclass(frozen=True)
class RelayTool:
    name: str
    description: str
    input_schema: dict

    @property
    def write(self) -> bool:
        return self.name not in READ_ONLY

    def listing(self) -> dict:
        return {"name": self.name, "description": self.description, "inputSchema": self.input_schema}


def enabled() -> bool:
    return config.operator_relaunch72_enabled()


def bearer(secret: str | None = None) -> str:
    """base64url(HMAC-SHA256(shared secret, label)) without padding: what Relaunch72's deriveGroupHqOperatorToken gives."""
    key = (config.R72_SSO_SECRET if secret is None else secret).strip().encode()   # Relaunch72 trims its copy too
    return base64.urlsafe_b64encode(hmac.new(key, LABEL, hashlib.sha256).digest()).rstrip(b"=").decode()


def _client(timeout: httpx.Timeout) -> httpx.Client:
    return httpx.Client(timeout=timeout, follow_redirects=False)


def _send(email: str, payload: dict, timeout: httpx.Timeout, limit: int) -> Any:
    """One JSON-RPC request to Relaunch72 as this admin; its "result". Sent once, never retried, redirects not followed."""
    try:
        with _client(timeout) as http:
            response = http.post(f"https://{config.R72_PP_HOST}{PATH}", json=payload, headers={
                "Authorization": f"Bearer {bearer()}", ADMIN_HEADER: email, "Accept": "application/json",
                "User-Agent": "GroupHQ/1.0"})
    except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
        raise RelayUnavailable(sent=False) from exc
    except httpx.HTTPError as exc:
        raise RelayUnavailable() from exc
    if response.status_code in (401, 403):
        raise RelayRefused()
    if response.status_code != 200 or len(response.content) > limit:
        raise RelayUnavailable()
    try:
        body = response.json()
    except ValueError as exc:
        raise RelayUnavailable() from exc
    if not isinstance(body, dict) or body.get("jsonrpc") != "2.0" or "result" not in body:
        raise RelayUnavailable()
    return body["result"]


def _parse_listing(result: Any) -> tuple[RelayTool, ...]:
    raw = result.get("tools") if isinstance(result, dict) else None
    if not isinstance(raw, list) or len(raw) > MAX_TOOLS:
        raise RelayUnavailable()
    found: dict[str, RelayTool] = {}
    for item in raw:
        if not isinstance(item, dict):
            raise RelayUnavailable()
        name, description, schema = item.get("name"), item.get("description"), item.get("inputSchema")
        if not isinstance(name, str) or not NAME.fullmatch(name) or name in found:
            raise RelayUnavailable()
        if not isinstance(description, str) or not isinstance(schema, dict) or schema.get("type") != "object":
            raise RelayUnavailable()
        if len(json.dumps(schema)) > MAX_SCHEMA_BYTES:
            raise RelayUnavailable()
        found[name] = RelayTool(name, (PREFIX + description)[:MAX_DESCRIPTION], schema)
    return tuple(found.values())


# email -> (monotonic expiry, tools or None). None remembers a failed or refused attempt for RETRY_AFTER seconds.
_cache: dict[str, tuple[float, tuple[RelayTool, ...] | None]] = {}
_lock = threading.Lock()


def forget() -> None:
    """Drop every remembered tool list (tests, and nothing else needs it)."""
    with _lock:
        _cache.clear()


def offered(email: str) -> tuple[RelayTool, ...] | None:
    """Every tool Relaunch72 offers this admin, or None while it cannot be asked. () when it relays nothing for them."""
    if not enabled():
        return ()
    key = email.strip().lower()
    now = time.monotonic()
    with _lock:
        hit = _cache.get(key)
    if hit is not None and hit[0] > now:
        return hit[1]
    try:
        found: tuple[RelayTool, ...] | None = _parse_listing(
            _send(key, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, LIST_TIMEOUT, MAX_LISTING_BYTES))
        ttl = LIST_TTL
    except RelayRefused:
        found, ttl = (), LIST_TTL
    except RelayUnavailable:
        log.warning("relaunch72 relay: the tool list could not be read")
        found, ttl = None, RETRY_AFTER
    with _lock:
        if len(_cache) > 200:
            _cache.clear()
        _cache[key] = (now + ttl, found)
    return found


def available(email: str, *, writes_enabled: bool) -> list[RelayTool]:
    """What tools/list adds for this admin: the read tools, and the rest only while this deployment's writes are on."""
    return [tool for tool in offered(email) or () if writes_enabled or not tool.write]


def call(email: str, name: str, arguments: dict) -> dict:
    """Pass one tools/call through and return Relaunch72's MCP result (content and isError) once it has been checked."""
    result = _send(email.strip().lower(), {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                           "params": {"name": name, "arguments": arguments}}, CALL_TIMEOUT, MAX_RESULT_BYTES)
    content = result.get("content") if isinstance(result, dict) else None
    if not isinstance(content, list) or not 1 <= len(content) <= 20 or not isinstance(result.get("isError", False), bool):
        raise RelayUnavailable()
    if not all(isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str) for part in content):
        raise RelayUnavailable()
    return {"content": [{"type": "text", "text": part["text"]} for part in content], "isError": bool(result.get("isError", False))}
