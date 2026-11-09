"""Owner-scoped public-comment targets, isolated from private inbox tickets."""
import os

from fastapi import HTTPException
from itsdangerous import BadData, URLSafeTimedSerializer

from .. import config
from .linkedin_comments_provider import SURFACE, load_public_accounts, reply_target
from .inbox_providers import InboxError, opaque

PAGE = "/inbox/comments"


def accounts(user_email):
    return tuple(a for a in load_public_accounts(os.environ.get("GHQ_INBOX_PUBLIC_ACCOUNTS", "[]"), os.environ)
                 if a.owner_email == user_email.lower())


def signer():
    return URLSafeTimedSerializer(config.SECRET_KEY, salt="ghq-linkedin-public-comment-v1")


def ticket(account, post_id, user_id, *, comment_id=None, name=""):
    return signer().dumps({"surface": SURFACE, "account": account.key, "binding": account.binding,
        "post": opaque(post_id), "comment": opaque(comment_id) if comment_id is not None else None,
        "user": user_id, "name": str(name)[:500]})


def resolve(value, user_id, user_email, *, for_reply=False):
    try:
        bound = signer().loads(value, max_age=7200)
        account = next((a for a in accounts(user_email) if a.key == bound["account"]), None)
        if (bound.get("surface") != SURFACE or bound.get("user") != user_id or account is None
                or account.binding != bound.get("binding")):
            raise ValueError()
        opaque(bound["post"])
        if for_reply:
            if not account.can_reply:
                raise ValueError()
            reply_target(bound["post"], bound["comment"])
        elif bound.get("comment") is not None:
            raise ValueError()
        return account, bound
    except (BadData, ValueError, TypeError, KeyError, AttributeError, InboxError):
        raise HTTPException(409, "Refresh public LinkedIn comments before continuing.") from None


def validate_audience(audience, acknowledge):
    if audience != "public_linkedin" or acknowledge is not True:
        raise HTTPException(400, "Confirm that this reply will be public on LinkedIn.")
