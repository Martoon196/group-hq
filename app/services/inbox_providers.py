"""Shared inbox transports. No subscription broker or second CRM.

Zernio's account binding, paths and response contract follow Relaunch72's
public-social-outbound/zernio-messaging-client.ts at 459554640eeddae9c.
Matrix is the self-hosted transport for explicitly mapped personal bridge rooms.
Credentials and homeserver origins come only from operator configuration.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import quote, urlsplit

import httpx

BRANDS = {"mhp": "Martin Howard Property", "pp": "Property Predator", "kp": "Key Properties UK", "piuk": "Prop Invest UK",
          "hnh": "Hello New Home", "ordris": "Ordris", "group": "Group"}
CHANNELS = {"facebook": "Facebook", "instagram": "Instagram", "linkedin": "LinkedIn",
            "twitter": "X", "tiktok": "TikTok", "youtube": "YouTube", "whatsapp": "WhatsApp"}
MAX_BYTES = 1_048_576


class InboxError(Exception):
    """Bounded reason codes only; upstream bodies and credentials never reach the UI."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def opaque(value) -> str:
    if (not isinstance(value, str) or not 1 <= len(value) <= 500 or value in (".", "..")
            or any(ord(c) < 33 or ord(c) > 126 for c in value)):
        raise InboxError("invalid_provider_response")
    return value


def clean(value, maximum=65536) -> str:
    if not isinstance(value, str) or len(value) > maximum:
        raise InboxError("invalid_provider_response")
    return value


def rows(body, key, limit=100):
    value = body.get(key)
    if not isinstance(value, list) or len(value) > limit or any(not isinstance(x, dict) for x in value):
        raise InboxError("invalid_provider_response")
    return value


def object_field(body, key):
    value = body.get(key) or {}
    if not isinstance(value, dict):
        raise InboxError("invalid_provider_response")
    return value


def next_cursor(body):
    cursor = object_field(body, "pagination").get("nextCursor")
    if cursor is not None and (not isinstance(cursor, str) or not 1 <= len(cursor) <= 4096):
        raise InboxError("invalid_provider_response")
    return cursor


@dataclass(frozen=True)
class Account:
    key: str
    brand: str
    channel: str
    label: str
    provider: str
    account_id: str
    profile_id: str = ""
    token: str = field(default="", repr=False)
    origin: str = ""
    user_id: str = ""
    rooms: tuple = ()
    can_reply: bool = False
    owner_email: str = ""

    @property
    def configured(self):
        return bool(self.token) and (self.provider in ("zernio", "local", "r72_whatsapp") or bool(self.origin and self.user_id and self.rooms))

    @property
    def binding(self):
        # Rotation, profile/account remapping or room changes invalidate an old composer.
        value = [self.key, self.brand, self.channel, self.provider, self.account_id, self.profile_id,
                 self.origin, self.user_id, self.rooms, self.can_reply, self.owner_email, hashlib.sha256(self.token.encode()).hexdigest()]
        return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()

    @property
    def identity(self):
        return hashlib.sha256(json.dumps([self.provider,self.channel,self.account_id,self.owner_email]).encode()).hexdigest()


def load_accounts(raw: str, environ: dict) -> tuple[Account, ...]:
    """Explicit operator allowlist. No credentials or URLs accepted from an inbox form."""
    try:
        values = json.loads(raw or "[]")
        if not isinstance(values, list) or len(values) > 40:
            raise ValueError()
        result, seen = [], set()
        for row in values:
            key = row["key"]
            provider, channel = row["provider"], row["channel"]
            if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", key) or key in seen:
                raise ValueError()
            if row["brand"] not in BRANDS or channel not in CHANNELS:
                raise ValueError()
            if provider == "zernio" and channel not in ("facebook", "instagram", "twitter"):
                raise ValueError()
            if provider == "matrix" and channel not in ("facebook", "linkedin"):
                raise ValueError()
            if provider == "local" and channel not in ("facebook", "linkedin"):
                raise ValueError()
            if provider not in ("zernio", "matrix", "local"):
                raise ValueError()
            owner_email = row.get("owner_email", "").strip().lower()
            if provider in ("local", "matrix") and not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", owner_email):
                raise ValueError()
            profile_id = row.get("profile_id", "")
            if provider == "zernio" and not re.fullmatch(r"[A-Za-z0-9_-]{3,128}", profile_id):
                raise ValueError()
            env_key = row["credential_env"]
            if not re.fullmatch(r"GHQ_INBOX_[A-Z0-9_]{1,80}", env_key):
                raise ValueError()
            origin = row.get("origin", "")
            if provider == "matrix":
                parts = urlsplit(origin)
                if (parts.scheme != "https" or not parts.hostname or parts.username or parts.password
                        or parts.path not in ("", "/") or parts.query or parts.fragment):
                    raise ValueError()
                if not opaque(row["user_id"]).startswith("@"):
                    raise ValueError()
            rooms = row.get("rooms", [])
            if not isinstance(rooms, list) or len(rooms) > 200:
                raise ValueError()
            for room in rooms:
                if (not isinstance(room, dict) or not opaque(room["id"]).startswith("!")
                        or not clean(room["label"], 300) or not opaque(room["bridge_bot"]).startswith("@")):
                    raise ValueError()
                clean(room["bridge_state_key"], 1000)
                opaque(room["protocol_id"])
            if len({r["id"] for r in rooms}) != len(rooms):
                raise ValueError()
            result.append(Account(key=key, brand=row["brand"], channel=channel, label=clean(row["label"], 200),
                provider=provider, account_id=opaque(row["account_id"]), profile_id=profile_id,
                token=environ.get(env_key, ""), origin=origin.rstrip("/"), user_id=row.get("user_id", ""),
                rooms=tuple(rooms), can_reply=row.get("can_reply") is True, owner_email=owner_email))
            seen.add(key)
        return tuple(result)
    except (ValueError, TypeError, KeyError, InboxError, AttributeError):
        raise InboxError("invalid_account_configuration") from None


class Transport:
    def __init__(self, account: Account, transport=None):
        self.account = account
        self.transport = transport

    def validate_send(self, thread_id, body, request_id):
        opaque(thread_id)
        if not self.account.can_reply:
            raise InboxError("reply_not_enabled")
        if (not isinstance(body, str) or not body.strip() or len(body.encode()) > 10000
                or not re.fullmatch(r"ghq-[a-z0-9-]{36}", request_id)):
            raise InboxError("unbound_target")

    def request(self, method, path, *, params=None, payload=None, request_id=None, array=False):
        a = self.account
        if not a.configured:
            raise InboxError("setup_needed")
        headers = {"Authorization": "Bearer " + a.token, "Accept": "application/json"}
        if request_id:
            headers["Idempotency-Key"] = request_id
        origin = "https://zernio.com" if a.provider == "zernio" else a.origin
        writing = method != "GET"
        try:
            with httpx.Client(timeout=12, follow_redirects=False, transport=self.transport, trust_env=False) as client:
                with client.stream(method, origin + path, params=params, json=payload, headers=headers) as response:
                    if response.status_code >= 500 or response.status_code == 408:
                        raise InboxError("outcome_unknown" if writing else "provider_unavailable")
                    if writing and 300 <= response.status_code < 400:
                        raise InboxError("outcome_unknown")
                    if response.status_code >= 300:
                        raise InboxError({401: "sign_in_needed", 403: "permission_needed", 429: "rate_limited"}
                                         .get(response.status_code, "provider_rejected"))
                    chunks, size = [], 0
                    for chunk in response.iter_bytes():
                        size += len(chunk)
                        if size > MAX_BYTES:
                            raise ValueError()
                        chunks.append(chunk)
                    body = json.loads(b"".join(chunks))
                    if not isinstance(body, list if array else dict):
                        raise ValueError()
                    return body
        except httpx.HTTPError:
            raise InboxError("outcome_unknown" if writing else "provider_unavailable") from None
        except (ValueError, TypeError):
            raise InboxError("outcome_unknown" if writing else "invalid_provider_response") from None


class Zernio(Transport):
    def _bound(self, row, thread_id=None):
        if (row.get("accountId") != self.account.account_id or row.get("platform") != self.account.channel
                or (thread_id is not None and row.get("conversationId") != thread_id)):
            raise InboxError("unbound_target")

    def conversations(self):
        a = self.account
        body = self.request("GET", "/api/v1/inbox/conversations", params={
            "profileId": a.profile_id, "accountId": a.account_id, "limit": 100, "sortOrder": "desc"})
        result = []
        for item in rows(body, "data"):
            self._bound(item)
            result.append({"id": opaque(item.get("id")), "name": clean(item.get("participantName") or "Unnamed contact", 500),
                "participant_id": opaque(item.get("participantId")), "preview": clean(item.get("lastMessage") or ""),
                "updated_at": clean(item.get("updatedTime") or "", 100), "unread": item.get("unreadCount", 0)})
        meta = object_field(body, "meta")
        if meta.get("failedAccounts") or meta.get("accountsFailed"):
            raise InboxError("provider_unavailable")
        return result, object_field(body, "pagination").get("hasMore") is True

    def messages(self, thread_id, cursor=None):
        params = {"accountId": self.account.account_id, "sortOrder": "desc", "limit": 100}
        if cursor:
            params["cursor"] = clean(cursor, 4096)
        body = self.request("GET", "/api/v1/inbox/conversations/" + quote(opaque(thread_id), safe="") + "/messages", params=params)
        result = []
        for item in rows(body, "messages"):
            self._bound(item, thread_id)
            direction = item.get("direction")
            if direction not in ("incoming", "outgoing"):
                raise InboxError("invalid_provider_response")
            result.append({"id": opaque(item.get("id")), "body": clean(item.get("message") or "[Attachment]"),
                "direction": direction, "name": clean(item.get("senderName") or "", 500),
                "occurred_at": clean(item.get("createdAt") or "", 100)})
        return list(reversed(result)), next_cursor(body)

    def send(self, thread_id, body, request_id):
        self.validate_send(thread_id, body, request_id)
        response = self.request("POST", "/api/v1/inbox/conversations/" + quote(opaque(thread_id), safe="") + "/messages",
            payload={"accountId": self.account.account_id, "message": body}, request_id=request_id)
        try:
            result = response.get("data", response)
            return opaque(result.get("messageId") or result.get("id"))
        except (InboxError, AttributeError):
            raise InboxError("outcome_unknown") from None


class Matrix(Transport):
    """Mapped bridge rooms only, never a general Matrix-room composer.

    A Matrix event acknowledgement is NOT proof of delivery to Facebook/LinkedIn.
    Encrypted Matrix rooms require an encryption-capable client, not a fallback to
    plaintext. Remote-platform encryption remains the bridge's responsibility.
    """
    def room(self, room_id):
        room = next((r for r in self.account.rooms if r["id"] == room_id), None)
        if room is None:
            raise InboxError("unbound_target")
        base = "/_matrix/client/v3/rooms/" + quote(opaque(room_id), safe="")
        identity = self.request("GET", "/_matrix/client/v3/account/whoami")
        if identity.get("user_id") != self.account.user_id:
            raise InboxError("unbound_target")
        state = self.request("GET", base + "/state", array=True)
        if any(not isinstance(event, dict) or not isinstance(event.get("content"), dict) for event in state):
            raise InboxError("invalid_provider_response")
        if any(e.get("type") == "m.room.encryption" for e in state):
            raise InboxError("encrypted_room_client_needed")
        own_membership = next((e.get("content", {}).get("membership") for e in state
            if e.get("type") == "m.room.member" and e.get("state_key") == self.account.user_id), None)
        bridged = any(e.get("type") == "m.bridge" and e.get("state_key") == room["bridge_state_key"]
            and e.get("sender") == room["bridge_bot"]
            and isinstance(e["content"].get("protocol"), dict)
            and e["content"]["protocol"].get("id") == room["protocol_id"] for e in state)
        if own_membership != "join" or not bridged:
            raise InboxError("unbound_target")
        return room, base, state

    def conversations(self):
        result = []
        for mapping in self.account.rooms:
            room, _, _ = self.room(mapping["id"])
            result.append({"id": room["id"], "name": room["label"], "participant_id": room["id"],
                "preview": "Open conversation", "updated_at": "", "unread": None})
        return result, False

    def messages(self, thread_id, cursor=None):
        _, base, state = self.room(thread_id)
        params = {"dir": "b", "limit": 100}
        if cursor:
            params["from"] = clean(cursor, 4096)
        response = self.request("GET", base + "/messages", params=params)
        names = {e.get("state_key"): e.get("content", {}).get("displayname", "") for e in state
                 if e.get("type") == "m.room.member"}
        result = []
        for event in rows(response, "chunk"):
            if event.get("type") == "m.room.encrypted":
                raise InboxError("encrypted_room_client_needed")
            if event.get("type") != "m.room.message":
                continue
            content = event.get("content") or {}
            if not isinstance(content, dict):
                raise InboxError("invalid_provider_response")
            timestamp = event.get("origin_server_ts")
            if not isinstance(timestamp, int) or not 0 <= timestamp <= 253402300799000:
                raise InboxError("invalid_provider_response")
            result.append({"id": opaque(event.get("event_id")), "body": clean(content.get("body") or "[Attachment]"),
                "direction": "outgoing" if event.get("sender") == self.account.user_id else "incoming",
                "name": clean(names.get(event.get("sender")) or "", 500),
                "occurred_at": datetime.fromtimestamp(timestamp / 1000, timezone.utc).isoformat()})
        return list(reversed(result)), response.get("end")

    def send(self, thread_id, body, request_id):
        self.validate_send(thread_id, body, request_id)
        _, base, _ = self.room(thread_id)
        response = self.request("PUT", base + "/send/m.room.message/" + quote(request_id, safe=""),
            payload={"msgtype": "m.text", "body": body})
        try:
            return opaque(response.get("event_id"))
        except InboxError:
            raise InboxError("outcome_unknown") from None


def client(account, transport=None):
    if getattr(account, "surface", None) == "linkedin_public_comments":
        from .linkedin_comments_provider import LinkedInComments
        return LinkedInComments(account, transport)
    if account.provider == "zernio":
        return Zernio(account, transport)
    if account.provider == "matrix":
        return Matrix(account, transport)
    raise InboxError("unsupported_provider")
