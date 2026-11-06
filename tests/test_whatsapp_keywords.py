import hashlib
import hmac
import json
import time
from datetime import datetime, timezone

import pytest
from app import config
from app.models import KeywordDelivery, KeywordEvent
from app.services import whatsapp_keywords as service

SECRET = 'test-keyword-adapter-secret-' + 'x' * 32
GROUP = '123456789@g.us'
BASE = service.BASE


def signed(client, method, path, data=None, *, stamp=None, secret=SECRET):
    body = b'' if data is None else json.dumps(data, ensure_ascii=False, separators=(',', ':')).encode()
    stamp = str(stamp or int(time.time()))
    signature = hmac.new(secret.encode(), f'{stamp}\n{method}\n{path}\n'.encode() + body, hashlib.sha256).hexdigest()
    return client.request(method, path, content=body, headers={
        'content-type': 'application/json', 'x-ghq-timestamp': stamp, 'x-ghq-signature': signature})


@pytest.fixture(autouse=True)
def adapter_secret(monkeypatch):
    monkeypatch.setattr(config, 'WHATSAPP_KEYWORD_ADAPTER_SECRET', SECRET)


def inventory(client):
    response = signed(client, 'POST', BASE + '/groups', {'listenerId': service.LISTENER,
        'groups': [{'id': GROUP, 'name': 'Income test group'}]})
    assert response.status_code == 200


def create(admin_client, *, revision=0, rule_id='', enabled=True, message='Hi {name}, https://example.test/plan', groups=None, keywords='Income'):
    return admin_client.post('/crm/channels/whatsapp/keywords', data={
        'revision': revision, 'rule_id': rule_id, 'brand': 'mhp', 'name': 'Income action plan', 'keywords': keywords,
        'message': message, 'match_mode': 'exact', 'group_ids': groups if groups is not None else [GROUP],
        'enabled': 'true' if enabled else 'false', 'requested_reply': 'true'})


def event(event_id='message-1', **kw):
    return {'eventId': event_id, 'listenerId': service.LISTENER, 'groupId': GROUP,
            'senderId': '447700900123@c.us', 'senderPhone': '447700900123', 'senderName': 'Emma Test',
            'text': 'INCOME 🙌', 'observedAt': datetime.now(timezone.utc).isoformat(), **kw}


def queued(client, admin_client):
    inventory(client)
    assert create(admin_client).status_code == 303
    response = signed(client, 'POST', BASE + '/events', event())
    assert response.status_code == 200, response.text
    return response.json()['deliveryId']


def test_owner_editor_auth_origin_groups_and_reload(client, admin_client, db):
    page = '/crm/channels/whatsapp/keywords'
    assert client.get(page).status_code == 303
    assert admin_client.get(page).status_code == 200
    assert 'No groups are selected automatically' in admin_client.get(page).text
    assert admin_client.post(page, headers={'Origin': 'https://evil.test'}).status_code == 403
    inventory(client)
    assert create(admin_client, groups=['999999@g.us']).status_code == 400
    assert create(admin_client).status_code == 303
    html = admin_client.get(page)
    assert html.headers['cache-control'] == 'no-store'
    assert 'Income action plan' in html.text and 'Emma Test' not in html.text
    snapshot = signed(client, 'GET', BASE + '/rules').json()
    assert snapshot['revision'] == 1 and snapshot['rules'][0]['brand'] == 'mhp'
    assert snapshot['rules'][0]['groupIds'] == [GROUP]
    assert create(admin_client).status_code == 409  # stale editor
    assert create(admin_client, revision=1).status_code == 409  # overlapping enabled rule


@pytest.mark.parametrize('body,expected', [('Income', True), ('income!', True), ('INCOME 🙌', True), ('my income dropped', False), ('', False)])
def test_matcher_reuses_claude_semantics(body, expected):
    assert service.matches(body, ['Income'], 'exact') is expected


def test_signed_boundary_fails_closed(client):
    assert client.get(BASE + '/rules').status_code == 401
    assert signed(client, 'GET', BASE + '/rules', secret='bad').status_code == 401
    assert signed(client, 'GET', BASE + '/rules', stamp=int(time.time())-121).status_code == 401
    assert signed(client, 'GET', BASE + '/rules?brand=pp').status_code == 400
    assert signed(client, 'POST', BASE + '/groups', {'listenerId': 'another-account', 'groups': []}).status_code == 400
    assert signed(client, 'POST', BASE + '/events', {'privateChat': 'never accepted'}).status_code == 400


def test_same_event_replay_is_effect_free_and_changed_evidence_conflicts(client, admin_client, db):
    inventory(client); assert create(admin_client).status_code == 303
    payload = event()
    first = signed(client, 'POST', BASE + '/events', payload).json()
    again = signed(client, 'POST', BASE + '/events', payload).json()
    assert again == {'outcome': 'replayed', 'deliveryId': first['deliveryId']}
    assert signed(client, 'POST', BASE + '/events', {**payload, 'text': 'Changed'}).status_code == 409
    assert signed(client, 'POST', BASE + '/events', event('message-2')).json()['outcome'] == 'already_contacted'
    assert db.query(KeywordDelivery).count() == 1 and db.query(KeywordEvent).count() == 2


def test_begin_once_result_replay_conflict_and_unknown_never_retries(client, admin_client):
    job = queued(client, admin_client)
    path = BASE + '/deliveries/' + job
    first = signed(client, 'POST', path + '/begin', {}).json()
    assert first['send'] and first['message'] == 'Hi Emma, https://example.test/plan'
    assert signed(client, 'POST', path + '/begin', {}).json() == {'send': False, 'state': 'calling'}
    assert signed(client, 'GET', BASE + '/pending').json() == {'deliveryIds': []}
    result = {'state': 'outcome_unknown', 'providerMessageId': ''}
    assert signed(client, 'POST', path + '/settle', result).json()['ok']
    assert signed(client, 'POST', path + '/settle', result).json()['replayed']
    assert signed(client, 'POST', path + '/settle', {'state': 'sent', 'providerMessageId': 'different'}).status_code == 409
    assert signed(client, 'POST', path + '/begin', {}).json()['send'] is False


def test_edit_holds_pending_and_cannot_change_prepared_message(client, admin_client):
    job = queued(client, admin_client)
    snapshot = signed(client, 'GET', BASE + '/rules').json()
    assert create(admin_client, revision=1, rule_id=snapshot['rules'][0]['id'], enabled=False).status_code == 303
    assert signed(client, 'POST', BASE + '/deliveries/' + job + '/begin', {}).json() == {'send': False, 'state': 'held'}


def test_stop_before_begin_and_future_requests_start_is_channel_specific(client, admin_client, db):
    job = queued(client, admin_client)
    control = {'eventId': 'stop-1', 'senderPhone': '447700900123', 'action': 'STOP',
               'observedAt': datetime.now(timezone.utc).isoformat()}
    assert signed(client, 'POST', BASE + '/opt-outs', control).json()['ok']
    assert signed(client, 'POST', BASE + '/opt-outs', control).json()['replayed']
    assert signed(client, 'POST', BASE + '/opt-outs', {**control, 'action': 'START'}).status_code == 409
    assert signed(client, 'POST', BASE + '/deliveries/' + job + '/begin', {}).json()['send'] is False
    assert signed(client, 'POST', BASE + '/events', event('message-3')).json()['outcome'] == 'opted_out'
    start = {**control, 'eventId': 'start-1', 'action': 'START', 'observedAt': datetime.now(timezone.utc).isoformat()}
    assert signed(client, 'POST', BASE + '/opt-outs', start).json()['ok']
    # Clearing an opt-out never re-sends a previous rule or adds Meta/SMS consent.
    assert signed(client, 'POST', BASE + '/events', event('message-4')).json()['outcome'] == 'already_contacted'
    assert db.query(KeywordDelivery).count() == 1


def test_no_selected_group_no_match_and_html_is_escaped(client, admin_client):
    inventory(client)
    assert create(admin_client, message='<script>bad()</script>').status_code == 303
    assert '&lt;script&gt;bad()&lt;/script&gt;' in admin_client.get('/crm/channels/whatsapp/keywords').text
    assert signed(client, 'POST', BASE + '/events', event(groupId='999999@g.us')).json()['outcome'] == 'ignored'
    assert signed(client, 'POST', BASE + '/events', event(text='my income dropped')).json()['outcome'] == 'ignored'


def test_concurrent_callback_creates_one_delivery(client, admin_client, db):
    from concurrent.futures import ThreadPoolExecutor
    from app.db import SessionLocal
    inventory(client); assert create(admin_client).status_code == 303
    payload = event()
    body = json.dumps(payload, separators=(',', ':')).encode()
    def run(_):
        with SessionLocal() as session:
            return service.ingest(session, payload, body)['outcome']
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(run, range(2))) == ['queued', 'replayed']
    assert db.query(KeywordDelivery).count() == 1


def test_an_older_start_cannot_release_a_newer_stop(client, admin_client):
    from datetime import timedelta
    inventory(client); assert create(admin_client).status_code == 303
    now = datetime.now(timezone.utc)
    stop = {'eventId': 'late-stop', 'senderPhone': '447700900123', 'action': 'STOP', 'observedAt': now.isoformat()}
    start = {**stop, 'eventId': 'older-start', 'action': 'START', 'observedAt': (now-timedelta(seconds=5)).isoformat()}
    assert signed(client, 'POST', BASE + '/opt-outs', stop).status_code == 200
    assert signed(client, 'POST', BASE + '/opt-outs', start).status_code == 200
    assert signed(client, 'POST', BASE + '/events', event('after-controls')).json()['outcome'] == 'opted_out'
