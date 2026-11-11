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


# ---- pacing: one private reply at a time ------------------------------------------------------------------------------
def begun(db, seconds_ago):
    """Pretend the last reply began that long ago, as time passing would."""
    from datetime import timedelta
    from app.models import utcnow
    db.query(KeywordDelivery).filter(KeywordDelivery.begun_at.isnot(None)).update({'begun_at': utcnow() - timedelta(seconds=seconds_ago)})
    db.commit()


def test_pending_releases_one_reply_and_none_within_the_gap_of_the_last_one_begun(client, admin_client, db):
    first = queued(client, admin_client)
    others = [signed(client, 'POST', BASE + '/events', event(f'message-{n}', senderId=f'44770090020{n}@c.us',
              senderPhone=f'44770090020{n}')).json()['deliveryId'] for n in (2, 3, 4)]
    assert signed(client, 'GET', BASE + '/pending').json() == {'deliveryIds': [first]}   # the oldest, and only one
    assert signed(client, 'GET', BASE + '/pending').json() == {'deliveryIds': [first]}   # asking again releases nothing more
    assert signed(client, 'POST', BASE + '/deliveries/' + first + '/begin', {}).json()['send']
    assert signed(client, 'GET', BASE + '/pending').json() == {'deliveryIds': []}
    begun(db, service.SEND_GAP - 2)
    assert signed(client, 'GET', BASE + '/pending').json() == {'deliveryIds': []}
    begun(db, service.SEND_GAP + 1)
    assert signed(client, 'GET', BASE + '/pending').json() == {'deliveryIds': [others[0]]}
    # A reply the rules no longer allow is held at once, as begin would hold it, so it cannot delay the queue behind it.
    snapshot = signed(client, 'GET', BASE + '/rules').json()
    assert create(admin_client, revision=1, rule_id=snapshot['rules'][0]['id']).status_code == 303
    assert signed(client, 'GET', BASE + '/pending').json() == {'deliveryIds': []}
    assert sorted(j.state for j in db.query(KeywordDelivery)) == ['calling', 'held', 'held', 'held']
    assert signed(client, 'POST', BASE + '/deliveries/' + others[0] + '/begin', {}).json() == {'send': False, 'state': 'held'}


# ---- catch-up: an explicit, bounded request to answer earlier messages ------------------------------------------------
PAGE = '/crm/channels/whatsapp/keywords'


def past(event_id, hours_ago, **kw):
    from datetime import timedelta
    return event(event_id, observedAt=(datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat(), **kw)


def person(n):
    return {'senderId': f'4477009003{n:02d}@c.us', 'senderPhone': f'4477009003{n:02d}', 'senderName': f'Person {n}'}


def rule_id(client):
    return signed(client, 'GET', BASE + '/rules').json()['rules'][0]['id']


def catch_up(client, admin_client, hours=48, confirmed=True, rule=None):
    data = {'rule_id': rule or rule_id(client), 'hours': hours}
    return admin_client.post(PAGE + '/catchup', data={**data, 'requested_reply': 'true'} if confirmed else data)


def opened(client, admin_client, **kw):
    inventory(client)
    assert create(admin_client).status_code == 303
    assert catch_up(client, admin_client, **kw).status_code == 303
    [snapshot] = signed(client, 'GET', BASE + '/rules').json()['catchups']
    return snapshot


def submit(client, catchup, event_id, hours_ago=3.0, **kw):
    response = signed(client, 'POST', BASE + '/catchup-events', {**past(event_id, hours_ago, **kw), 'catchupId': catchup['id']})
    assert response.status_code == 200, response.text
    return response.json()


def progress(client, catchup, **kw):
    return signed(client, 'POST', f"{BASE}/catchups/{catchup['id']}/progress", {
        'listenerId': service.LISTENER, 'groupId': GROUP, 'status': 'scanned', 'reachedSince': True, 'scanned': 40, 'matched': 3,
        'unresolved': 1, **kw})


def test_a_catch_up_needs_an_enabled_rule_the_owners_confirmation_and_no_other_running(client, admin_client, db):
    from app.models import AuditLog
    inventory(client)
    assert signed(client, 'GET', BASE + '/rules').json()['catchups'] == []
    assert create(admin_client, enabled=False).status_code == 303
    rule = rule_id(client)
    assert client.post(PAGE + '/catchup', data={'rule_id': rule}).status_code == 303                     # signed out: to the login page
    assert admin_client.post(PAGE + '/catchup', headers={'Origin': 'https://evil.test'}, data={'rule_id': rule}).status_code == 403
    paused = catch_up(client, admin_client)
    assert paused.status_code == 400 and 'Enable this rule before catching up' in paused.text
    assert create(admin_client, revision=1, rule_id=rule).status_code == 303
    unconfirmed = catch_up(client, admin_client, confirmed=False)
    assert unconfirmed.status_code == 400 and 'were told this keyword requests this private reply' in unconfirmed.text
    for hours in (0, 169):
        assert catch_up(client, admin_client, hours=hours).status_code == 400
    assert catch_up(client, admin_client, rule='0f8fad5b-d9cb-469f-a165-70867728950e').status_code == 404
    assert signed(client, 'GET', BASE + '/rules').json()['catchups'] == [] and service.catchups(db) == []

    started = catch_up(client, admin_client, hours=24)
    assert started.status_code == 303 and 'ok=Catch-up+requested' in started.headers['location']
    [item] = service.catchups(db)
    assert (item['state'], item['ruleId'], item['revision'], item['hours'], item['requestedBy']) == ('open', rule, 2, 24, 'founder@example.test')
    since, until, expires = (datetime.fromisoformat(item[k]) for k in ('since', 'until', 'expiresAt'))
    assert (until - since).total_seconds() == 24 * 3600 and (expires - until).total_seconds() == service.CATCHUP_EXPIRY
    assert abs((datetime.now(timezone.utc) - until).total_seconds()) < 30
    snapshot = signed(client, 'GET', BASE + '/rules').json()
    assert snapshot['schemaVersion'] == 1 and snapshot['revision'] == 2          # the contract an older listener reads is unchanged
    assert snapshot['catchups'] == [{'id': item['id'], 'ruleId': rule, 'since': item['since'], 'until': item['until'],
                                     'expiresAt': item['expiresAt'], 'groupIds': [GROUP], 'pendingGroupIds': [GROUP]}]
    again = catch_up(client, admin_client)
    assert again.status_code == 409 and 'A catch-up is already running' in again.text
    assert len(service.catchups(db)) == 1
    row = db.query(AuditLog).filter(AuditLog.action == 'whatsapp.keyword.catchup').one()
    assert row.entity == 'whatsapp-keyword:' + rule and row.after['hours'] == 24 and row.actor == 'founder@example.test'
    html = admin_client.get(PAGE).text
    assert 'Catch up on earlier messages' in html and 'Cancel catch-up' in html and 'waiting for the listener' in html
    assert 'about one every 15 seconds' in html and 'answered only once' in html and 'up to 300 replies' in html


def test_catch_up_admits_only_its_rule_groups_and_window_then_the_live_rules(client, admin_client, db):
    catchup = opened(client, admin_client)
    old = past('message-1', 3)
    live = signed(client, 'POST', BASE + '/events', old)
    assert live.status_code == 400 and 'outside its admission window' in live.text   # the live rule is untouched
    assert signed(client, 'POST', BASE + '/catchup-events', old).status_code == 400   # no catch-up named
    assert signed(client, 'POST', BASE + '/catchup-events', {**old, 'catchupId': 'not-a-uuid'}).status_code == 400
    assert signed(client, 'POST', BASE + '/catchup-events', {**old, 'catchupId': catchup['id'], 'extra': 'x'}).status_code == 400

    first = submit(client, catchup, 'message-1')
    assert first['outcome'] == 'queued'
    job = db.get(KeywordDelivery, first['deliveryId'])
    assert (job.state, job.reply, job.sender_phone, job.rule_revision) == ('pending', 'Hi Emma, https://example.test/plan', '447700900123', 1)
    assert submit(client, catchup, 'message-1') == {'outcome': 'replayed', 'deliveryId': first['deliveryId']}
    assert submit(client, catchup, 'message-2', 5)['outcome'] == 'already_contacted'      # once per person per rule
    assert submit(client, catchup, 'message-3', groupId='999999@g.us', **person(1))['outcome'] == 'ignored'
    assert submit(client, catchup, 'message-4', text='my income dropped', **person(1))['outcome'] == 'ignored'
    assert submit(client, catchup, 'message-5', 49, **person(1))['outcome'] == 'outside_window'
    assert submit(client, catchup, 'message-6', -0.2, **person(1))['outcome'] == 'outside_window'   # after the request: live's job
    assert submit(client, catchup, 'message-7', 47.99, **person(2))['outcome'] == 'queued'
    edge = submit(client, catchup, 'message-8', -0.01, **person(3))                         # inside the clock tolerance
    assert edge['outcome'] == 'queued'
    stop = {'eventId': 'stop-1', 'senderPhone': person(4)['senderPhone'], 'action': 'STOP', 'observedAt': datetime.now(timezone.utc).isoformat()}
    assert signed(client, 'POST', BASE + '/opt-outs', stop).json()['ok']
    assert submit(client, catchup, 'message-9', **person(4)) == {'outcome': 'opted_out', 'deliveryId': None}
    unknown = {**past('message-10', 3, **person(5)), 'catchupId': '0f8fad5b-d9cb-469f-a165-70867728950e'}
    assert signed(client, 'POST', BASE + '/catchup-events', unknown).json() == {'outcome': 'closed', 'deliveryId': None}
    assert db.query(KeywordDelivery).count() == 3
    # Recorded: the queued ones, the already-contacted one and the opted-out one; never an ignored or out-of-window message.
    assert sorted(e.external_id for e in db.query(KeywordEvent)) == ['message-1', 'message-2', 'message-7', 'message-8', 'message-9', 'stop-1']
    [item] = service.catchups(db)
    assert item['counts'] == {'queued': 3, 'already_contacted': 1, 'opted_out': 1, 'over_cap': 0, 'replayed': 1, 'outside_window': 2, 'ignored': 2}
    assert sorted(item['deliveryIds']) == sorted(j.id for j in db.query(KeywordDelivery))


def test_a_message_answered_live_is_one_event_when_a_catch_up_finds_it_again(client, admin_client, db):
    inventory(client); assert create(admin_client).status_code == 303
    live = event('message-1')
    answered = signed(client, 'POST', BASE + '/events', live).json()
    assert answered['outcome'] == 'queued'
    assert catch_up(client, admin_client).status_code == 303
    [catchup] = signed(client, 'GET', BASE + '/rules').json()['catchups']
    # The same WhatsApp message, read back from the phone a little later with a slightly different name: no conflict, no second reply.
    again = signed(client, 'POST', BASE + '/catchup-events', {**live, 'senderName': 'Emma', 'catchupId': catchup['id']})
    assert again.status_code == 200 and again.json() == {'outcome': 'replayed', 'deliveryId': answered['deliveryId']}
    assert submit(client, catchup, 'message-2')['outcome'] == 'already_contacted'
    assert db.query(KeywordDelivery).count() == 1
    assert signed(client, 'POST', BASE + '/events', {**live, 'text': 'Changed'}).status_code == 409   # live evidence rule unchanged


def test_catch_up_cap_counts_further_matches_without_queueing_or_recording_them(client, admin_client, db, monkeypatch):
    monkeypatch.setattr(service, 'CATCHUP_CAP', 2)
    catchup = opened(client, admin_client)
    assert [submit(client, catchup, f'message-{n}', **person(n))['outcome'] for n in (1, 2, 3, 4)] == ['queued', 'queued', 'over_cap', 'over_cap']
    assert submit(client, catchup, 'message-5', **person(1))['outcome'] == 'already_contacted'   # no new reply, so the cap does not matter
    assert db.query(KeywordDelivery).count() == 2
    assert 'message-3' not in [e.external_id for e in db.query(KeywordEvent)]                   # a later catch-up may still answer them
    [item] = service.catchups(db)
    assert (item['counts']['queued'], item['counts']['over_cap']) == (2, 2)
    assert '2 over the 2 limit, not queued' in admin_client.get(PAGE).text


def test_progress_from_every_group_finishes_the_catch_up_and_queued_replies_still_go(client, admin_client, db):
    catchup = opened(client, admin_client)
    queued_job = submit(client, catchup, 'message-1')['deliveryId']
    assert progress(client, catchup, listenerId='another').status_code == 400
    assert progress(client, catchup, groupId='999999@g.us').status_code == 400
    assert progress(client, catchup, status='finished').status_code == 400
    assert progress(client, catchup, scanned=-1).status_code == 400
    assert progress(client, catchup, reachedSince='yes').status_code == 400
    assert signed(client, 'POST', f"{BASE}/catchups/not-a-uuid/progress", {}).status_code == 400
    assert service.catchups(db)[0]['state'] == 'open'
    done = progress(client, catchup)
    assert done.status_code == 200 and done.json() == {'ok': True, 'state': 'done'}
    assert progress(client, catchup).json() == {'ok': True, 'state': 'done'}                 # a repeated report changes nothing
    [item] = service.catchups(db)
    [group] = item['groups']
    assert (item['state'], group['status'], group['reachedSince'], group['scanned'], group['matched'], group['unresolved']) == ('done', 'scanned', True, 40, 3, 1)
    assert signed(client, 'GET', BASE + '/rules').json()['catchups'] == []
    assert submit(client, catchup, 'message-2', **person(1)) == {'outcome': 'closed', 'deliveryId': None}
    assert signed(client, 'GET', BASE + '/pending').json() == {'deliveryIds': [queued_job]}
    assert signed(client, 'POST', BASE + '/deliveries/' + queued_job + '/begin', {}).json()['send'] is True
    html = admin_client.get(PAGE).text
    assert 'Finished reading' in html and '40 messages read, 3 with the keyword, 1 without a phone number (not messaged)' in html
    assert 'Cancel catch-up' not in html
    assert catch_up(client, admin_client).status_code == 303                                  # a new one may start now


def test_an_unavailable_group_is_reported_and_shown(client, admin_client, db):
    catchup = opened(client, admin_client)
    assert progress(client, catchup, status='unavailable', reachedSince=True, scanned=0, matched=0, unresolved=0).json()['state'] == 'done'
    assert service.catchups(db)[0]['groups'][0]['reachedSince'] is False
    assert 'earlier messages were not available' in admin_client.get(PAGE).text


def test_cancel_stops_admission_and_holds_the_replies_not_yet_sent(client, admin_client, db):
    from app.models import AuditLog
    catchup = opened(client, admin_client)
    sent = submit(client, catchup, 'message-1')['deliveryId']
    waiting = submit(client, catchup, 'message-2', **person(1))['deliveryId']
    assert signed(client, 'POST', BASE + '/deliveries/' + sent + '/begin', {}).json()['send']
    assert admin_client.post(PAGE + '/catchup/cancel', headers={'Origin': 'https://evil.test'}, data={'catchup_id': catchup['id']}).status_code == 403
    assert admin_client.post(PAGE + '/catchup/cancel', data={'catchup_id': '0f8fad5b-d9cb-469f-a165-70867728950e'}).status_code == 404
    cancelled = admin_client.post(PAGE + '/catchup/cancel', data={'catchup_id': catchup['id']})
    assert cancelled.status_code == 303 and '1+reply+not+yet+sent+will+not+go+out' in cancelled.headers['location']
    assert {j.id: j.state for j in db.query(KeywordDelivery)} == {sent: 'calling', waiting: 'held'}
    assert signed(client, 'POST', BASE + '/deliveries/' + waiting + '/begin', {}).json() == {'send': False, 'state': 'held'}
    assert signed(client, 'GET', BASE + '/rules').json()['catchups'] == []
    assert submit(client, catchup, 'message-3', **person(2)) == {'outcome': 'closed', 'deliveryId': None}
    assert progress(client, catchup).json() == {'ok': True, 'state': 'cancelled'}
    [item] = service.catchups(db)
    assert (item['state'], item['reason']) == ('cancelled', 'owner') and item['groups'][0]['status'] == 'pending'
    assert admin_client.post(PAGE + '/catchup/cancel', data={'catchup_id': catchup['id']}).status_code == 409
    row = db.query(AuditLog).filter(AuditLog.action == 'whatsapp.keyword.catchup.cancel').one()
    assert row.after == {'catchupId': catchup['id'], 'state': 'cancelled', 'held': 1}
    assert 'Cancelled' in admin_client.get(PAGE).text
    # The rule itself still answers new messages.
    assert signed(client, 'POST', BASE + '/events', event('message-9', **person(3))).json()['outcome'] == 'queued'


def test_editing_any_rule_ends_the_catch_up_and_holds_its_unsent_replies(client, admin_client, db):
    catchup = opened(client, admin_client)
    waiting = submit(client, catchup, 'message-1')['deliveryId']
    assert create(admin_client, revision=1, rule_id=catchup['ruleId'], message='Hi {name}, a new link').status_code == 303
    [item] = service.catchups(db)
    assert (item['state'], item['reason']) == ('cancelled', 'rules_changed') and item['closedAt']
    assert signed(client, 'GET', BASE + '/rules').json()['catchups'] == []
    assert submit(client, catchup, 'message-2', **person(1)) == {'outcome': 'closed', 'deliveryId': None}
    assert signed(client, 'POST', BASE + '/deliveries/' + waiting + '/begin', {}).json() == {'send': False, 'state': 'held'}
    assert db.query(KeywordDelivery).count() == 1
    assert 'Stopped because the rules changed' in admin_client.get(PAGE).text


def test_a_catch_up_the_listener_never_finishes_expires_after_two_hours(client, admin_client, db, monkeypatch):
    from datetime import timedelta
    catchup = opened(client, admin_client)
    waiting = submit(client, catchup, 'message-1')['deliveryId']
    real = service._now
    monkeypatch.setattr(service, '_now', lambda: real() + timedelta(seconds=service.CATCHUP_EXPIRY - 5))
    assert len(signed(client, 'GET', BASE + '/rules').json()['catchups']) == 1
    monkeypatch.setattr(service, '_now', lambda: real() + timedelta(seconds=service.CATCHUP_EXPIRY + 5))
    assert signed(client, 'GET', BASE + '/rules').json()['catchups'] == []
    assert submit(client, catchup, 'message-2', **person(1)) == {'outcome': 'closed', 'deliveryId': None}
    assert progress(client, catchup).json() == {'ok': True, 'state': 'expired'}
    assert 'Stopped after two hours' in admin_client.get(PAGE).text
    assert signed(client, 'GET', BASE + '/pending').json() == {'deliveryIds': [waiting]}       # what was queued in time still goes
    assert catch_up(client, admin_client).status_code == 303                                   # and the owner may ask again
    assert [c['state'] for c in service.catchups(db)] == ['open', 'expired']


def test_two_catch_up_callbacks_for_one_person_queue_one_reply(client, admin_client, db):
    from concurrent.futures import ThreadPoolExecutor
    from app.db import SessionLocal
    catchup = opened(client, admin_client)
    payloads = [{**past(f'message-{n}', 3 + n), 'catchupId': catchup['id']} for n in (1, 2)]
    def run(payload):
        with SessionLocal() as session:
            return service.ingest_catchup(session, payload, json.dumps(payload, separators=(',', ':')).encode())['outcome']
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(run, payloads)) == ['already_contacted', 'queued']
    assert db.query(KeywordDelivery).count() == 1 and service.catchups(db)[0]['counts']['queued'] == 1
