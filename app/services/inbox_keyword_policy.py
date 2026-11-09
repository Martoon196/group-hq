"""Deterministic keyword matching and message identity across inbox polls."""
import hashlib
import json
import re
import uuid
from datetime import datetime, timezone


def timestamp(value):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc) if parsed.tzinfo is not None else None
    except (AttributeError, TypeError, ValueError, OverflowError):
        return None


def matching_rule(rules, message, occurred_at, *, exact=True):
    choices = []
    for rule in rules:
        since = timestamp(rule.get("since"))
        if not rule["enabled"] or since is None or occurred_at <= since:
            continue
        phrase = r"\s+".join(re.escape(word) for word in rule["keyword"].split())
        punctuation = r"[\s!?.,:;…“”‘’]*"
        pattern = r"\A" + punctuation + phrase + punctuation + r"\Z" if exact else r"(?<!\w)" + phrase + r"(?!\w)"
        if re.search(pattern, message, re.IGNORECASE):
            choices.append(rule)
    # Prefer the more specific phrase. Insertion order breaks equal-length ties.
    return max(choices, key=lambda rule: len(rule["keyword"]), default=None)


def receipt_id(account, thread_id, message_id):
    # Stable across rule edits, owner sessions, credential rotations and direction refreshes.
    fields = ["ghq-keyword-reply-v1", account.identity, thread_id, message_id]
    digest = hashlib.sha256(json.dumps(fields, ensure_ascii=False).encode()).hexdigest()
    return str(uuid.UUID(digest[:32], version=5))
