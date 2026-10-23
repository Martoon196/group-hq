"""Google's service-account token, minted through google-auth over this app's own httpx client.

GA4 and Search Console share one service account (GOOGLE_SA_JSON: the key file's JSON as one environment variable,
so fromService can copy it to the crons). google-auth signs the JWT assertion and exchanges it at Google's token
endpoint; HttpxRequest is google-auth's transport interface over httpx, so no second HTTP library is needed and the
tests can answer the token call with respx like any other. The token endpoint is always Google's own, whatever the
key file says, so a doctored file cannot send the signed assertion elsewhere. Nothing here logs the key, the
assertion or the token; a dry run plans the exchange with the assertion redacted and never signs anything.
"""
from __future__ import annotations

import json
from typing import Any

import httpx
from google.auth import exceptions as google_exceptions
from google.auth import transport as google_transport
from google.oauth2 import service_account

from ...connectors.base import ConnectorError
from .base import REDACTED_VALUE, MetricCollector, scrub

TOKEN_URI = "https://oauth2.googleapis.com/token"
REQUIRED_KEYS = ("type", "client_email", "private_key")


def parse_service_account(raw: str) -> dict | None:
    """The key file as a dict when it looks like a service-account key, else None (its content is never echoed)."""
    try:
        info = json.loads(raw or "")
    except (TypeError, ValueError):
        return None
    if not isinstance(info, dict) or info.get("type") != "service_account":
        return None
    if not all(isinstance(info.get(key), str) and info[key] for key in REQUIRED_KEYS):
        return None
    return {**info, "token_uri": TOKEN_URI}


class _Response(google_transport.Response):
    def __init__(self, resp: httpx.Response):
        self._resp = resp

    @property
    def status(self) -> int:
        return self._resp.status_code

    @property
    def headers(self):
        return dict(self._resp.headers)

    @property
    def data(self) -> bytes:
        return self._resp.content


class HttpxRequest(google_transport.Request):
    """google-auth's transport callable, backed by an httpx client."""

    def __init__(self, client: httpx.Client, timeout: float = 10.0):
        self.client = client
        self.timeout = timeout

    def __call__(self, url, method="GET", body=None, headers=None, timeout=None, **kwargs):
        try:
            resp = self.client.request(method, url, content=body, headers=headers or {},
                                       timeout=timeout or self.timeout)
        except httpx.HTTPError as exc:
            raise google_exceptions.TransportError(f"{type(exc).__name__} reaching {url}") from exc
        return _Response(resp)


def token_plan(scope: str) -> dict:
    """The token exchange as a dry-run plan shows it: nothing signed, nothing sent."""
    return {"method": "POST", "url": TOKEN_URI, "params": None,
            "json": {"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "scope": scope,
                     "assertion": f"{REDACTED_VALUE} (a JWT signed with the service-account key)"},
            "headers": {}}


def mint_token(info: dict, scope: str, client: httpx.Client, *, timeout: float = 10.0) -> str:
    """A short-lived access token for the scope. Raises ConnectorError(auth_failed) with scrubbed text."""
    secrets: tuple[str, ...] = (str(info.get("private_key", "")), str(info.get("private_key_id", "")))
    try:
        credentials = service_account.Credentials.from_service_account_info(info, scopes=[scope])
        credentials.refresh(HttpxRequest(client, timeout))
    except google_exceptions.RefreshError as exc:
        raise ConnectorError("auth_failed", detail=scrub(_refresh_text(exc), secrets)[:300]) from None
    except google_exceptions.TransportError as exc:
        raise ConnectorError("unreachable", detail=scrub(str(exc), secrets)[:300]) from None
    except Exception as exc:   # noqa: BLE001  (a key that parses but is not an RSA key raises pyasn1's own error, and
        # google-auth's signer can raise others: the type name only, never the text, which may quote the key)
        raise ConnectorError("bad_credentials", detail=f"GOOGLE_SA_JSON could not be used: {type(exc).__name__}") from None
    token = credentials.token
    if not token:
        raise ConnectorError("auth_failed", detail="Google returned no access token")
    return str(token)


def _refresh_text(exc: Exception) -> str:
    args: Any = getattr(exc, "args", ())
    if args and isinstance(args[0], str):
        return args[0]
    return type(exc).__name__


class GoogleCollector(MetricCollector):
    """The shared half of GA4 and Search Console: one service account, a token minted on the first request."""
    scope = ""
    mapping_env = ""
    timeout_seconds = 30.0

    def __init__(self, base_url: str, sa_json: str, mapping: dict[str, str], *, dry_run: bool = False,
                 timeout: float | None = None):
        super().__init__(base_url, "", dry_run=dry_run, timeout=timeout)
        self._sa_set = bool((sa_json or "").strip())
        self._info = parse_service_account(sa_json)
        self.mapping = mapping

    @property
    def configured(self) -> bool:
        return bool(self._info and self.mapping)

    def missing_env(self) -> list[str]:
        missing = []
        if not self._sa_set:
            missing.append("GOOGLE_SA_JSON")
        elif self._info is None:
            missing.append("GOOGLE_SA_JSON (not a service-account key file)")
        if not self.mapping:
            missing.append(self.mapping_env)
        return missing

    def secrets(self) -> tuple[str, ...]:
        info = self._info or {}
        return (self._token, str(info.get("private_key", "")), str(info.get("private_key_id", "")))

    def ensure_token(self) -> None:
        """Mint the access token before the first request (plan it, in dry run)."""
        if self.dry_run:
            if not any(p.get("url") == TOKEN_URI for p in self.plans):
                self.record(token_plan(self.scope))
            return
        if not self.configured:
            raise ConnectorError("not_configured", detail=f"{self.source}: missing {', '.join(self.missing_env())}")
        if not self._token:
            self.record(token_plan(self.scope))
            self._token = mint_token(self._info or {}, self.scope, self.client(), timeout=self.timeout)
