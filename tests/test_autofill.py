"""Сопоставление полей Jira и автозаполнение замеров по расписанию."""

from datetime import datetime, timezone

import pytest

import app as app_module
from fake_jira import FakeJira
from test_jira import connect, create_button, snapshot

MONDAY_0930_MSK = datetime(2026, 11, 2, 6, 30, tzinfo=timezone.utc)
MONDAY = '2026-11-02'


@pytest.fixture
def fake():
    server = FakeJira()
    yield server
    server.stop()


@pytest.fixture
def button(admin, fake):
    connect(admin, fake)
    return create_button(admin).get_json()


def schedule(admin, bid, **over):
    body = {'enabled': True, 'days': [1, 2, 3, 4, 5], 'time': '09:00', 'timezone': 'Europe/Moscow',
            'overwrite': True}
    body.update(over)
    return admin.put(f'/api/jira/buttons/{bid}/schedule', json=body)


def run_due(flask_app, now):
    with flask_app.app_context():
        return app_module.run_due_schedules(now)


def entry(flask_app, sid, date):
    with flask_app.app_context():
        return app_module.get_db().execute('SELECT * FROM entries WHERE service_id = ? AND date = ?',
                                           (sid, date)).fetchone()


# ─── Сопоставление полей ─────────────────────────────────────────

def test_default_field_map_and_fields_list(admin, button):
    assert button['fieldMap']['remaining'] == {'field': 'timeestimate', 'unit': 'seconds'}
    assert button['lastSuccessAt'] is None and button['schedule']['enabled'] is False
    fields = admin.get('/api/projects/1/jira/fields').get_json()
    ids = [f['id'] for f in fields]
    assert set(ids[:3]) == {'timeestimate', 'timespent', 'timeoriginalestimate'}  # поля учёта времени — первыми
    assert 'customfield_10050' in ids and 'customfield_10060' not in ids and 'summary' not in ids


def test_custom_field_map_in_hours(admin, fake, button):
    field_map = {'remaining': {'field': 'customfield_10050', 'unit': 'hours'},
                 'spent': {'field': '', 'unit': 'seconds'},
                 'estimate': {'field': 'timeoriginalestimate', 'unit': 'seconds'}}
    r = admin.put(f"/api/jira/buttons/{button['id']}", json={
        'name': 'Срез', 'structureId': None, 'fieldMap': field_map,
        'mappings': [{'serviceId': 1, 'issueKeys': 'ABC-1', 'includeSubtree': True}]})
    assert r.status_code == 200 and r.get_json()['fieldMap'] == field_map
    item = snapshot(admin, button['id'])[1]
    assert (item['remaining'], item['spent'], item['estimate']) == (25.5, None, 18)


@pytest.mark.parametrize('field_map, message', [
    ({'remaining': {'field': 'bad id!'}}, 'идентификатор'),
    ({'remaining': {'field': 'timeestimate', 'unit': 'days'}}, 'Единица'),
    ({m: {'field': ''} for m in ('remaining', 'spent', 'estimate')}, 'хотя бы одну'),
], ids=['bad-id', 'bad-unit', 'all-empty'])
def test_field_map_validation(admin, button, field_map, message):
    r = create_button(admin, fieldMap=field_map)
    assert r.status_code == 400 and message in r.get_json()['error']


def test_text_field_gives_warning(admin, button):
    r = admin.put(f"/api/jira/buttons/{button['id']}", json={
        'name': 'Срез', 'structureId': None,
        'fieldMap': {'remaining': {'field': 'customfield_10060', 'unit': 'hours'}},
        'mappings': [{'serviceId': 1, 'issueKeys': 'ABC-1', 'includeSubtree': False}]})
    assert r.status_code == 200
    item = snapshot(admin, button['id'])[1]
    assert item['remaining'] is None and any('не числовое' in w for w in item['warnings'])


# ─── Расписание ──────────────────────────────────────────────────

def test_schedule_locked_until_successful_snapshot(admin, button):
    bid = button['id']
    r = schedule(admin, bid)
    assert r.status_code == 400 and 'первого успешного среза' in r.get_json()['error']
    # выключенное расписание можно сохранить заранее
    assert schedule(admin, bid, enabled=False).status_code == 200
    assert admin.post(f'/api/jira/buttons/{bid}/schedule/run').status_code == 400
    snapshot(admin, bid)
    data = admin.get('/api/projects/1/jira').get_json()
    saved = next(b for b in data['buttons'] if b['id'] == bid)
    assert saved['lastSuccessAt']
    r = schedule(admin, bid, days=[3, 1, 1], time='07:45', timezone='Asia/Novosibirsk', overwrite=False)
    assert r.status_code == 200
    s = r.get_json()['schedule']
    assert s['enabled'] and s['days'] == [1, 3] and s['time'] == '07:45' and s['overwrite'] is False
    assert s['nextRunAt'] and s['nextRunAt'].endswith('T07:45')


@pytest.mark.parametrize('over, message', [
    ({'days': []}, 'день недели'),
    ({'days': [0]}, 'Дни недели'),
    ({'time': '25:00'}, 'ЧЧ:ММ'),
    ({'timezone': 'Mars/Base'}, 'часовой пояс'),
], ids=['no-days', 'bad-day', 'bad-time', 'bad-tz'])
def test_schedule_validation(admin, button, over, message):
    snapshot(admin, button['id'])
    r = schedule(admin, button['id'], **over)
    assert r.status_code == 400 and message in r.get_json()['error']


def test_schedule_requires_edit_rights(admin, make_user, button):
    viewer = make_user('viewer1', project_id=1)
    assert viewer.put(f"/api/jira/buttons/{button['id']}/schedule", json={'enabled': False}).status_code == 403


def test_scheduler_runs_once_per_day(admin, flask_app, button):
    bid = button['id']
    snapshot(admin, bid)
    assert schedule(admin, bid).status_code == 200
    # до времени запуска и в выходной — ничего
    assert run_due(flask_app, datetime(2026, 11, 2, 5, 59, tzinfo=timezone.utc)) == 0
    assert run_due(flask_app, datetime(2026, 11, 1, 7, 0, tzinfo=timezone.utc)) == 0
    assert run_due(flask_app, MONDAY_0930_MSK) == 1
    e = entry(flask_app, 1, MONDAY)
    assert (e['remaining'], e['spent'], e['estimate']) == (12.5, 6, 16)
    # повторная проверка (другой воркер, следующий тик) в тот же день не запускает срез снова
    assert run_due(flask_app, MONDAY_0930_MSK) == 0
    assert run_due(flask_app, datetime(2026, 11, 2, 20, 0, tzinfo=timezone.utc)) == 0
    # на следующий день — снова
    assert run_due(flask_app, datetime(2026, 11, 3, 6, 1, tzinfo=timezone.utc)) == 1
    s = next(b for b in admin.get('/api/projects/1/jira').get_json()['buttons'] if b['id'] == bid)['schedule']
    assert s['lastStatus'] == 'ok' and 'Сохранено замеров за 2026-11-03: 1' in s['lastMessage']
    audit = admin.get('/api/audit?username=Автозаполнение').get_json()
    rows = audit['entries'] if isinstance(audit, dict) else audit
    assert any(r.get('username') == 'Автозаполнение' and r.get('action') == 'autofill' for r in rows)


def test_scheduler_uses_schedule_timezone(admin, flask_app, button):
    bid = button['id']
    snapshot(admin, bid)
    schedule(admin, bid, timezone='Asia/Vladivostok', time='09:00')
    # 23:30 UTC воскресенья = 09:30 понедельника во Владивостоке
    assert run_due(flask_app, datetime(2026, 11, 1, 23, 30, tzinfo=timezone.utc)) == 1
    assert entry(flask_app, 1, MONDAY) is not None


def test_scheduler_without_overwrite_keeps_manual_entry(admin, flask_app, button):
    bid = button['id']
    snapshot(admin, bid)
    schedule(admin, bid, overwrite=False)
    admin.post('/api/projects/1/measurements', json={'date': MONDAY, 'items': [
        {'serviceId': 1, 'remaining': 99, 'spent': 1, 'estimate': 100}]})
    assert run_due(flask_app, MONDAY_0930_MSK) == 1
    assert entry(flask_app, 1, MONDAY)['remaining'] == 99
    s = next(b for b in admin.get('/api/projects/1/jira').get_json()['buttons'] if b['id'] == bid)['schedule']
    assert 'пропущено' in s['lastMessage']


def test_unmapped_metric_keeps_existing_value(admin, flask_app, button):
    bid = button['id']
    admin.put(f'/api/jira/buttons/{bid}', json={
        'name': 'Срез', 'structureId': 7, 'fieldMap': {'remaining': {'field': 'timeestimate', 'unit': 'seconds'},
                                                       'spent': {'field': ''}, 'estimate': {'field': ''}},
        'mappings': [{'serviceId': 1, 'issueKeys': 'ABC-1', 'includeSubtree': True}]})
    snapshot(admin, bid)
    schedule(admin, bid)
    admin.post('/api/projects/1/measurements', json={'date': MONDAY, 'items': [
        {'serviceId': 1, 'remaining': 99, 'spent': 3, 'estimate': 100}]})
    run_due(flask_app, MONDAY_0930_MSK)
    e = entry(flask_app, 1, MONDAY)
    assert (e['remaining'], e['spent'], e['estimate']) == (12.5, 3, 100)


def test_scheduler_records_jira_error(admin, flask_app, fake, button):
    bid = button['id']
    snapshot(admin, bid)
    schedule(admin, bid)
    fake.configure(token='rotated')
    assert run_due(flask_app, MONDAY_0930_MSK) == 1
    s = next(b for b in admin.get('/api/projects/1/jira').get_json()['buttons'] if b['id'] == bid)['schedule']
    assert s['lastStatus'] == 'error' and s['lastMessage']
    assert entry(flask_app, 1, MONDAY) is None


def test_disabled_schedule_does_not_run(admin, flask_app, button):
    snapshot(admin, button['id'])
    schedule(admin, button['id'])
    schedule(admin, button['id'], enabled=False)
    assert run_due(flask_app, MONDAY_0930_MSK) == 0


def test_run_now_saves_today(admin, flask_app, button):
    bid = button['id']
    snapshot(admin, bid)
    r = admin.post(f'/api/jira/buttons/{bid}/schedule/run')
    assert r.status_code == 200 and r.get_json()['status'] == 'ok'
    today = datetime.now(timezone.utc).astimezone(app_module.ZoneInfo('Europe/Moscow')).date().isoformat()
    assert entry(flask_app, 1, today)['remaining'] == 12.5


def test_schedule_deleted_with_button(admin, flask_app, button):
    snapshot(admin, button['id'])
    schedule(admin, button['id'])
    assert admin.delete(f"/api/jira/buttons/{button['id']}").status_code == 200
    with flask_app.app_context():
        assert app_module.get_db().execute('SELECT COUNT(*) FROM jira_schedules').fetchone()[0] == 0
