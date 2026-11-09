import hashlib
import uuid

from sqlalchemy import event, insert

from app.models import InboxCrmLink, InboxThread, utcnow
from app.services import inbox, inbox_crm, inbox_organisation
from tests.test_personal_inbox import personal_account

__all__ = ["personal_account"]  # Export the exact existing owner fixture for this module.


def imported(account, user, ident):
    return {"account_key": account.key, "thread_sha256": hashlib.sha256(ident.encode()).hexdigest(),
        "thread_id": ident, "owner_user_id": user.id, "binding_sha256": account.binding,
        "participant_id": "peer-" + ident, "name": "Private person " + ident, "preview": "Rendered preview",
        "provider_updated_at": "2026-11-09T12:00:00Z", "messages": [{"body": "Never select this body in a snapshot"}],
        "has_more": True, "seen_at": utcnow()}


def test_local_snapshot_exposes_imported_metadata_beyond_100_without_per_thread_queries(db, personal_account):
    user, account, _ = personal_account
    db.execute(insert(InboxThread), [imported(account, user, "import-" + str(i)) for i in range(520)])
    db.commit()
    explicit = inbox_organisation.save(db, user, account, "import-0", "unclassified", [], [], "Owner decided")
    queries = []
    def capture(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            queries.append(statement)
    event.listen(db.bind, "before_cursor_execute", capture)
    try:
        _, results, threads = inbox.snapshot(user.id, [], db=db, user_email=user.email)
    finally:
        event.remove(db.bind, "before_cursor_execute", capture)
    assert len(threads) == 521 and not results[0]["has_more"]
    assert len(queries) <= 7  # Account registry, one metadata window, two batches each of choices/links.
    assert all("inbox_threads.messages" not in statement for statement in queries)
    by_id = {t["id"]: t for t in threads}
    assert by_id["import-0"]["owner_choice"] is True
    assert by_id["import-0"]["organisation"] == explicit
    assert by_id["import-1"]["owner_choice"] is False


def test_batched_default_choices_preserve_identity_bound_crm_brands(db, personal_account):
    user, account, _ = personal_account
    digest = hashlib.sha256(b"durham-thread").hexdigest()
    for brand, identity in (("kp", account.identity), ("pp", "0" * 64)):
        db.add(InboxCrmLink(account_key=account.key, thread_sha256=digest, brand=brand,
            account_identity_sha256=identity, participant_id="native-peer", contact_id=str(uuid.uuid4()),
            workspace_id=str(uuid.uuid4()), user_id=user.id))
    db.commit()
    expected = inbox_organisation.choices(db, user.id, account, "durham-thread",
        inbox_crm.links(db, account, "durham-thread"))
    _, _, threads = inbox.snapshot(user.id, [], db=db, user_email=user.email)
    assert threads[0]["organisation"] == expected == {"kind": "business", "brands": ["kp"], "sync_brands": ["kp"], "note": ""}
    assert threads[0]["owner_choice"] is False


def test_local_snapshot_retains_explicit_metadata_bound_and_overflow_signal(db, personal_account):
    user, account, _ = personal_account
    db.execute(insert(InboxThread), [imported(account, user, "extra-" + str(i)) for i in range(inbox.LOCAL_METADATA_LIMIT)])
    db.commit()
    _, results, threads = inbox.snapshot(user.id, [], db=db, user_email=user.email)
    assert len(threads) == inbox.LOCAL_METADATA_LIMIT
    assert results[0]["has_more"] is True
