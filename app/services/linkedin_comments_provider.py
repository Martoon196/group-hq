"""LinkedIn company PUBLIC comments through the existing Zernio transport.

This is deliberately not a conversation/DM adapter. Each reply targets an
observed comment on a post belonging to an explicitly configured company.
"""
import base64
import hashlib
import json
import re
from dataclasses import dataclass
from urllib.parse import quote

from .inbox_providers import Account, InboxError, Transport, clean, load_accounts, object_field, opaque, rows

SURFACE = "linkedin_public_comments"
COMPANIES = {
    "pp": ("6a98ae4677555aae01c8ec39", "6a98ac5a647e96173efa7ed8"),
    "kp": ("6aebc74669f0b74d1604ce03", "6acfc2c1347304c5e294a88a"),
}


@dataclass(frozen=True)
class PublicCommentAccount(Account):
    @property
    def surface(self):
        return SURFACE

    @property
    def binding(self):
        return hashlib.sha256((SURFACE + ":" + super().binding).encode()).hexdigest()

    @property
    def identity(self):
        return hashlib.sha256((SURFACE + ":" + super().identity).encode()).hexdigest()


def load_public_accounts(raw, environ):
    """Separate operator allowlist; personal LinkedIn accounts are excluded."""
    try:
        values = json.loads(raw or "[]")
        if not isinstance(values, list) or len(values) > 2:
            raise ValueError()
        result, seen = [], set()
        for row in values:
            brand = row["brand"]
            if (brand not in COMPANIES or brand in seen or row["key"] != brand + "-linkedin-public"
                    or (row["account_id"], row["profile_id"]) != COMPANIES[brand]
                    or row.get("channel") != "linkedin" or row.get("provider") != "zernio"):
                raise ValueError()
            owner = row["owner_email"].strip().lower()
            if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", owner):
                raise ValueError()
            env_key = row["credential_env"]
            if not re.fullmatch(r"GHQ_INBOX_[A-Z0-9_]{1,80}", env_key):
                raise ValueError()
            token = environ.get(env_key, "")
            if env_key == "GHQ_INBOX_REUSE_PROFILE":
                # An explicitly enabled company may reuse its own approved inbox credential.
                private = load_accounts(environ.get("GHQ_INBOX_ACCOUNTS", "[]"), environ)
                tokens = {a.token for a in private if a.provider == "zernio" and a.brand == brand
                          and a.profile_id == row["profile_id"] and a.token and
                          (not a.owner_email or a.owner_email == owner)}
                token = tokens.pop() if len(tokens) == 1 else ""
            result.append(PublicCommentAccount(key=row["key"], brand=brand, channel="linkedin",
                label=clean(row["label"], 200), provider="zernio", account_id=row["account_id"],
                profile_id=row["profile_id"], token=token,
                owner_email=owner, can_reply=row.get("can_reply") is True))
            seen.add(brand)
        return tuple(result)
    except (ValueError, TypeError, KeyError, AttributeError, InboxError):
        raise InboxError("invalid_account_configuration") from None


def reply_target(post_id, comment_id):
    packed = json.dumps([SURFACE, opaque(post_id), opaque(comment_id)], separators=(",", ":")).encode()
    return opaque(base64.urlsafe_b64encode(packed).decode().rstrip("="))


def unpack_target(value):
    try:
        opaque(value)
        data = json.loads(base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True))
        if not isinstance(data, list) or len(data) != 3 or data[0] != SURFACE:
            raise ValueError()
        if reply_target(data[1], data[2]) != value:
            raise ValueError()
        return opaque(data[1]), opaque(data[2])
    except (ValueError, TypeError, UnicodeError, InboxError):
        raise InboxError("unbound_target") from None


def page_cursor(body, key):
    pagination = object_field(body, "pagination")
    more = pagination.get("hasMore", False)
    cursor = pagination.get(key)
    if not isinstance(more, bool) or (cursor is not None and (not isinstance(cursor, str) or not 1 <= len(cursor) <= 4096)):
        raise InboxError("invalid_provider_response")
    if more and cursor is None:
        raise InboxError("invalid_provider_response")
    return cursor if more else None


class LinkedInComments(Transport):
    def __init__(self, account, transport=None):
        if (not isinstance(account, PublicCommentAccount) or account.provider != "zernio"
                or account.channel != "linkedin" or COMPANIES.get(account.brand) != (account.account_id, account.profile_id)):
            raise InboxError("unbound_target")
        super().__init__(account, transport)

    def posts(self, cursor=None):
        params = {"accountId": self.account.account_id, "profileId": self.account.profile_id,
                  "platform": "linkedin", "limit": 100, "sortBy": "date", "sortOrder": "desc"}
        if cursor:
            params["cursor"] = clean(cursor, 4096)
        body = self.request("GET", "/api/v1/inbox/comments", params=params)
        meta = object_field(body, "meta")
        if meta.get("accountsFailed") or meta.get("failedAccounts") or meta.get("accountsSkipped"):
            raise InboxError("provider_unavailable")
        result = []
        for row in rows(body, "data"):
            if (row.get("accountId") != self.account.account_id or row.get("platform") != "linkedin"
                    or row.get("isAd", False) is not False):
                raise InboxError("unbound_target")
            result.append({"id": opaque(row.get("id")), "body": clean(row.get("content") or ""),
                           "occurred_at": clean(row.get("createdTime") or "", 100)})
        return result, page_cursor(body, "nextCursor")

    def comments(self, post_id, cursor=None):
        post_id = opaque(post_id)
        params = {"accountId": self.account.account_id, "limit": 100}
        if cursor:
            params["cursor"] = clean(cursor, 4096)
        body = self.request("GET", "/api/v1/inbox/comments/" + quote(post_id, safe=""), params=params)
        meta = object_field(body, "meta")
        if (meta.get("accountId") != self.account.account_id or meta.get("platform") != "linkedin"
                or meta.get("postId") != post_id):
            raise InboxError("unbound_target")
        result, count = [], [0]

        def visit(row, depth):
            count[0] += 1
            if count[0] > 500 or depth > 5:
                raise InboxError("invalid_provider_response")
            if row.get("platform") != "linkedin":
                raise InboxError("unbound_target")
            for key, expected in (("accountId", self.account.account_id), ("postId", post_id)):
                if key in row and row[key] != expected:
                    raise InboxError("unbound_target")
            author = object_field(row, "from")
            can_reply = row.get("canReply")
            if not isinstance(can_reply, bool):
                raise InboxError("invalid_provider_response")
            result.append({"id": opaque(row.get("id")), "body": clean(row.get("message") or ""),
                "name": clean(author.get("name") or "LinkedIn member", 500), "depth": depth,
                "occurred_at": clean(row.get("createdTime") or "", 100), "can_reply": can_reply,
                "more_replies": row.get("repliesHasMore") is True})
            for reply in rows({"replies": row.get("replies", [])}, "replies"):
                visit(reply, depth + 1)

        for row in rows(body, "comments"):
            visit(row, 0)
        return result, page_cursor(body, "cursor")

    def send(self, thread_id, body, request_id):
        self.validate_send(thread_id, body, request_id)
        post_id, comment_id = unpack_target(thread_id)
        response = self.request("POST", "/api/v1/inbox/comments/" + quote(post_id, safe=""),
            payload={"accountId": self.account.account_id, "commentId": comment_id, "message": body},
            request_id=request_id)
        try:
            data = object_field(response, "data")
            if response.get("success") is not True or data.get("isReply") is not True:
                raise ValueError()
            return opaque(data.get("commentId"))
        except (ValueError, InboxError):
            raise InboxError("outcome_unknown") from None
