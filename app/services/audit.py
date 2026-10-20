"""The audit trail: one row per change, written in the same transaction as the change.

    audit(db, user, "mentee.create", f"mentee:{m.id}", before=None, after={"status": "active"})
    audit(db, "job:recompute", "grant.extend", f"grant:{g.id}", before={...}, after={...})

The caller commits; audit() only adds the row to the session so a failed write leaves no trace of
a change that never happened.
"""
from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy.orm import Session

from ..models import AuditLog, User

MAX_JSON_BYTES = 8000
REDACT_KEYS = ("password", "secret", "token")
REDACTED = "[redacted]"


def _redacted(key: str) -> bool:
    lowered = str(key).lower()
    return any(word in lowered for word in REDACT_KEYS)


def _jsonable(value):
    """Plain JSON for the before/after columns: dates become ISO strings, decimals become strings, and
    anything with a __dict__ is reduced to its public attributes. Any key that names a password, secret or
    token is replaced by "[redacted]", so a snapshot of a User or a ConnectorCall can never carry one."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): (REDACTED if _redacted(k) else _jsonable(v)) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in value]
    if hasattr(value, "__dict__"):
        return {k: (REDACTED if _redacted(k) else _jsonable(v)) for k, v in vars(value).items() if not k.startswith("_")}
    return str(value)


def _bounded(value):
    """Keep an audit row readable: an oversized snapshot is replaced by a note rather than truncated JSON."""
    data = _jsonable(value)
    if data is None:
        return None
    if len(json.dumps(data, ensure_ascii=False)) > MAX_JSON_BYTES:
        return {"note": f"snapshot over {MAX_JSON_BYTES} bytes was not stored"}
    return data


def audit(db: Session, actor: User | str | None, action: str, entity: str = "",
          before=None, after=None) -> AuditLog:
    """Record who (a User, or a string such as "system" or "job:recompute") did what to which entity."""
    if isinstance(actor, User):
        user_id, actor_label = actor.id, actor.email
    else:
        user_id, actor_label = None, (actor or "system")
    row = AuditLog(user_id=user_id, actor=str(actor_label)[:200], action=action[:80], entity=(entity or "")[:200],
                   before=_bounded(before), after=_bounded(after))
    db.add(row)
    return row


def changes(before: dict, after: dict) -> tuple[dict, dict]:
    """The subset of two field dicts that differs, so an audit row shows only what moved."""
    keys = [k for k in after if before.get(k) != after.get(k)]
    return {k: before.get(k) for k in keys}, {k: after.get(k) for k in keys}
