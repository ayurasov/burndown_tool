"""Восстановление замеров и состава задач на прошлые даты по журналу изменений Jira."""

import time

import pytest

import app as app_module
import jira_integration as jira
from fake_jira import FakeJira
from test_jira import connect, create_button


@pytest.fixture
def fake():
    server = FakeJira()
    yield server
    server.stop()


@pytest.fixture(autouse=True)
def sync_jobs(flask_app, monkeypatch):
    monkeypatch.setitem(flask_app.config, 'HISTORY_SYNC', True)


def make_button(admin, fake, conn=None, **over):
    connect(admin, fake, **(conn or {}))
    bid = create_button(admin, **over).get_json()['id']
    assert admin.post(f'/api/jira/buttons/{bid}/snapshot', json={'date': '2026-10-28'}).status_code == 200
    return bid


def restore(admin, bid, **over):
    body = {'from': '2026-10-19', 'to': '2026-10-26', 'step': 'week', 'timezone': 'Europe/Moscow', 'keepExisting': True}
    body.update(over)
    return admin.post(f'/api/jira/buttons/{bid}/history', json=body)


def entry(flask_app, sid, date):
    with flask_app.app_context():
        e = app_module.get_db().execute('SELECT * FROM entries WHERE service_id = ? AND date = ?', (sid, date)).fetchone()
        return (e['remaining'], e['spent'], e['estimate']) if e else None


# ─── Чистая логика ───────────────────────────────────────────────

def test_issue_state_at_rolls_back_changelog():
    fields = {'created': '2026-10-01T10:00:00.000+0300', 'status': {'id': '3'}, 'timeestimate': 1800,
              'customfield_1': 5}
    histories = [
        {'created': '2026-10-20T12:00:00.000+0300', 'items': [
            {'field': 'status', 'from': '1', 'to': '3'},
            {'field': 'timeestimate', 'from': '7200', 'to': '1800'}]},
        {'created': '2026-10-24T12:00:00.000+0300', 'items': [
            {'fieldId': 'customfield_1', 'field': 'Трудозатраты', 'from': None, 'fromString': '2,5'.replace(',', '.'),
             'to': None, 'toString': '5'}]},
    ]
    ids = ['timeestimate', 'customfield_1']
    at = lambda s: jira.parse_jira_time(s)
    values, status, seen = jira.issue_state_at(fields, histories, ids, at('2026-10-19T23:59:59.000+0300'))
    assert (values, status) == ({'timeestimate': 7200.0, 'customfield_1': 2.5}, '1')
    assert {'timeestimate', 'customfield_1', 'status'} <= seen
    values, status, _ = jira.issue_state_at(fields, histories, ids, at('2026-10-22T23:59:59.000+0300'))
    assert (values, status) == ({'timeestimate': 1800, 'customfield_1': 2.5}, '3')
    assert jira.issue_state_at(fields, histories, ids, at('2026-09-30T23:59:59.000+0300')) is None
    assert jira.parse_jira_time('мусор') is None


def test_history_dates():
    assert app_module.history_dates('2026-10-01', '2026-10-20', 7) == ['2026-10-01', '2026-10-08', '2026-10-15', '2026-10-20']
    assert app_module.history_dates('2026-10-01', '2026-10-03', 1) == ['2026-10-01', '2026-10-02', '2026-10-03']
    assert app_module.history_dates('2026-10-01', '2026-10-01', 7) == ['2026-10-01']


# ─── Восстановление ──────────────────────────────────────────────

def test_restore_structure_button(admin, flask_app, fake):
    bid = make_button(admin, fake)
    r = restore(admin, bid)
    assert r.status_code == 202, r.get_json()
    job = admin.get(f"/api/jira/history-jobs/{r.get_json()['id']}").get_json()
    assert job['status'] == 'done' and job['message'].startswith('Восстановлено замеров: 2 на 2 дат(ы). ')
    assert 'недоступны для просмотра' in job['message']  # предупреждения текущего среза (скрытая ABC-9)
    assert job['total'] == 3 and job['progress'] == 3 and job['createdBy'] == 'admin'
    # 19.10: ABC-3 ещё не в работе и с остатком 2 ч, ABC-2 не закрыта и без затрат
    assert entry(flask_app, 1, '2026-10-19') == (14, 5, 16)
    assert entry(flask_app, 1, '2026-10-26') == (12.5, 6, 16)
    issues = {i['key']: i for i in admin.get('/api/services/1/jira/issues?date=2026-10-19').get_json()['issues']}
    assert (issues['ABC-2']['category'], issues['ABC-2']['status']) == ('indeterminate', 'В работе')
    assert (issues['ABC-3']['category'], issues['ABC-3']['remaining']) == ('new', 2.0)
    history = admin.get('/api/projects/1/jira/composition').get_json()['history']['1']
    assert [p['date'] for p in history] == ['2026-10-19', '2026-10-26', '2026-10-28']
    # последняя задача — в кнопке, аудит от имени запустившего
    button = next(b for b in admin.get('/api/projects/1/jira').get_json()['buttons'] if b['id'] == bid)
    assert button['history']['status'] == 'done'
    audit = admin.get('/api/audit?username=admin').get_json()['entries']
    assert any(a['action'] == 'restore' and 'Восстановлено замеров: 2' in (a['newValue'] or '') for a in audit)


def test_restore_subtasks_respects_creation_date(admin, flask_app, fake):
    bid = make_button(admin, fake, structureId=None,
                      mappings=[{'serviceId': 2, 'issueKeys': 'ABC-1', 'includeSubtree': True}])
    assert restore(admin, bid).status_code == 202
    assert entry(flask_app, 2, '2026-10-19') == (10, 5, 12)     # ABC-6 создана 22.10
    assert entry(flask_app, 2, '2026-10-26') == (14, 7, 18)


def test_restore_cloud_reads_full_changelog(admin, flask_app, fake):
    fake.configure(mode='cloud', login='me@corp.ru', token='api-token')
    bid = make_button(admin, fake, conn={'instanceType': 'cloud', 'login': 'me@corp.ru', 'token': 'api-token'},
                      structureId=None, mappings=[{'serviceId': 1, 'issueKeys': 'ABC-2, ABC-3', 'includeSubtree': False}])
    assert restore(admin, bid).status_code == 202
    assert entry(flask_app, 1, '2026-10-19') == (4, None, 4)  # затрат ещё не было
    assert ('GET', '/rest/api/3/issue/ABC-3/changelog') in fake.state['requests']
    assert any(b.get('expand') == 'changelog' for b in fake.state['search_bodies'])


def test_restore_keeps_manual_measurements(admin, flask_app, fake):
    bid = make_button(admin, fake)
    admin.post('/api/projects/1/measurements', json={'date': '2026-10-19', 'items': [
        {'serviceId': 1, 'remaining': 99, 'spent': 1, 'estimate': 100}]})
    job = restore(admin, bid).get_json()
    assert 'пропущено (замер уже был): 1' in job['message']
    assert entry(flask_app, 1, '2026-10-19') == (99, 1, 100)
    restore(admin, bid, keepExisting=False)
    assert entry(flask_app, 1, '2026-10-19') == (14, 5, 16)


def test_restore_does_not_replace_real_composition(admin, fake):
    bid = make_button(admin, fake)
    admin.post(f'/api/jira/buttons/{bid}/snapshot', json={'date': '2026-10-26'})  # «настоящий» срез
    restore(admin, bid)
    issues = {i['key']: i for i in admin.get('/api/services/1/jira/issues?date=2026-10-26').get_json()['issues']}
    assert issues['ABC-2']['category'] == 'done'  # совпадает и так, но запись не пересоздавалась
    assert len(issues) == 3


@pytest.mark.parametrize('over, message', [
    ({'from': '2026-10-27', 'to': '2026-10-26'}, 'не позже'),
    ({'to': '2099-01-01'}, 'прошедшие'),
    ({'from': '2024-01-01', 'step': 'day'}, 'Слишком много дат'),
    ({'step': 'month'}, 'день или неделя'),
    ({'timezone': 'Mars/Base'}, 'часовой пояс'),
    ({'from': ''}, 'Дата «с»'),
], ids=['order', 'future', 'too-many', 'step', 'tz', 'empty'])
def test_restore_validation(admin, fake, over, message):
    bid = make_button(admin, fake)
    r = restore(admin, bid, **over)
    assert r.status_code == 400 and message in r.get_json()['error']


def test_restore_requires_successful_snapshot_and_rights(admin, make_user, fake):
    connect(admin, fake)
    bid = create_button(admin).get_json()['id']
    r = restore(admin, bid)
    assert r.status_code == 400 and 'срез' in r.get_json()['error']
    viewer = make_user('viewer1', project_id=1)
    assert restore(viewer, bid).status_code == 403


def test_restore_jira_error_is_reported(admin, fake):
    bid = make_button(admin, fake)
    fake.configure(token='rotated')
    job = restore(admin, bid).get_json()
    assert job['status'] == 'error' and '401' in job['message']


def test_running_job_blocks_second_start(admin, flask_app, fake):
    bid = make_button(admin, fake)
    with flask_app.app_context():
        db = app_module.get_db()
        db.execute("INSERT INTO jira_history_jobs (button_id, status, params, created_at, updated_at) "
                   "VALUES (?, 'running', '{}', ?, ?)", (bid, app_module._utc_now_iso(), app_module._utc_now_iso()))
        db.commit()
    r = restore(admin, bid)
    assert r.status_code == 409 and r.get_json()['job']['status'] == 'running'
    with flask_app.app_context():  # зависшая (нет обновлений > 15 мин) задача не мешает
        db = app_module.get_db()
        db.execute("UPDATE jira_history_jobs SET updated_at = '2026-01-01 00:00:00'")
        db.commit()
    assert restore(admin, bid).status_code == 202


def test_history_job_access(admin, make_user, fake):
    bid = make_button(admin, fake)
    jid = restore(admin, bid).get_json()['id']
    viewer = make_user('viewer1', project_id=1)
    assert viewer.get(f'/api/jira/history-jobs/{jid}').status_code == 403
    assert admin.get('/api/jira/history-jobs/9999').status_code == 404


def test_background_thread_runs_job(admin, flask_app, fake, monkeypatch):
    monkeypatch.setitem(flask_app.config, 'HISTORY_SYNC', False)
    bid = make_button(admin, fake)
    jid = restore(admin, bid).get_json()['id']
    for _ in range(100):
        job = admin.get(f'/api/jira/history-jobs/{jid}').get_json()
        if job['status'] != 'running':
            break
        time.sleep(0.1)
    assert job['status'] == 'done', job


def test_custom_field_without_history_warns(admin, fake):
    bid = make_button(admin, fake, fieldMap={'remaining': {'field': 'customfield_10050', 'unit': 'hours'}})
    job = restore(admin, bid).get_json()
    assert job['status'] == 'done' and 'customfield_10050' in job['message']


def test_restore_before_issues_created(admin, fake):
    bid = make_button(admin, fake)
    job = restore(admin, bid, **{'from': '2026-08-03', 'to': '2026-08-10'}).get_json()
    assert job['status'] == 'done' and 'Восстановлено замеров: 0' in job['message']
    assert 'задач кнопки в Jira ещё не было' in job['message']
