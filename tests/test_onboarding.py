"""Эпики из Jira (список, целевые даты, «Сдано») и мастер подключения проекта к Jira."""

import pytest

import app as app_module
import fake_jira
import jira_integration as jira
from fake_jira import FakeJira
from test_jira import connect, create_button


@pytest.fixture
def fake(monkeypatch):
    server = FakeJira()
    monkeypatch.setitem(fake_jira.ISSUES[101], 'duedate', '2026-12-15')
    monkeypatch.setitem(fake_jira.ISSUES[104], 'fixVersions', [{'name': '4.0', 'releaseDate': '2027-01-20'},
                                                                {'name': '3.9', 'releaseDate': '2026-11-30'}, {'name': 'б/д'}])
    yield server
    server.stop()


def client_for(fake):
    return jira.JiraClient(fake.url, 'server', '', 'secret-pat')


def services(admin, pid):
    return {s['name']: s for s in admin.get(f'/api/projects/{pid}/data').get_json()['services']}


# ─── Кандидаты в эпики ───────────────────────────────────────────

def test_epic_candidates_from_structure_levels(fake):
    top = jira.epic_candidates(client_for(fake), 7, 0, target='duedate')
    assert [(e['key'], e['target'], e['done'], e['stage']) for e in top] == [('ABC-1', '2026-12-15', False, None),
                                                                            ('ABC-4', None, False, None)]
    by_version = jira.epic_candidates(client_for(fake), 7, 0, target='fixVersion')
    assert [e['target'] for e in by_version] == [None, '2027-01-20']  # самая поздняя дата выпуска версий
    nested = jira.epic_candidates(client_for(fake), 7, 1, target='none')
    # второй уровень: ABC-2 (видимая), этап — тема задачи верхнего уровня; ABC-9 скрыта от учётной записи; папки пропущены
    assert [(e['key'], e['stage'], e['done'], e['target']) for e in nested] == [('ABC-2', 'Задача ABC-1', True, None)]


def test_epic_candidates_by_jql_and_errors(fake, monkeypatch):
    found = jira.epic_candidates(client_for(fake), None, 0, jql='key in (ABC-1, ABC-5)', target='duedate')
    assert {e['key'] for e in found} == {'ABC-1', 'ABC-5'}
    with pytest.raises(jira.JiraError, match='JQL'):
        jira.epic_candidates(client_for(fake), None, 0, jql='  ')
    with pytest.raises(jira.JiraError, match='Источник'):
        jira.epic_candidates(client_for(fake), 7, 0, target='magic')
    monkeypatch.setattr(jira, 'EPIC_LIMIT', 1)
    with pytest.raises(jira.JiraError, match='другой уровень'):
        jira.epic_candidates(client_for(fake), 7, 0)
    with pytest.raises(jira.JiraError, match='уточните JQL'):
        jira.epic_candidates(client_for(fake), None, 0, jql='key in (ABC-1, ABC-4, ABC-5)')


def test_epic_target_parsing():
    assert jira.epic_target({'duedate': '2026-12-01T00:00:00'}, 'duedate') == '2026-12-01'
    assert jira.epic_target({'duedate': 'скоро'}, 'duedate') is None
    assert jira.epic_target({'fixVersions': [{'releaseDate': 5}, 'x']}, 'fixVersion') is None
    assert jira.epic_target({'duedate': '2026-12-01'}, 'none') is None


# ─── Мастер подключения ──────────────────────────────────────────

def onboard(admin, pid=2, **over):
    body = {'name': 'Релиз', 'structureId': 7, 'structureName': 'Релиз 2026',
            'epics': {'level': 0, 'target': 'duedate', 'done': True, 'enabled': True, 'stage': 'Этап 1'},
            'historyWeeks': 2, 'schedule': {'enabled': True, 'days': [1, 2, 3, 4, 5], 'time': '09:00',
                                            'timezone': 'Europe/Moscow'}}
    body.update(over)
    return admin.post(f'/api/projects/{pid}/jira/onboard', json=body)


def test_onboarding_creates_everything_in_one_step(admin, flask_app, fake, monkeypatch):
    monkeypatch.setitem(flask_app.config, 'HISTORY_SYNC', True)
    connect(admin, fake)
    preview = admin.post('/api/projects/2/jira/epics/preview', json={'structureId': 7, 'level': 0}).get_json()['epics']
    assert [e['key'] for e in preview] == ['ABC-1', 'ABC-4'] and all(e['serviceId'] is None for e in preview)
    r = onboard(admin)
    data = r.get_json()
    assert r.status_code == 200, data
    assert (data['created'], data['linked'], data['snapshot']['status']) == (2, 0, 'ok')
    assert data['history']['status'] == 'done' and data['button']['schedule']['enabled']
    assert data['button']['epicSync']['enabled'] and data['button']['epicSync']['lastMessage'].startswith('Эпики из Jira')
    epics = services(admin, 2)
    assert set(epics) == {'Задача ABC-1', 'Задача ABC-4'}
    assert epics['Задача ABC-1']['targetDate'] == '2026-12-15' and epics['Задача ABC-1']['etap'] == 'Этап 1'
    entries = admin.get('/api/projects/2/data').get_json()['entries']
    assert {e['service_id'] for e in entries} == {epics['Задача ABC-1']['id'], epics['Задача ABC-4']['id']}
    assert len({e['date'] for e in entries}) >= 3  # сегодня и восстановленные недели
    audit = admin.get('/api/audit', query_string={'page': 'jira'}).get_json()['entries']
    assert {'sync', 'create', 'update'} <= {a['action'] for a in audit}
    # повторный мастер по той же структуре: эпики уже есть — связываются, а не дублируются
    again = onboard(admin, name='Второй срез', historyWeeks=0, schedule={}).get_json()
    assert (again['created'], again['linked']) == (0, 2) and len(services(admin, 2)) == 2


def test_onboarding_selected_keys_and_existing_epics(admin, flask_app, fake):
    connect(admin, fake)
    create_button(admin, mappings=[{'serviceId': 1, 'issueKeys': 'ABC-4', 'includeSubtree': True}])
    before = len(services(admin, 1))
    data = onboard(admin, pid=1, keys=['abc-4'], historyWeeks=0, schedule={}).get_json()
    assert (data['created'], data['linked']) == (0, 1)  # ABC-4 уже связана с эпиком проекта
    assert [m['issueKeys'] for m in data['button']['mappings']] == ['ABC-4'] and len(services(admin, 1)) == before


@pytest.mark.parametrize('over, message', [
    ({'structureId': None, 'epics': {'level': 0}}, 'JQL'),
    ({'keys': 'ABC-1'}, 'ключи'),
    ({'keys': []}, 'ни одного эпика'),
    ({'historyWeeks': 60}, 'недель'),
    ({'schedule': {'enabled': True, 'days': [], 'time': '09:00'}}, 'дни недели'),
    ({'schedule': {'enabled': True, 'days': [1], 'time': '9 утра'}}, 'ЧЧ:ММ'),
    ({'epics': {'level': 2}}, 'Уровень'),
    ({'epics': {'target': 'вчера'}}, 'Целевая дата'),
    ({'epics': {'done': 'да'}}, 'да или нет'),
    ({'epics': {'jql': 5}}, 'текст'),
    ({'fieldMap': {'remaining': {'field': 'x y', 'unit': 'seconds'}}}, ''),
])
def test_onboarding_validation(admin, fake, over, message):
    connect(admin, fake)
    r = onboard(admin, **over)
    assert r.status_code == 400 and message in r.get_json()['error']
    assert not admin.get('/api/projects/2/jira').get_json()['buttons']  # ничего не создано


def test_onboarding_errors_and_rights(admin, make_user, fake, monkeypatch):
    assert onboard(admin).status_code == 502  # подключения к Jira нет
    connect(admin, fake)
    viewer = make_user('viewer', project_id=2)
    assert onboard(viewer).status_code == 403
    assert viewer.post('/api/projects/2/jira/epics/preview', json={'structureId': 7}).status_code == 403
    assert onboard(admin, pid=999).status_code in (403, 404)
    r = admin.post('/api/projects/2/jira/epics/preview', json={'level': 0})
    assert r.status_code == 502 and 'JQL' in r.get_json()['error']
    # срез не дал значений — кнопка и эпики созданы, ошибка среза показана, история и расписание не запускаются
    monkeypatch.setattr(app_module, 'run_autofill', lambda *a, **k: ('error', 'Jira не вернула значений ни по одному эпику'))
    data = onboard(admin).get_json()
    assert data['snapshot']['status'] == 'error' and data['history'] is None and not data['button']['schedule']['enabled']
    monkeypatch.setattr(app_module, 'run_autofill', lambda *a, **k: 1 / 0)
    assert onboard(admin, name='Ещё').get_json()['snapshot']['message'].startswith('Внутренняя ошибка')


# ─── Синхронизация эпиков существующей кнопки ────────────────────

def make_synced_button(admin, fake, **config):
    connect(admin, fake)
    data = onboard(admin, historyWeeks=0, schedule={}).get_json()
    bid = data['button']['id']
    if config:
        assert admin.put(f'/api/jira/buttons/{bid}/epic-sync', json=dict({'level': 0, 'target': 'duedate'}, **config)).status_code == 200
    return bid


def test_sync_updates_targets_done_and_names(admin, fake, monkeypatch):
    bid = make_synced_button(admin, fake, names=True)
    monkeypatch.setitem(fake_jira.ISSUES[101], 'duedate', '2026-12-31')
    monkeypatch.setitem(fake_jira.ISSUES[101], 'summary', 'Новая тема')
    monkeypatch.setitem(fake_jira.ISSUES[104], 'category', 'done')
    dry = admin.post(f'/api/jira/buttons/{bid}/epic-sync/run', json={'dryRun': True}).get_json()['plan']
    changes = {u['key']: {c['field']: c['new'] for c in u['changes']} for u in dry['update']}
    assert changes == {'ABC-1': {'targetDate': '2026-12-31', 'name': 'Новая тема'}, 'ABC-4': {'isDone': True}}
    assert services(admin, 2)['Задача ABC-1']['targetDate'] == '2026-12-15'  # предпросмотр ничего не меняет
    r = admin.post(f'/api/jira/buttons/{bid}/epic-sync/run', json={}).get_json()
    assert r['message'] == 'Эпики из Jira — новых эпиков: 0, обновлено: 2'
    epics = services(admin, 2)
    assert epics['Новая тема']['targetDate'] == '2026-12-31' and epics['Задача ABC-4']['isDone']
    # переоткрыли в Jira — «Сдано» снимается; пустой срок в Jira не стирает цель
    monkeypatch.setitem(fake_jira.ISSUES[104], 'category', 'indeterminate')
    monkeypatch.setitem(fake_jira.ISSUES[101], 'duedate', None)
    admin.post(f'/api/jira/buttons/{bid}/epic-sync/run', json={})
    epics = services(admin, 2)
    assert not epics['Задача ABC-4']['isDone'] and epics['Новая тема']['targetDate'] == '2026-12-31'


def test_sync_adds_new_epics_and_reports_missing(admin, fake, monkeypatch):
    bid = make_synced_button(admin, fake)
    monkeypatch.setattr(fake_jira, 'FORMULA', '1:0:101,2:1:102,7:0:106')  # ABC-4 убрали, ABC-5 добавили
    plan = admin.post(f'/api/jira/buttons/{bid}/epic-sync/run', json={}).get_json()['plan']
    assert [c['key'] for c in plan['create']] == ['ABC-5'] and [m['name'] for m in plan['missing']] == ['Задача ABC-4']
    button = admin.get('/api/projects/2/jira').get_json()['buttons'][0]
    assert sorted(m['issueKeys'] for m in button['mappings']) == ['ABC-1', 'ABC-4', 'ABC-5']
    assert 'нет в Jira: 1' in button['epicSync']['lastMessage']


def test_sync_level_one_sets_stage_and_links_to_button(admin, fake):
    connect(admin, fake)
    sid = admin.post('/api/projects/2/services', json={'name': 'Интеграция', 'etap': 'Старый этап'}).get_json()['id']
    bid = create_button(admin, pid=2, mappings=[{'serviceId': sid, 'issueKeys': 'ABC-1', 'includeSubtree': True}]).get_json()['id']
    other = create_button(admin, pid=2, name='Второй', mappings=[{'serviceId': sid, 'issueKeys': 'ABC-2', 'includeSubtree': False}])
    assert admin.put(f'/api/jira/buttons/{bid}/epic-sync', json={'level': 1, 'target': 'none', 'done': False}).status_code == 200
    plan = admin.post(f'/api/jira/buttons/{bid}/epic-sync/run', json={'dryRun': True}).get_json()['plan']
    # ABC-2 связана с эпиком через другую кнопку — эпик не дублируется, а связывается и с этой
    assert plan['create'] == [] and [l['key'] for l in plan['link']] == ['ABC-2']
    assert plan['update'][0]['changes'] == [{'field': 'etap', 'old': 'Старый этап', 'new': 'Задача ABC-1'}]
    admin.post(f'/api/jira/buttons/{bid}/epic-sync/run', json={})
    button = next(b for b in admin.get('/api/projects/2/jira').get_json()['buttons'] if b['id'] == bid)
    assert button['mappings'] == [{'serviceId': sid, 'issueKeys': 'ABC-1, ABC-2', 'includeSubtree': True}]
    assert services(admin, 2)['Интеграция']['etap'] == 'Задача ABC-1'
    assert other.status_code == 200


def test_sync_config_validation_and_rights(admin, make_user, fake):
    connect(admin, fake)
    bid = create_button(admin, structureId=None, mappings=[{'serviceId': 1, 'issueKeys': 'ABC-1', 'includeSubtree': False}]).get_json()['id']
    r = admin.put(f'/api/jira/buttons/{bid}/epic-sync', json={'level': 0})
    assert r.status_code == 400 and 'JQL' in r.get_json()['error']
    ok = admin.put(f'/api/jira/buttons/{bid}/epic-sync', json={'jql': 'key = ABC-1', 'target': 'none'})
    assert ok.status_code == 200 and ok.get_json()['epicSync']['jql'] == 'key = ABC-1'
    viewer = make_user('viewer', project_id=1)
    assert viewer.put(f'/api/jira/buttons/{bid}/epic-sync', json={}).status_code == 403
    assert viewer.post(f'/api/jira/buttons/{bid}/epic-sync/run', json={}).status_code == 403
    assert admin.post('/api/jira/buttons/999/epic-sync/run', json={}).status_code == 404
    fake.configure(token='other')
    assert admin.post(f'/api/jira/buttons/{bid}/epic-sync/run', json={'dryRun': True}).status_code == 502


def test_broken_sync_config_falls_back_to_defaults(flask_app):
    row = {'epic_sync': '{"level": 7, "target": "x", "enabled": "да"}'}
    assert app_module.epic_sync_config(row) == app_module.EPIC_SYNC_DEFAULT
    assert app_module.epic_sync_config({'epic_sync': 'не json'}) == app_module.EPIC_SYNC_DEFAULT


# ─── Перед автозаполнением ───────────────────────────────────────

def due_now(flask_app, bid):
    with flask_app.app_context():
        db = app_module.get_db()
        db.execute('INSERT OR IGNORE INTO jira_schedules (button_id) VALUES (?)', (bid,))
        db.execute("UPDATE jira_schedules SET enabled = 1, days = '1,2,3,4,5,6,7', run_time = '00:00', timezone = 'UTC', "
                   'last_run_date = NULL WHERE button_id = ?', (bid,))
        db.commit()
        assert app_module.run_due_schedules() == 1
        return db.execute('SELECT * FROM jira_schedules WHERE button_id = ?', (bid,)).fetchone()


def test_autofill_syncs_epics_first(admin, flask_app, fake, monkeypatch):
    bid = make_synced_button(admin, fake, enabled=True)
    monkeypatch.setattr(fake_jira, 'FORMULA', fake_jira.FORMULA + ',7:0:106')
    sched = due_now(flask_app, bid)
    assert sched['last_status'] == 'ok'
    assert sched['last_message'].startswith('Эпики из Jira — новых эпиков: 1') and 'Сохранено замеров' in sched['last_message']
    assert 'Задача ABC-5' in services(admin, 2)
    sched = due_now(flask_app, bid)  # без изменений в Jira — итог автозаполнения без примечания
    assert sched['last_message'].startswith('Сохранено замеров')


def test_autofill_runs_even_if_sync_fails(admin, flask_app, fake, monkeypatch):
    bid = make_synced_button(admin, fake, enabled=True)

    def broken(*a, **k):
        raise jira.JiraError('Структура недоступна')
    monkeypatch.setattr(jira, 'epic_candidates', broken)
    sched = due_now(flask_app, bid)
    assert sched['last_status'] == 'ok' and sched['last_message'].startswith('Эпики из Jira не синхронизированы: Структура')
    monkeypatch.setattr(jira, 'epic_candidates', lambda *a, **k: 1 / 0)
    assert 'внутренняя ошибка' in due_now(flask_app, bid)['last_message']
