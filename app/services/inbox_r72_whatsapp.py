"""Read signed, canonical MHP WhatsApp projections as the current Growth HQ actor."""
import uuid
from types import SimpleNamespace
from .inbox_providers import Account, InboxError, clean, rows
from .. import config


def account():
    raw = config.GHQ_MHP_WHATSAPP_INBOX_ID
    try:
        if not raw or str(uuid.UUID(raw)) != raw:
            return None
    except ValueError:
        return None
    return Account(key="mhp-whatsapp", brand="mhp", channel="whatsapp", label="Martin Howard Property · WhatsApp",
                   provider="r72_whatsapp", account_id=raw, token=config.R72_GROUP_HQ_INBOX_SECRET,
                   can_reply=config.GHQ_MHP_WHATSAPP_MANUAL_REPLIES)


def native_id(value):
    try:
        if not isinstance(value, str) or str(uuid.UUID(value)) != value:
            raise ValueError()
    except ValueError:
        raise InboxError("invalid_provider_response") from None
    return value


class WhatsAppReader:
    def __init__(self, connection, actor_email):
        self.connection = connection
        self.actor = SimpleNamespace(email=actor_email)

    def read(self, action, **args):
        from .inbox_crm import command, CrmUnavailable
        try:
            result = command(self.actor, "mhp", action, accountId=self.connection.account_id, **args)
        except CrmUnavailable:
            raise InboxError("provider_rejected") from None
        if result.get("brand") != "mhp" or result.get("accountId") != self.connection.account_id:
            raise InboxError("unbound_target")
        return result

    def conversations(self):
        threads, more, _ = self.conversation_page()
        return threads, more

    def conversation_page(self, cursor=None):
        result = self.read("whatsappThreads", **({"cursor": clean(cursor, 4096)} if cursor else {}))
        if not isinstance(result.get("hasMore"), bool):
            raise InboxError("invalid_provider_response")
        following = result.get("nextCursor")
        if following is not None and (not isinstance(following, str) or not 1 <= len(following) <= 4096):
            raise InboxError("invalid_provider_response")
        return [{"id": native_id(t.get("id")), "participant_id": native_id(t.get("participantId")),
                 "name": clean(t.get("name"), 500), "preview": clean(t.get("preview"), 500),
                 "updated_at": clean(t.get("updatedAt"), 100), "unread": None}
                for t in rows(result, "threads")], result["hasMore"], following

    def messages(self, thread_id, participant_id, cursor=None):
        result = self.read("whatsappMessages", threadId=native_id(thread_id), cursor=native_id(cursor) if cursor else None)
        if result.get("threadId") != thread_id or "nextCursor" not in result:
            raise InboxError("unbound_target")
        messages = []
        for m in rows(result, "messages"):
            if native_id(m.get("participantId")) != participant_id:
                raise InboxError("unbound_target")
            direction = m.get("direction", "incoming")
            state = m.get("replyState")
            if direction not in ("incoming", "outgoing") or state not in (None,"queued","calling","expired","failed","accepted","outcome_unknown","sent","delivered","read","deleted"):
                raise InboxError("invalid_provider_response")
            # Before 0143 the source could only return an already verified CRM contact.
            contact = m.get("contactId", participant_id)
            messages.append({"id": native_id(m.get("id")), "direction": direction, "body": clean(m.get("body"), 4096),
                             "reply_state": state, "can_reply": result.get("canReply") is True,
                             "contact_id": native_id(contact) if contact is not None else None,
                             "name": clean(m.get("name"), 500), "occurred_at": clean(m.get("occurredAt"), 100)})
        cursor = result["nextCursor"]
        return list(reversed(messages)), native_id(cursor) if cursor is not None else None

    def queue(self, thread_id, text, request_id):
        from .inbox_crm import command, CrmUnavailable, ReplyRefused
        try:
            result = command(self.actor, "mhp", "whatsappReply", accountId=self.connection.account_id,
                             threadId=native_id(thread_id), body=text, request_id=native_id(request_id))
        except ReplyRefused:
            raise InboxError("provider_rejected") from None
        except CrmUnavailable:
            raise InboxError("outcome_unknown") from None
        if (result.get("brand"), result.get("accountId"), result.get("threadId"), result.get("requestId")) != (
                "mhp", self.connection.account_id, thread_id, request_id):
            raise InboxError("outcome_unknown")
        state = result.get("state")
        if state in ("queued", "calling"):
            return "bridge_accepted"
        if state == "accepted":
            return state
        raise InboxError("provider_rejected" if state in ("failed", "expired") else "outcome_unknown")
