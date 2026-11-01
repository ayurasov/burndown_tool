"""Запись прогноза в задачи Jira и автозаполнение по событиям Jira (вебхук)."""

from datetime import datetime, timedelta, timezone

import pytest

import app as app_module
import jira_integration as jira
from fake_jira import FakeJira
from test_jira import connect, create_button

FIELDS = {'forecast': {'id': 'customfield_10100', 'name': 'Прогноз Burndown', 'type': 'date'},
          'status': {'id': 'customfield_10101', 'name': 'Статус сроков', 'type': 'option'},
          'chance': {'id': 'customfield_10102', 'name': 'Шанс успеть', 'type': 'number'},
          'headline': {'id': 'customfield_10060', 'name': 'Комментарий', 'type': 'string'}}


@pytest.fixture
def fake():
    server = FakeJira()
    yield server
    server.stop()


@pytest.fixture
def ready(admin, fake):
    """Подключение, кнопка (эпик 1 → ABC-1, эпик 2 → ABC-4) и разрешение записи."""
    connect(admin, fake)
    bid = create_button(admin, mappings=[{'serviceId': 1, 'issueKeys': 'ABC-1', 'includeSubtree': True},
                                         {'serviceId': 2, 'issueKeys': 'ABC-4, ABC-5', 'includeSubtree': False}]).get_json()['id']
    assert admin.put('/api/admin/jira-options', json={'writebackAllowed': True}).get_json()['writebackAllowed']
    return bid


# ─── Значения и поля ─────────────────────────────────────────────

def test_field_values_by_type():
    assert jira.field_value('date', '2026-12-01') == '2026-12-01'
    assert jira.field_value('datetime', '2026-12-01') == '2026-12-01T00:00:00.000+0000'
    assert jira.field_value('number', 85) == 85.0 and jira.field_value('option', 'Риск') == {'value': 'Риск'}
    assert jira.field_value('string', 'x' * 400) == 'x' * 250
    assert jira.field_value('date', None) is None and jira.field_value('string', '') is None


def test_writable_fields_filter(admin, fake, ready):
    fields = admin.get('/api/projects/1/jira/writable-fields').get_json()
    ids = {f['id']: f['type'] for f in fields}
    assert ids == {'customfield_10050': 'number', 'customfield_10060': 'string', 'customfield_10100': 'date',
                   'customfield_10101': 'option', 'customfield_10102': 'number', 'customfield_10103': 'datetime'}
    assert [f['name'] for f in fields] == sorted((f['name'] for f in fields), key=str.lower)
    assert jira.writable_fields([None, {'id': 'bad id', 'custom': True, 'schema': {'type': 'date'}}]) == []


def test_epic_values_for_each_kind():
    e = {'forecast': '2026-12-01', 'noBurn': False, 'tooFar': False, 'finished': False, 'status': 'risk',
         'probability': 0.456, 'headline': {'text': 'Нужен темп выше'}}
    string_fields = {k: dict(f, type='string') for k, f in FIELDS.items() if k != 'status'}
    _, shown = app_module.writeback_values(e, string_fields)
    assert shown == {'forecast': '01.12.2026', 'chance': '46 %', 'headline': 'Нужен темп выше'}
    values, shown = app_module.writeback_values(dict(e, noBurn=True, forecast=None, probability=None, headline=None),
                                                FIELDS)
    assert values == {'customfield_10100': None, 'customfield_10101': {'value': 'Риск'}, 'customfield_10102': None,
                      'customfield_10060': None}
    assert app_module.writeback_values(dict(e, tooFar=True), {'forecast': dict(FIELDS['forecast'], type='string')})[1] == \
        {'forecast': '> 3 лет'}
    assert app_module.writeback_values(dict(e, finished=True), {'status': FIELDS['status']})[1] == {'status': 'Завершён'}


# ─── Настройка и запись ──────────────────────────────────────────

def test_writeback_requires_admin_permission(admin, fake, make_user):
    connect(admin, fake)
    create_button(admin)
    r = admin.put('/api/projects/1/jira/writeback', json={'enabled': True, 'fields': FIELDS})
    assert r.status_code == 403 and 'не разрешена' in r.get_json()['error']
    assert admin.post('/api/projects/1/jira/writeback/run').status_code == 403
    data = admin.get('/api/projects/1/jira/writeback').get_json()
    assert data['allowed'] is False and data['connected'] is True and len(data['kinds']) == 4
    viewer = make_user('viewer', project_id=1)
    assert viewer.get('/api/projects/1/jira/writeback').status_code == 403
    assert viewer.get('/api/projects/1/jira/writable-fields').status_code == 403
    assert admin.get('/api/projects/999/jira/writeback').status_code == 404
    with app_module.app.app_context():  # без разрешения автоматическая запись не идёт
        assert app_module.run_due_writebacks() == 0


def test_writeback_validation(admin, ready):
    cases = [({'enabled': True, 'fields': {}}, 'хотя бы одно'),
             ({'fields': ['x']}, 'объект'),
             ({'fields': {'forecast': 'x'}}, 'некорректное'),
             ({'fields': {'forecast': {'id': 'duedate', 'type': 'date'}}}, 'пользовательское'),
             ({'fields': {'status': {'id': 'customfield_10100', 'type': 'date'}}}, 'не подходит'),
             ({'fields': {'forecast': FIELDS['forecast'], 'chance': dict(FIELDS['forecast'], type='number')}}, 'двух')]
    for body, message in cases:
        r = admin.put('/api/projects/1/jira/writeback', json=body)
        assert r.status_code == 400 and message in r.get_json()['error'], (body, r.get_json())


def test_writeback_writes_only_changes(admin, fake, ready, flask_app):
    r = admin.put('/api/projects/1/jira/writeback', json={'enabled': True, 'fields': FIELDS})
    data = r.get_json()
    assert r.status_code == 200 and data['enabled'] and data['fields']['status']['name'] == 'Статус сроков'
    plan = {e['serviceId']: e for e in data['epics']}
    assert plan[1]['issue'] == 'ABC-1' and plan[2]['issue'] == 'ABC-4' and plan[3]['issue'] is None
    assert plan[1]['changed'] and set(plan[1]['values']) == set(FIELDS)
    result = admin.post('/api/projects/1/jira/writeback/run').get_json()
    assert result['result'] == {'status': 'ok', 'message': 'Записано в Jira: 2 из 2'}
    writes = dict(fake.state['writes'])
    assert set(writes) == {'ABC-1', 'ABC-4'} and set(writes['ABC-1']) == {f['id'] for f in FIELDS.values()}
    assert writes['ABC-1']['customfield_10101']['value'] in jira_status_names()
    assert not any(e['changed'] for e in result['epics'])
    again = admin.post('/api/projects/1/jira/writeback/run').get_json()['result']
    assert again['message'] == 'Изменений нет — в Jira всё актуально' and len(fake.state['writes']) == 2
    forced = admin.post('/api/projects/1/jira/writeback/run', json={'force': True}).get_json()['result']
    assert forced['message'] == 'Записано в Jira: 2 из 2' and len(fake.state['writes']) == 4
    admin.put('/api/services/1', json={'targetDate': '2030-01-01'})  # изменились данные — меняется статус эпика 1
    assert admin.post('/api/projects/1/jira/writeback/run').get_json()['result']['message'] == 'Записано в Jira: 1 из 1'
    audit = admin.get('/api/audit?page=jira').get_json()['entries']
    assert any(a['entityType'] == 'jira_writeback' for a in audit)


def jira_status_names():
    return set(app_module.WRITEBACK_STATUS.values()) | {'Завершён'}


def test_writeback_errors_are_reported(admin, fake, ready):
    admin.put('/api/projects/1/jira/writeback', json={'enabled': True, 'fields': FIELDS})
    fake.configure(readonly_fields={'customfield_10102'})
    result = admin.post('/api/projects/1/jira/writeback/run').get_json()['result']
    assert result['status'] == 'error' and 'ABC-1' in result['message'] and 'cannot be set' in result['message']
    data = admin.get('/api/projects/1/jira/writeback').get_json()
    assert data['lastStatus'] == 'error' and all(e['changed'] for e in data['epics'] if e['issue'])
    fake.configure(readonly_fields=set())
    admin.put('/api/projects/1/jira/writeback', json={'enabled': True, 'fields': {'forecast': FIELDS['forecast']}})
    with app_module.app.app_context():
        db = app_module.get_db()
        db.execute("UPDATE jira_snapshot_mappings SET issue_keys = 'ABC-404' WHERE service_id = 2")
        db.commit()
    partial = admin.post('/api/projects/1/jira/writeback/run').get_json()['result']
    assert partial['status'] == 'warning' and 'ABC-404' in partial['message'] and '1 из 2' in partial['message']
    admin.delete('/api/admin/jira-connection')
    failed = admin.post('/api/projects/1/jira/writeback/run', json={'force': True}).get_json()['result']
    assert failed['status'] == 'error' and 'не настроено' in failed['message']


def test_writeback_without_fields_and_many_errors(admin, fake, ready, flask_app, monkeypatch):
    with flask_app.app_context():
        assert app_module.run_writeback(app_module.get_db(), 1) == ('skip', 'Не выбраны поля Jira для записи')
    admin.put('/api/projects/1/jira/writeback', json={'enabled': True, 'fields': {'forecast': FIELDS['forecast']}})
    monkeypatch.setattr(app_module, 'WRITEBACK_MAX_ERRORS', 1)
    fake.configure(readonly_fields={'customfield_10100'})
    result = admin.post('/api/projects/1/jira/writeback/run').get_json()['result']
    assert result['message'].startswith('Записано в Jira: 0 из 2') and result['message'].count('cannot be set') == 1


def test_scheduled_writeback(admin, fake, ready, flask_app):
    admin.put('/api/projects/1/jira/writeback', json={'enabled': True, 'fields': {'status': FIELDS['status']}})
    with flask_app.app_context():
        assert app_module.run_due_writebacks() == 1
        assert app_module.run_due_writebacks() == 0  # данные не менялись
    assert len(fake.state['writes']) == 2
    admin.put('/api/services/1', json={'targetDate': '2026-07-20'})  # цель в прошлом — статус эпика меняется
    with flask_app.app_context():
        assert app_module.run_due_writebacks() == 0  # слишком рано после прошлой записи
        later = datetime.now(timezone.utc) + timedelta(seconds=app_module.WRITEBACK_MIN_INTERVAL + 5)
        assert app_module.run_due_writebacks(later) == 1
    assert len(fake.state['writes']) == 3
    admin.delete('/api/admin/jira-connection')
    admin.put('/api/services/1', json={'targetDate': '2031-01-01'})  # статус снова меняется, а Jira не подключена
    with flask_app.app_context():
        later = datetime.now(timezone.utc) + timedelta(seconds=2 * app_module.WRITEBACK_MIN_INTERVAL)
        assert app_module.run_due_writebacks(later) == 1
        row = app_module.get_db().execute('SELECT * FROM jira_writeback WHERE project_id = 1').fetchone()
    assert row['last_status'] == 'error' and row['last_version'] is None  # повторится при следующей проверке


def test_scheduled_writeback_survives_crash(admin, fake, ready, flask_app, monkeypatch):
    admin.put('/api/projects/1/jira/writeback', json={'enabled': True, 'fields': {'status': FIELDS['status']}})
    monkeypatch.setattr(app_module, 'run_writeback', lambda *a, **k: 1 / 0)
    with flask_app.app_context():
        assert app_module.run_due_writebacks() == 1


def test_writeback_settings_audit_and_disable(admin, ready):
    admin.put('/api/projects/1/jira/writeback', json={'enabled': True, 'fields': {'forecast': FIELDS['forecast']}})
    data = admin.put('/api/projects/1/jira/writeback', json={'enabled': False, 'fields': {}}).get_json()
    assert data['enabled'] is False and data['fields'] == {}
    audit = admin.get('/api/audit?page=jira').get_json()['entries']
    assert {a['fieldName'] for a in audit if a['entityType'] == 'jira_writeback'} >= {'enabled', 'forecast'}
    with app_module.app.app_context():
        db = app_module.get_db()
        db.execute("UPDATE jira_writeback SET fields = 'не json' WHERE project_id = 1")
        db.commit()
    assert admin.get('/api/projects/1/jira/writeback').get_json()['fields'] == {}


# ─── Вебхук Jira ─────────────────────────────────────────────────

def test_webhook_issue_keys():
    payload = {'webhookEvent': 'jira:issue_updated',
               'issue': {'key': 'ABC-2', 'fields': {'parent': {'key': 'ABC-1'}, 'customfield_10014': 'ABC-7',
                                                    'summary': 'не ключ', 'labels': ['ABC-8']}}}
    assert app_module.webhook_issue_keys(payload) == {'ABC-2', 'ABC-1', 'ABC-7'}
    assert app_module.webhook_issue_keys({'issue': 'x'}) == set() and app_module.webhook_issue_keys([]) == set()
    assert app_module.webhook_issue_keys({'issue': {'key': 'плохой'}}) == set()


def test_webhook_admin_options(admin, client, make_user):
    options = admin.get('/api/admin/jira-options').get_json()
    assert options == {'writebackAllowed': False, 'webhook': {'enabled': False, 'url': '', 'last': None}}
    on = admin.put('/api/admin/jira-options', json={'webhook': True}).get_json()['webhook']
    assert on['enabled'] and on['url'].startswith('http://localhost/api/jira/webhook/')
    admin.put('/api/admin/notify-settings', json={'publicUrl': 'https://burndown.example.ru'})
    same = admin.put('/api/admin/jira-options', json={'webhook': True}).get_json()['webhook']
    assert same['url'] == 'https://burndown.example.ru/api/jira/webhook/' + on['url'].rsplit('/', 1)[1]
    rotated = admin.put('/api/admin/jira-options', json={'webhook': True, 'rotate': True}).get_json()['webhook']
    assert rotated['url'] != same['url']
    assert client.post(same['url'].replace('https://burndown.example.ru', ''), json={}).status_code == 404  # старый адрес
    off = admin.put('/api/admin/jira-options', json={'webhook': False}).get_json()['webhook']
    assert off == {'enabled': False, 'url': '', 'last': None}
    assert client.post(rotated['url'].replace('https://burndown.example.ru', ''), json={}).status_code == 404
    assert client.post('/api/jira/webhook/секрет', json={}).status_code == 404  # не-ASCII в адресе — тоже 404
    assert make_user('ivan').get('/api/admin/jira-options').status_code == 403
    audit = admin.get('/api/audit?page=integrations').get_json()['entries']
    assert any(a['entityType'] == 'jira_options' for a in audit)


def webhook_path(admin):
    url = admin.put('/api/admin/jira-options', json={'webhook': True}).get_json()['webhook']['url']
    return url.replace('http://localhost', '')


def enable_events(admin, bid, on=True):
    assert admin.post(f'/api/jira/buttons/{bid}/snapshot', json={}).status_code == 200  # первый успешный срез
    r = admin.put(f'/api/jira/buttons/{bid}/schedule', json={'enabled': False, 'days': [1], 'time': '09:00',
                                                            'timezone': 'Europe/Moscow', 'onEvents': on})
    assert r.status_code == 200, r.get_json()
    return r.get_json()['schedule']


def test_webhook_queues_and_runs_autofill(admin, client, fake, ready, flask_app):
    path = webhook_path(admin)
    issue_event = {'webhookEvent': 'jira:issue_updated', 'issue': {'key': 'ABC-3', 'fields': {}}}
    assert client.post(path, json=issue_event).get_json() == {'queued': 0}  # «по событиям» ещё не включено
    sched = enable_events(admin, ready)
    assert sched['onEvents'] is True and sched['eventsRunAt'] is None
    assert client.post(path, json=issue_event).get_json() == {'queued': 1}  # ABC-3 — в последнем срезе эпика 1
    assert client.post(path, json={'issue': {'key': 'ABC-5'}}).get_json() == {'queued': 1}  # в привязке эпика 2
    assert client.post(path, json={'issue': {'key': 'XYZ-1'}}).get_json() == {'queued': 0}
    last = admin.get('/api/admin/jira-options').get_json()['webhook']['last']
    assert last['keys'] == ['XYZ-1'] and last['buttons'] == 0
    with flask_app.app_context():
        db = app_module.get_db()
        item = db.execute('SELECT * FROM jira_event_queue').fetchone()
        assert item['events'] == 2 and item['last_keys'] == 'ABC-5'
        assert app_module.run_due_events() == 0  # серия правок: ждём WEBHOOK_DELAY
        later = datetime.now(timezone.utc) + timedelta(seconds=app_module.WEBHOOK_DELAY + 5)
        assert app_module.run_due_events(later) == 1
        assert db.execute('SELECT COUNT(*) FROM jira_event_queue').fetchone()[0] == 0
    button = admin.get(f'/api/projects/1/jira').get_json()['buttons'][0]
    assert button['schedule']['eventsStatus'] == 'ok' and 'событий Jira: 2' in button['schedule']['eventsMessage']
    # следующий запуск — не раньше чем через WEBHOOK_MIN_INTERVAL после прошлого
    client.post(path, json=issue_event)
    with flask_app.app_context():
        due = app_module.get_db().execute('SELECT due_at FROM jira_event_queue').fetchone()[0]
    soon = (datetime.now(timezone.utc) + timedelta(seconds=app_module.WEBHOOK_DELAY + 5)).strftime('%Y-%m-%d %H:%M:%S')
    assert due > soon
    audit = admin.get('/api/audit?page=jira').get_json()['entries']
    assert any(a['username'] == app_module.WEBHOOK_ACTOR for a in audit)


def test_webhook_event_errors_and_disable(admin, client, fake, ready, flask_app, monkeypatch):
    path = webhook_path(admin)
    enable_events(admin, ready)
    client.post(path, json={'issue': {'key': 'ABC-1'}})
    fake.configure(token='другой')  # Jira отклоняет токен
    later = datetime.now(timezone.utc) + timedelta(seconds=app_module.WEBHOOK_DELAY + 5)
    with flask_app.app_context():
        assert app_module.run_due_events(later) == 1
    sched = admin.get('/api/projects/1/jira').get_json()['buttons'][0]['schedule']
    assert sched['eventsStatus'] == 'error'
    client.post(path, json={'issue': {'key': 'ABC-1'}})
    monkeypatch.setattr(app_module, 'run_autofill', lambda *a, **k: 1 / 0)
    later = datetime.now(timezone.utc) + timedelta(seconds=app_module.WEBHOOK_MIN_INTERVAL + 5)
    with flask_app.app_context():
        assert app_module.run_due_events(later) == 1
    assert 'Внутренняя ошибка' in admin.get('/api/projects/1/jira').get_json()['buttons'][0]['schedule']['eventsMessage']
    client.post(path, json={'issue': {'key': 'ABC-1'}})
    fake.configure(token='secret-pat')
    enable_events(admin, ready, on=False)  # выключили «по событиям» — очередь кнопки очищается
    with flask_app.app_context():
        assert app_module.get_db().execute('SELECT COUNT(*) FROM jira_event_queue').fetchone()[0] == 0
        db = app_module.get_db()
        db.execute("INSERT INTO jira_event_queue (button_id, due_at) VALUES (?, '2020-01-01 00:00:00')", (ready,))
        db.commit()
        assert app_module.run_due_events() == 0  # кнопка без «по событиям» — запуск пропускается


def test_on_events_needs_successful_snapshot(admin, fake, ready):
    r = admin.put(f'/api/jira/buttons/{ready}/schedule', json={'enabled': False, 'days': [1], 'time': '09:00',
                                                              'timezone': 'Europe/Moscow', 'onEvents': True})
    assert r.status_code == 400 and 'первого успешного среза' in r.get_json()['error']
