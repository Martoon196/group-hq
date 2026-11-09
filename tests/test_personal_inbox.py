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


def test_discovery_cached_outreach_and_verified_new_candidates(admin_client, db, native_discovery):
    from app.services import inbox_discovery as discovery
    user, account, payload = native_discovery
    phrase = 'what sort of property deals'
    row = {'threadId': '1234', 'participantId': '9876', 'name': 'Known buyer'}
    discovery.report(db, {**payload, 'candidates': [row]})
    admin_client.post('/inbox/discover/select', data={'account_key': account.key, 'threads': '1234'})
    personal.ingest(db, {**payload, 'observed_at': utcnow().isoformat() + 'Z', 'thread': {
        'id': '1234', 'participant_id': '9876', 'name': 'Known buyer', 'updated_at': '2026-11-06T12:00:00Z',
        'is_group': False, 'is_merged': False, 'messages': [{'id': 'm1', 'name': 'You', 'direction': 'outgoing',
        'body': 'Hi, WHAT sort of\nproperty deals are you looking for please?', 'occurred_at': '2026-11-06T12:00:00Z'}]}})
    response = admin_client.post('/inbox/discover/search', data={'account_key': account.key, 'query': phrase})
    assert response.status_code == 303
    local = discovery.read(db, user.id, account)['request']
    assert local['cached'] == [{'threadId': '1234', 'name': 'Known buyer'}]
    assert 'cached' not in discovery.selections(db, payload)['request']
    selected = [{**row, 'selected': True, 'reason': ''}]
    assert discovery.cached_matches(db, user.id+1, account, selected, phrase) == []
    assert discovery.cached_matches(db, user.id, account, [{**selected[0], 'participantId': '9999'}], phrase) == []
    assert discovery.cached_matches(db, user.id, account, [{**selected[0], 'selected': False}], phrase) == []
    assert discovery.cached_matches(db, user.id, account, selected, '%') == []

    fresh = {'threadId': '4444', 'participantId': '5555', 'name': 'New buyer'}
    discovery.report(db, {**payload, 'candidates': [fresh], 'query': phrase, 'request_id': 'stale'})
    assert not discovery.read(db, user.id, account)['request']['done']
    discovery.report(db, {**payload, 'candidates': [row, fresh], 'query': phrase, 'request_id': local['id']})
    saved = discovery.read(db, user.id, account)
    assert [m['status'] for m in saved['coverage']['matches']] == ['Already imported', 'New candidate']
    assert not discovery.permits_thread(db, user.id, account, '4444', '5555')
    page = admin_client.get('/inbox/discover', params={'account': account.key})
    assert 'New buyer · New candidate' in page.text and 'looking for please?' not in page.text
    assert not db.scalars(select(InboxCrmLink)).all()


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


def test_sync_all_requires_owner_explicit_consent_and_origin(admin_client, db, native_discovery, monkeypatch):
    from app.services import inbox_discovery as discovery
    user, account, payload = native_discovery
    data = {'account_key': account.key, 'sync_all': 'true'}
    assert admin_client.post('/inbox/discover/sync-all', data=data).status_code == 400
    assert not discovery.selections(db, payload)['sync_all']
    data['acknowledge'] = 'true'
    assert admin_client.post('/inbox/discover/sync-all', data=data, headers={'Origin': 'https://foreign.example.test'}).status_code == 403
    monkeypatch.setattr(config, 'GHQ_PERSONAL_INBOX_OWNER_EMAIL', 'other@example.test')
    assert admin_client.post('/inbox/discover/sync-all', data=data).status_code == 403


def test_sync_all_imports_verified_incoming_personal_chat_without_crm_grant(admin_client, db, native_discovery):
    from app.services import inbox_discovery as discovery
    user, account, payload = native_discovery
    response = admin_client.post('/inbox/discover/sync-all', data={
        'account_key': account.key, 'sync_all': 'true', 'acknowledge': 'true'})
    assert response.status_code == 303
    discovery.report(db, {**payload, 'candidates': [
        {'threadId': '1234', 'participantId': '9876', 'name': 'A friend'},
        {'threadId': '1235', 'participantId': '', 'name': 'Unverified friend', 'reason': 'Identity not supplied'}]})
    selected = discovery.selections(db, payload)
    assert selected['sync_all'] and [r['threadId'] for r in selected['threads']] == ['1234']
    assert discovery.permits_thread(db, user.id, account, '1234', '9876')
    assert not discovery.permits_thread(db, user.id, account, '1235', '1111')
    personal.ingest(db, {**payload, 'observed_at': utcnow().isoformat() + 'Z', 'thread': {
        'id': '1234', 'participant_id': '9876', 'name': 'A friend', 'updated_at': '2026-11-09T12:00:00Z',
        'is_group': False, 'is_merged': False, 'messages': [{'id': 'incoming-native-id', 'name': 'A friend',
        'direction': 'incoming', 'body': 'Personal incoming chat', 'occurred_at': '2026-11-09T12:00:00Z'}]}})
    assert not db.scalars(select(InboxCrmLink)).all()
    page = admin_client.get('/inbox/discover', params={'account': account.key})
    assert 'Automatic discovery is on' in page.text and 'Personal incoming chat' not in page.text


def test_sync_all_off_restores_named_selections_and_blocks_auto_only_ingest(admin_client, db, native_discovery):
    from app.services import inbox_discovery as discovery
    user, account, payload = native_discovery
    discovery.report(db, {**payload, 'candidates': [
        {'threadId': '1234', 'participantId': '9876', 'name': 'Named buyer'},
        {'threadId': '2234', 'participantId': '8876', 'name': 'Personal friend'}]})
    admin_client.post('/inbox/discover/select', data={'account_key': account.key, 'threads': '1234'})
    admin_client.post('/inbox/discover/sync-all', data={'account_key': account.key, 'sync_all': 'true', 'acknowledge': 'true'})
    assert len(discovery.selections(db, payload)['threads']) == 2
    assert admin_client.post('/inbox/discover/sync-all', data={'account_key': account.key}).status_code == 303
    selection = discovery.selections(db, payload)
    assert not selection['sync_all'] and [r['threadId'] for r in selection['threads']] == ['1234']
    with pytest.raises(HTTPException) as error:
        personal.ingest(db, {**payload, 'thread': {'id': '2234', 'participant_id': '8876', 'is_group': False, 'is_merged': False}})
    assert error.value.status_code == 409


def test_sync_all_incremental_batches_extend_existing_registry_without_search(admin_client, db, native_discovery):
    from app.services import inbox_discovery as discovery
    user, account, payload = native_discovery
    admin_client.post('/inbox/discover/sync-all', data={'account_key': account.key, 'sync_all': 'true', 'acknowledge': 'true'})
    for batch in range(3):
        rows = [{'threadId': str(1000 + batch * 100 + i), 'participantId': str(9000 + batch * 100 + i),
                 'name': 'Private person ' + str(batch * 100 + i)} for i in range(100)]
        discovery.report(db, {**payload, 'candidates': rows, 'complete': False})
    selected = discovery.selections(db, payload)
    assert len(selected['threads']) == 300 and selected['request'] is None
    assert all(not r['selected'] for r in selected['threads'])  # Manual choices are not overwritten.
    assert not discovery.read(db, user.id, account)['coverage']['complete']


def test_sync_all_still_rejects_changed_participant_and_groups(admin_client, db, native_discovery):
    from app.services import inbox_discovery as discovery
    user, account, payload = native_discovery
    admin_client.post('/inbox/discover/sync-all', data={'account_key': account.key, 'sync_all': 'true', 'acknowledge': 'true'})
    discovery.report(db, {**payload, 'candidates': [{'threadId': '1234', 'participantId': '9876', 'name': 'Friend'}]})
    with pytest.raises(HTTPException) as error:
        discovery.report(db, {**payload, 'candidates': [{'threadId': '1234', 'participantId': '1111', 'name': 'Different'}]})
    assert error.value.status_code == 409
    for field in ('is_group', 'is_merged'):
        with pytest.raises(HTTPException):
            personal.ingest(db, {**payload, 'thread': {'id': '1234', 'participant_id': '9876',
                'is_group': False, 'is_merged': False, field: True}})


def test_sync_all_off_revokes_pending_auto_only_reply(db, native_discovery):
    from app.services import inbox_discovery as discovery
    user, account, payload = native_discovery
    discovery.report(db, {**payload, 'candidates': [{'threadId': '1234', 'participantId': '9876', 'name': 'Friend'}]})
    saved = discovery.read(db, user.id, account)
    saved['sync_all'] = True
    discovery.write(db, user.id, account, saved, user.email)
    personal.ingest(db, {**payload, 'observed_at': utcnow().isoformat() + 'Z', 'thread': {
        'id': '1234', 'participant_id': '9876', 'name': 'Friend', 'updated_at': '2026-11-09T12:00:00Z',
        'is_group': False, 'is_merged': False, 'messages': []}})
    reply_id = str(uuid.uuid4())
    inbox.send(db, user, account, '1234', 'Owner draft', reply_id)
    saved['sync_all'] = False
    discovery.write(db, user.id, account, saved, user.email)
    assert personal.begin(db, reply_id, 'native-messenger') == {'state': 'rejected', 'dispatch': False}
    assert db.get(InboxReply, reply_id).reason == 'conversation_deselected'


def test_owner_takeover_blocks_queued_automatic_claim_and_preserves_manual_reply(db, personal_account, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from datetime import datetime, timedelta, timezone
    from threading import Event
    from app.db import SessionLocal
    from app.services import inbox_automations as auto
    from app.services.inbox_triage import analyse

    user, account, _ = personal_account
    clock = [datetime(2026, 11, 9, 9, tzinfo=timezone.utc)]
    monkeypatch.setattr(auto, "now", lambda: clock[0])
    auto.update(db, user, account, "add", keyword="income", reply="Here is your requested guide.")
    rule = auto.settings(db, user, account)["rules"][0]
    auto.update(db, user, account, "enable", rule_id=rule["id"])
    auto.update(db, user, account, "start")
    clock[0] += timedelta(minutes=1)
    cached = inbox.local_thread(db, account, "durham-thread", user.id)
    cached.messages = [{"body": "INCOME!", "direction": "incoming", "id": "income-message", "occurred_at": clock[0].isoformat()}]
    db.commit()
    messages = cached.messages
    automatic = auto.receipt_id(account, "durham-thread", "income-message")

    auto.process_thread(db, user, account, "durham-thread", messages, analyse)
    assert db.get(InboxReply, automatic) is None  # Personal account with no owner business choice.
    from app.services import inbox_organisation
    inbox_organisation.save(db, user, account, "durham-thread", "business", ["kp"], [])
    auto.process_thread(db, user, account, "durham-thread", messages, analyse)
    assert db.get(InboxReply, automatic).state == "queued"
    manual = str(uuid.uuid4())
    inbox.send(db, user, account, "durham-thread", "Personal reply from the owner", manual)

    entered = Event()
    real_lock = auto.lock_account
    real_lock(db, account)
    def observed_lock(session, account):
        if session is not db:
            entered.set()
        real_lock(session, account)
    monkeypatch.setattr(auto, "lock_account", observed_lock)
    def claim():
        with SessionLocal() as connector:
            return personal.begin(connector, automatic)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending_claim = pool.submit(claim)
        try:
            assert entered.wait(5), "Connector did not reach the shared account lock"
            auto.set_handoff(db, user, account, "durham-thread", True)
        finally:
            db.rollback()
        assert pending_claim.result(timeout=5) == {"state": "rejected", "dispatch": False}
    assert db.get(InboxReply, automatic).reason == "owner_handoff"
    assert personal.begin(db, manual)["dispatch"] is True


def test_linkedin_setup_channel_claim_and_crm_keep_native_identity(admin_client, db, personal_account, monkeypatch):
    user, facebook, _ = personal_account
    data = {"accounts": [{"channel":"linkedin", "account_id":"li-owned", "user_id":"member-1",
        "engine_provider":"local", "status":"connected", "label":"LinkedIn test account"}]}
    binding = next(b for b in personal.discover(db, data)["accounts"] if b["account_id"] == "li-owned")
    assert not binding["enabled"]
    page = admin_client.get("/inbox/connect")
    assert page.status_code == 200 and "http://127.0.0.1:23765/linkedin" in page.text
    assert admin_client.post("/inbox/connect", data={"account_key":binding["key"],"enabled":"true","brand":"kp"}).status_code == 303
    linkedin = next(a for a in inbox.accounts(db) if a.key == binding["key"])
    personal.ingest(db, {"account_key":linkedin.key, "account_id":linkedin.account_id, "user_id":"member-1",
        "observed_at":utcnow().isoformat()+"Z", "thread":{"id":"li-thread", "name":"Test enquiry", "participant_id":"li-peer",
        "is_group":False, "is_merged":False, "messages":[{"id":"li-1", "body":"Fixture only", "direction":"incoming", "occurred_at":"2026-11-09T12:00:00Z"}]}})
    manual, other = str(uuid.uuid4()), str(uuid.uuid4())
    inbox.send(db, user, linkedin, "li-thread", "Test reply", manual)
    inbox.send(db, user, facebook, "durham-thread", "Separate reply", other)
    assert personal.pending(db, "beeper", "linkedin") == {"reply_ids":[manual], "channel":"linkedin"}
    with pytest.raises(HTTPException):
        personal.begin(db, other, "beeper", "linkedin")
    assert db.get(InboxReply, other).state == "queued"
    job=personal.begin(db, manual, "beeper", "linkedin")
    assert job["dispatch"] and job["channel"]=="linkedin" and job["account_id"]=="li-owned" and job["thread_id"]=="li-thread"
    origins=[]
    def native(actor, brand, action, **payload):
        origins.append(payload["source"])
        return {"brand":brand,"contactId":str(uuid.uuid4()),"workspaceId":str(uuid.uuid4())}
    monkeypatch.setattr(inbox_crm,"command",native)
    inbox_crm.connect(db,user,inbox.ticket(linkedin,"li-thread",user.id,"Test enquiry","li-peer"),"kp","Test enquiry")
    assert origins == [{"accountId":"li-owned","threadId":"li-thread","participantId":"li-peer","channel":"linkedin"}]
