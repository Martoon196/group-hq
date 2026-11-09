"""YouTube/TikTok public comments, using the existing public parser and journal."""
import base64
import hashlib
import json
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from .inbox_providers import Account, InboxError, Transport, clean, object_field, opaque, rows
from .linkedin_comments_provider import LinkedInComments

PROFILES = {"mhp": "6accdd234644a9ea0a2b7376", "pp": "6a98ac5a647e96173efa7ed8",
            "piuk": "6accec6f88d3a22fceb2df31", "kp": "6acfc2c1347304c5e294a88a"}
CHANNELS = {"youtube": "YouTube", "tiktok": "TikTok"}


@dataclass(frozen=True)
class VideoCommentAccount(Account):
    @property
    def surface(self):
        return self.channel + "_public_comments"

    @property
    def binding(self):
        return hashlib.sha256((self.surface + ":" + super().binding).encode()).hexdigest()

    @property
    def identity(self):
        return hashlib.sha256((self.surface + ":" + super().identity).encode()).hexdigest()


def target(account, post_id, comment_id):
    packed = json.dumps([account.surface, opaque(post_id), opaque(comment_id)], separators=(",", ":")).encode()
    return opaque(base64.urlsafe_b64encode(packed).decode().rstrip("="))


class VideoComments(LinkedInComments):
    def __init__(self, account, transport=None):
        if (not isinstance(account, VideoCommentAccount) or account.channel not in CHANNELS
                or PROFILES.get(account.brand) != account.profile_id or account.provider != "zernio"
                or not re.fullmatch(r"[a-f0-9]{24}", account.account_id)):
            raise InboxError("unbound_target")
        Transport.__init__(self, account, transport)

    def request(self, method, path, **kwargs):
        response = super().request(method, path, **kwargs)
        if self.account.channel == "tiktok" and method == "GET" and path == "/api/v1/inbox/comments":
            skipped = object_field(response, "meta").get("accountsSkipped", [])
            if isinstance(skipped, list) and any(isinstance(r, dict) and r.get("accountId") == self.account.account_id for r in skipped):
                raise InboxError("business_connection_needed")
        if method == "GET" and path == "/api/v1/inbox/comments" and object_field(response, "meta").get("accountsQueried", 1) != 1:
            raise InboxError("provider_unavailable")
        return response

    def unpack_target(self, value):
        try:
            opaque(value)
            data = json.loads(base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True))
            if (not isinstance(data, list) or len(data) != 3 or data[0] != self.account.surface
                    or target(self.account, data[1], data[2]) != value):
                raise ValueError()
            return opaque(data[1]), opaque(data[2])
        except (ValueError, TypeError, UnicodeError, InboxError):
            raise InboxError("unbound_target") from None


class VideoSetup(Transport):
    def inventory(self):
        response = self.request("GET", "/api/v1/accounts", params={"profileId": self.account.profile_id,
            "platform": self.account.channel, "limit": 100})
        found = []
        for row in rows(response, "accounts"):
            profile = row.get("profileId")
            profile_id = profile.get("_id") if isinstance(profile, dict) else profile
            if profile_id != self.account.profile_id or row.get("platform") != self.account.channel:
                raise InboxError("unbound_target")
            ident = opaque(row.get("_id"))
            if not re.fullmatch(r"[a-f0-9]{24}", ident):
                raise InboxError("invalid_provider_response")
            found.append({"id": ident, "name": clean(row.get("displayName") or row.get("username") or ident, 200),
                          "username": clean(row.get("username") or "", 200),
                          "connected": row.get("isActive") is True and row.get("enabled") is True
                                       and row.get("needsReconnection") is not True})
        return found

    def connect_url(self, redirect_url, reconnect=None):
        params = {"profileId": self.account.profile_id, "redirect_url": redirect_url, "welcome": "true",
                  "brandName": "Growth HQ"}
        if reconnect:
            current = next((r for r in self.inventory() if r["id"] == reconnect), None)
            if current is None:
                raise InboxError("unbound_target")
            params["reconnectAccountId"] = current["id"]
            params["expectedUsername"] = current["username"]
        response = self.request("GET", "/api/v1/connect/" + self.account.channel, params=params)
        url = clean(response.get("authUrl"), 20000)
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or parsed.username or parsed.password or parsed.port not in (None, 443)
                or parsed.hostname not in ("zernio.com", "accounts.google.com", "www.tiktok.com", "business-api.tiktok.com")):
            raise InboxError("invalid_provider_response")
        return url
