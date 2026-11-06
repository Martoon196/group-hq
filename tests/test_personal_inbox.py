import hashlib
import hmac
import json
import time
import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app import config
from app.models import InboxCrmActivityReceipt, InboxCrmLink, InboxReply, User, utcnow
from app.services import inbox, inbox_crm, personal_inbox as personal


@pytest.fixture
def personal_account(db, monkeypatch):
    user = db.scalars(select(User)).first()
    monkeypatch.setattr(config, "GHQ_PERSONAL_INBOX_OWNER_EMAIL", user.email)
    monkeypatch.setattr(config, "GHQ_PERSONAL_INBOX_SECRET", "x" * 32)
    monkeypatch.setenv("GHQ_INBOX_PERSONAL_SECRET", "x" * 32)
    result = personal.discover(db, {"accounts": [{"channel": "facebook", "account_id": "personal-account", "user_id": "me",
        "engine_provider": "local", "status": "connected", "label": "Martin personal Messenger"}]})
    saved = personal.catalog(db)
    saved["accounts"][0]["enabled"], saved["accounts"][0]["brand"] = True, "kp"
    personal.save_catalog(db, saved, user.email)
    db.commit()
    binding = result["accounts"][0]
    personal.ingest(db, {"account_key": binding["key"], "account_id": "personal-account", "user_id": "me",
        "observed_at": utcnow().isoformat() + "Z", "thread": {"id": "durham-thread", "participant_id": "native-peer",
        "name": "Durham prospect", "preview": "Interested in Durham", "updated_at": "2026-11-06T12:00:00Z",
        "is_group": False, "is_merged": False, "messages": [{"id": "native-message", "body": "Interested in Durham",
            "direction": "incoming", "name": "Durham prospect", "occurred_at": "2026-11-06T12:00:00Z"}]}})
    account = inbox.accounts(db)[0]
    return user, account, inbox.ticket(account, "durham-thread", user.id, "Durham prospect", "native-peer")


def signed(action, body):
    path = personal.BASE + "/" + action
    raw = json.dumps(body, separators=(",", ":")).encode()
    stamp = str(int(time.time()))
    signature = hmac.new(b"x" * 32, f"{stamp}\nPOST\n{path}\n".encode() + raw, hashlib.sha256).hexdigest()
    return path, raw, {"content-type": "application/json", "x-ghq-timestamp": stamp, "x-ghq-signature": signature}


def test_private_accounts_cache_and_tickets_are_owner_bound(admin_client, db, personal_account):
    user, account, ticket = personal_account
    page = admin_client.get("/inbox")
    assert "Durham prospect" in page.text and "Connect personal Messenger" in page.text
    assert "Interested in Durham" in admin_client.get("/inbox?thread=" + ticket).text
    assert inbox.snapshot(user.id, [], db=db, user_email="another@example.test")[0] == ()
    with pytest.raises(HTTPException) as error:
        inbox.resolve(ticket, user.id, "another@example.test", db)
    assert error.value.status_code == 409


def test_selected_facebook_cloud_transport_keeps_native_owner_binding(admin_client, db, personal_account):
    user, account, _ = personal_account
    result = personal.discover(db, {"accounts": [{"channel": "facebook", "account_id": account.account_id, "user_id": "me",
        "engine_provider": "cloud", "status": "connected", "label": "Martin personal Messenger"}]})
    assert result["accounts"][0]["key"] == account.key
    assert result["accounts"][0]["enabled"] is True
    assert personal.catalog(db)["accounts"][0]["engine_provider"] == "cloud"
    assert "Beeper cloud connection" in admin_client.get("/inbox/connect").text
    with pytest.raises(HTTPException) as error:
        personal.discover(db, {"accounts": [{"channel": "facebook", "account_id": account.account_id,
            "user_id": "someone-else", "engine_provider": "cloud", "status": "connected"}]})
    assert error.value.status_code == 409


def test_native_discovery_does_not_disconnect_or_claim_other_connector(db, personal_account):
    user, beeper, _ = personal_account
    native = {"connector_id": "native-messenger", "accounts": [{"channel": "facebook", "account_id": "facebook-web:123",
        "user_id": "123", "engine_provider": "growth-hq-browser", "status": "connected", "label": "Own Messenger", "can_reply": False}]}
    result = personal.discover(db, native)
    assert len(result["accounts"]) == 1
    key = result["accounts"][0]["key"]
    rows = {r["key"]: r for r in personal.catalog(db)["accounts"]}
    assert rows[beeper.key]["status"] == "connected" and rows[beeper.key]["enabled"]
    assert rows[key]["can_reply"] is False
    personal.discover(db, {"accounts": []})
    rows = {r["key"]: r for r in personal.catalog(db)["accounts"]}
    assert rows[key]["status"] == "connected" and rows[beeper.key]["status"] == "disconnected"
    with pytest.raises(HTTPException):
        personal.discover(db, {"accounts": [{**native["accounts"][0], "engine_provider": "local"}]})


def test_native_queue_isolated_from_legacy_worker(db, personal_account):
    user, beeper, _ = personal_account
    ident = str(uuid.uuid4())
    inbox.send(db, user, beeper, "durham-thread", "Requested reply", ident)
    assert personal.pending(db, "native-messenger")["reply_ids"] == []
    with pytest.raises(HTTPException) as error:
        personal.begin(db, ident, "native-messenger")
    assert error.value.status_code == 403
    assert db.get(InboxReply, ident).state == "queued"
    assert personal.begin(db, ident)["dispatch"] is True
    with pytest.raises(HTTPException):
        personal.settle(db, ident, {"connector_id": "native-messenger", "state": "outcome_unknown"})
    assert db.get(InboxReply, ident).state == "calling"


def test_adapter_hmac_is_exact_body_and_path_bound(client, db, personal_account):
    _, account, _ = personal_account
    path, raw, headers = signed("accounts", {"accounts": []})
    assert client.post(path, content=raw, headers=headers).status_code == 200
    assert client.post(path, content=raw + b" ", headers=headers).status_code == 401
    assert client.post(path + "?x=1", content=raw, headers=headers).status_code == 400
    assert client.post(personal.BASE + "/threads", content=raw, headers=headers).status_code == 401


def test_original_account_queue_claim_and_pending_resolution(db, personal_account):
    user, account, _ = personal_account
    ident = str(uuid.uuid4())
    row = inbox.send(db, user, account, "durham-thread", "Requested reply", ident)
    assert row.state == "queued" and personal.pending(db)["reply_ids"] == [ident]
    assert personal.begin(db, ident)["dispatch"] is True
    assert personal.begin(db, ident)["dispatch"] is False
    assert inbox.send(db, user, account, "durham-thread", "Requested reply", ident).state == "calling"
    personal.settle(db, ident, {"state": "bridge_accepted", "provider_message_id": "temporary-id"})
    with pytest.raises(HTTPException):
        personal.settle(db, ident, {"state": "accepted", "provider_message_id": "different"})
    db.rollback()
    result = {"state": "accepted", "provider_message_id": "native-id", "previous_message_id": "temporary-id"}
    assert personal.settle(db, ident, result)["state"] == "accepted"
    assert personal.settle(db, ident, result)["state"] == "accepted"
    with pytest.raises(HTTPException):
        personal.settle(db, ident, {**result, "provider_message_id": "changed"})


def test_crm_uses_signed_native_source_and_only_stores_confirmed_ids(db, personal_account, monkeypatch):
    user, account, ticket = personal_account
    contact_id, workspace_id = str(uuid.uuid4()), str(uuid.uuid4())
    calls = []
    def native(actor, brand, action, **payload):
        calls.append((brand, action, payload))
        return {"brand": brand, "contactId": contact_id, "workspaceId": workspace_id}
    monkeypatch.setattr(inbox_crm, "command", native)
    result = inbox_crm.connect(db, user, ticket, "kp", "Durham prospect")
    assert result["contactId"] == contact_id and calls[0][2]["source"] == {
        "channel": "facebook", "accountId": account.account_id, "threadId": "durham-thread", "participantId": "native-peer"}
    cached = inbox.local_thread(db, account, "durham-thread", user.id)
    assert inbox_crm.record_messages(db, user, account, "durham-thread", cached.messages) == 1
    assert inbox_crm.record_messages(db, user, account, "durham-thread", cached.messages) == 0
    assert db.scalars(select(InboxCrmActivityReceipt)).all()
    assert db.scalars(select(InboxCrmLink)).first().contact_id == contact_id
    def unavailable(*args, **kwargs):
        raise inbox_crm.CrmUnavailable("Unavailable")
    monkeypatch.setattr(inbox_crm, "command", unavailable)
    with pytest.raises(inbox_crm.CrmUnavailable):
        inbox_crm.connect(db, user, ticket, "mhp", "Durham prospect")
    assert len(db.scalars(select(InboxCrmLink)).all()) == 1


def test_conversation_can_have_two_brands_without_auto_copy_and_personal_stops_projection(db, personal_account, monkeypatch, admin_client):
    from app.services import inbox_organisation as organisation
    user, account, ticket = personal_account
    ids = {brand: (str(uuid.uuid4()), str(uuid.uuid4())) for brand in ("kp", "pp", "mhp")}
    calls = []
    def native(actor, brand, action, **payload):
        calls.append((brand, action, payload))
        return {"brand": brand, "contactId": ids[brand][0], "workspaceId": ids[brand][1]}
    monkeypatch.setattr(inbox_crm, "command", native)
    for brand in ("kp", "pp"):
        response = admin_client.post('/inbox/crm', data={"thread_ticket": ticket, "brand": brand, "display_name": "Buyer"}, follow_redirects=False)
        assert response.status_code == 303
    cached = inbox.local_thread(db, account, "durham-thread", user.id)
    assert inbox_crm.record_messages(db, user, account, "durham-thread", cached.messages) == 0
    links = inbox_crm.links(db, account, "durham-thread")
    assert len(links) == 2 and len({link.workspace_id for link in links}) == 2
    for brand in ("kp", "pp"):
        assert len(inbox.snapshot(user.id, [], brand, db=db, user_email=user.email)[2]) == 1
    organisation.save(db, user, account, "durham-thread", "business", ["kp", "pp"], ["pp"])
    assert inbox_crm.record_messages(db, user, account, "durham-thread", cached.messages) == 1
    assert [c[0] for c in calls if c[1] == 'activity'] == ['pp']
    organisation.save(db, user, account, "durham-thread", "personal", ["kp", "pp"], ["pp"], "Private conversation")
    changed = [{**cached.messages[0], "id": "new-message"}]
    assert inbox_crm.record_messages(db, user, account, "durham-thread", changed) == 0
    assert inbox.snapshot(user.id, [], 'kp', db=db, user_email=user.email)[2] == []
    assert len(inbox.snapshot(user.id, [], db=db, user_email=user.email, kind='personal')[2]) == 1
    assert inbox.resolve(ticket, user.id, user.email, db)[0].account_id == account.account_id
    assert len(inbox_crm.links(db, account, "durham-thread")) == 2  # Keep history, don't delete contacts.
    inbox_crm.connect(db, user, ticket, 'mhp', 'Buyer', copy_messages=True)
    assert organisation.choices(db, user.id, account, 'durham-thread')['kind'] == 'personal'
    assert inbox_crm.record_messages(db, user, account, "durham-thread", changed) == 0


def test_unclassified_personal_account_does_not_inherit_business_filter(db, personal_account):
    user, account, ticket = personal_account
    assert inbox.snapshot(user.id, [], 'kp', db=db, user_email=user.email)[2] == []
    assert len(inbox.snapshot(user.id, [], db=db, user_email=user.email, kind='unclassified')[2]) == 1


def test_inbox_metadata_keys_fit_real_schema_and_keep_full_owner_tuple(personal_account):
    from app.services import inbox_organisation as organisation, inbox_discovery as discovery
    user, account, _ = personal_account
    keys = [organisation.key(user.id, account, 'a'*500), discovery.registry_key(user.id, account), inbox.account_brand_key(account)]
    assert all(len(k) <= 80 for k in keys)
    assert organisation.key(user.id, account, 'a'*500) != organisation.key(user.id, account, 'a'*499+'b')
    assert discovery.registry_key(user.id, account) != discovery.registry_key(user.id+1, account)


def test_new_crm_link_and_no_copy_choice_commit_together(db, personal_account, monkeypatch):
    from app.services import inbox_organisation as organisation
    user, account, ticket = personal_account
    monkeypatch.setattr(inbox_crm, 'command', lambda *args, **kwargs: {'brand':'kp','contactId':str(uuid.uuid4()),'workspaceId':str(uuid.uuid4())})
    commit = db.commit
    observations = []
    def checked_commit():
        links = inbox_crm.links(db, account, 'durham-thread')
        settings = organisation.choices(db, user.id, account, 'durham-thread', links)
        observations.append((len(links), settings['sync_brands']))
        commit()
    monkeypatch.setattr(db, 'commit', checked_commit)
    inbox_crm.connect(db, user, ticket, 'kp', 'Buyer', copy_messages=False)
    assert observations == [(1, [])]


def test_organisation_rejects_unlinked_copy_and_cross_owner_ticket(admin_client, db, personal_account):
    user, account, ticket = personal_account
    response = admin_client.post('/inbox/organise', data={'thread_ticket': ticket, 'kind': 'business', 'brands': 'pp', 'sync_brands': 'pp'})
    assert response.status_code == 400
    forged = inbox.ticket(account, 'durham-thread', user.id + 99, 'Buyer')
    response = admin_client.post('/inbox/organise', data={'thread_ticket': forged, 'kind': 'personal'})
    assert response.status_code == 409


@pytest.fixture
def native_discovery(db, personal_account):
    user, _, _ = personal_account
    found = personal.discover(db, {'connector_id': 'native-messenger', 'accounts': [{'channel': 'facebook', 'account_id': 'facebook-web:123',
        'user_id': '123', 'engine_provider': 'growth-hq-browser', 'status': 'connected', 'label': 'Own Messenger', 'can_reply': True}]})
    key = found['accounts'][0]['key']
    catalog = personal.catalog(db)
    next(r for r in catalog['accounts'] if r['key'] == key).update(enabled=True, brand='kp')
    personal.save_catalog(db, catalog, user.email); db.commit()
    return user, next(a for a in inbox.accounts(db) if a.key == key), {'connector_id': 'native-messenger', 'account_key': key, 'account_id': 'facebook-web:123', 'user_id': '123'}


def test_discovery_requires_owner_selection_preserves_native_identity_and_unknown_rows(admin_client, db, native_discovery):
    from app.services import inbox_discovery as discovery
    user, account, payload = native_discovery
    rows = [{'threadId': '1234', 'participantId': '9876', 'name': 'Buyer', 'textOnly': True},
            {'threadId': '1235', 'participantId': '', 'name': 'Unsupported buyer', 'reason': 'Format needs support.'}]
    discovery.report(db, {**payload, 'candidates': rows, 'query': 'Durham', 'complete': False})
    assert discovery.selections(db, payload)['threads'] == []
    assert not discovery.permits_thread(db, user.id, account, '1234', '9876')
    page = admin_client.get('/inbox/discover', params={'account': account.key})
    assert page.status_code == 200 and 'Unsupported buyer' in page.text and 'Format needs support.' in page.text
    assert admin_client.post('/inbox/discover/select', data={'account_key': account.key, 'threads': '1235'}).status_code == 400
    assert admin_client.post('/inbox/discover/select', data={'account_key': account.key, 'threads': '1234'}, follow_redirects=False).status_code == 303
    assert discovery.permits_thread(db, user.id, account, '1234', '9876')
    assert not discovery.permits_thread(db, user.id, account, '1234', '9999')
    with pytest.raises(HTTPException) as error:
        discovery.report(db, {**payload, 'candidates': [{**rows[0], 'participantId': '9999'}]})
    assert error.value.status_code == 409
    discovery.report(db, {**payload, 'candidates': rows})
    assert len(discovery.selections(db, payload)['threads']) == 1
    assert not db.scalars(select(InboxCrmLink)).all()


def test_discovery_wrong_connector_and_unselected_import_refused(db, native_discovery):
    from app.services import inbox_discovery as discovery
    user, account, payload = native_discovery
    discovery.report(db, {**payload, 'candidates': [{'threadId': '1234', 'participantId': '9876', 'name': 'Buyer'}]})
    with pytest.raises(HTTPException):
        discovery.selections(db, {**payload, 'connector_id': 'beeper'})
    with pytest.raises(HTTPException) as error:
        personal.ingest(db, {**payload, 'thread': {'id': '1234', 'participant_id': '9876', 'is_group': False, 'is_merged': False}})
    assert error.value.status_code == 409


def test_deselect_after_queue_rejects_native_reply_without_dispatch(db, native_discovery):
    from app.services import inbox_discovery as discovery
    user, account, payload = native_discovery
    discovery.report(db, {**payload, 'candidates': [{'threadId':'1234','participantId':'9876','name':'Buyer'}]})
    saved = discovery.read(db, user.id, account)
    saved['candidates'][0]['selected'] = True
    discovery.write(db, user.id, account, saved, user.email)
    personal.ingest(db, {**payload, 'observed_at':utcnow().isoformat()+'Z', 'thread':{'id':'1234','participant_id':'9876',
        'name':'Buyer','updated_at':'2026-11-06T12:00:00Z','is_group':False,'is_merged':False,'messages':[]}})
    reply_id = str(uuid.uuid4())
    inbox.send(db, user, account, '1234', 'Owner draft', reply_id)
    saved['candidates'][0]['selected'] = False
    discovery.write(db, user.id, account, saved, user.email)
    assert personal.begin(db, reply_id, 'native-messenger') == {'state':'rejected','dispatch':False}
    assert db.get(InboxReply, reply_id).reason == 'conversation_deselected'
