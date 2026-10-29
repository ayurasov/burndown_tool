"""Тесты REST API: авторизация, права доступа, CRUD, валидация и реестр изменений."""

import pytest

from conftest import ADMIN, login


def audit(admin, **params):
    return admin.get('/api/audit', query_string=params).get_json()['entries']


# ─── Авторизация ─────────────────────────────────────────────────

def test_login_ok_and_me(client):
    r = login(client, *ADMIN)
    assert r.status_code == 200 and r.get_json()['success']
    me = client.get('/api/auth/me').get_json()['user']
    assert me['username'] == 'admin' and me['role'] == 'admin'


def test_login_wrong_password(client):
    r = login(client, 'admin', 'wrong')
    assert r.status_code == 401
    assert 'error' in r.get_json()


def test_login_empty_fields(client):
    assert login(client, '', '').status_code == 400


def test_api_requires_auth(client):
    assert client.get('/api/projects').status_code == 401
    assert client.get('/api/projects/1/data').status_code == 401


def test_index_redirects_to_login(client):
    r = client.get('/')
    assert r.status_code == 302 and r.headers['Location'].endswith('/login')


def test_logout(admin):
    admin.post('/api/auth/logout')
    assert admin.get('/api/auth/me').status_code == 401


# ─── Проекты и видимость ─────────────────────────────────────────

def test_admin_sees_all_projects(admin):
    projects = admin.get('/api/projects').get_json()
    assert [p['name'] for p in projects] == ['АИСУЗ', 'ИСУП']
    assert all(p['role'] == 'admin' for p in projects)


def test_user_sees_only_assigned_projects(make_user):
    c = make_user('viewer', project_id=1, project_role='user')
    projects = c.get('/api/projects').get_json()
    assert [(p['id'], p['role']) for p in projects] == [(1, 'user')]
    assert c.get('/api/projects/2/data').status_code == 403


def test_project_data_shape(admin):
    data = admin.get('/api/projects/1/data').get_json()
    assert len(data['services']) == 6
    assert len(data['entries']) == 24
    assert data['stageTargets'] == {'Этап 3': '2026-08-24', 'Этап 4': '2026-08-31'}
    assert data['settings'] == {'forecastMethod': 'linear', 'riskBufferDays': 3,
                                'burnrateMethod': 'all', 'burnrateWindowDays': 14}
    svc = data['services'][0]
    assert set(svc) == {'id', 'project_id', 'etap', 'name', 'targetDate', 'sortOrder', 'isDone', 'stageGroup'}


def test_create_update_delete_project(admin):
    r = admin.post('/api/projects', json={'name': '  Новый  ', 'description': 'd'})
    pid = r.get_json()['id']
    assert r.get_json()['name'] == 'Новый'
    # настройки по умолчанию создаются вместе с проектом
    assert admin.get(f'/api/projects/{pid}/data').get_json()['settings']['forecastMethod'] == 'linear'
    assert admin.put(f'/api/projects/{pid}', json={'name': 'Переименован'}).status_code == 200
    assert admin.put(f'/api/projects/{pid}', json={'name': ' '}).status_code == 400
    assert admin.delete(f'/api/projects/{pid}').status_code == 200
    assert admin.delete(f'/api/projects/{pid}').status_code == 404
    actions = [(e['action'], e['entityId']) for e in audit(admin, page='projects')]
    assert ('delete', pid) in actions and ('create', pid) in actions


def test_create_project_requires_name(admin):
    assert admin.post('/api/projects', json={'name': ''}).status_code == 400


def test_project_admin_endpoints_forbidden_for_manager(make_user):
    c = make_user('mgr', project_id=1, project_role='manager')
    assert c.post('/api/projects', json={'name': 'x'}).status_code == 403
    assert c.delete('/api/projects/1').status_code == 403
    assert c.get('/api/users').status_code == 403
    assert c.get('/api/audit').status_code == 403
    assert c.get('/api/projects/1/export').status_code == 403


def test_delete_project_cascades(admin, db_path):
    import sqlite3
    admin.delete('/api/projects/1')
    conn = sqlite3.connect(db_path)
    assert conn.execute('SELECT COUNT(*) FROM services WHERE project_id = 1').fetchone()[0] == 0
    assert conn.execute('SELECT COUNT(*) FROM entries').fetchone()[0] == 0
    conn.close()


# ─── Эпики ───────────────────────────────────────────────────────

def test_service_crud_and_audit(admin):
    r = admin.post('/api/projects/1/services', json={'etap': 'Этап 5', 'name': 'Новый эпик', 'targetDate': '2026-09-01'})
    sid = r.get_json()['id']
    svc = next(s for s in admin.get('/api/projects/1/data').get_json()['services'] if s['id'] == sid)
    assert svc['sortOrder'] == 7 and svc['isDone'] is False and svc['stageGroup'] == ''

    assert admin.put(f'/api/services/{sid}', json={'name': 'Переименован', 'isDone': True, 'stageGroup': 'Группа'}).status_code == 200
    svc = next(s for s in admin.get('/api/projects/1/data').get_json()['services'] if s['id'] == sid)
    assert (svc['name'], svc['etap'], svc['isDone'], svc['stageGroup']) == ('Переименован', 'Этап 5', True, 'Группа')

    assert admin.delete(f'/api/services/{sid}').status_code == 200
    assert admin.delete(f'/api/services/{sid}').status_code == 404
    actions = {e['action'] for e in audit(admin, page='services')}
    assert {'create', 'update', 'delete'} <= actions


def test_service_update_keeps_unsent_fields(admin):
    """Изменение только целевой даты (как с дашборда) не сбрасывает признак «сдано» и группу."""
    admin.put('/api/services/1', json={'isDone': True, 'stageGroup': 'G'})
    admin.put('/api/services/1', json={'etap': 'Этап 4', 'name': 'Сервис "Работа с жалобами"', 'targetDate': '2026-09-15'})
    svc = admin.get('/api/projects/1/data').get_json()['services'][0]
    assert (svc['targetDate'], svc['isDone'], svc['stageGroup']) == ('2026-09-15', True, 'G')


def test_service_update_no_spurious_stage_group_audit(admin):
    """Пустая группа ('' против NULL в БД) не создаёт лишних записей в реестре."""
    admin.put('/api/services/1', json={'name': 'Сервис "Работа с жалобами"', 'stageGroup': ''})
    assert audit(admin, page='services') == []


def test_reorder_services(admin):
    orders = [{'id': sid, 'sortOrder': i} for i, sid in enumerate([6, 5, 4, 3, 2, 1], start=1)]
    assert admin.post('/api/projects/1/services/reorder', json={'orders': orders}).status_code == 200
    ids = [s['id'] for s in admin.get('/api/projects/1/data').get_json()['services']]
    assert ids == [6, 5, 4, 3, 2, 1]


def test_viewer_cannot_modify(make_user):
    c = make_user('viewer', project_id=1, project_role='user')
    assert c.get('/api/projects/1/data').status_code == 200
    assert c.post('/api/projects/1/services', json={'name': 'x'}).status_code == 403
    assert c.put('/api/services/1', json={'name': 'x'}).status_code == 403
    assert c.delete('/api/services/1').status_code == 403
    assert c.post('/api/services/1/entries', json={'date': '2026-08-01'}).status_code == 403
    assert c.put('/api/entries/1', json={'date': '2026-07-17'}).status_code == 403
    assert c.delete('/api/entries/1').status_code == 403
    assert c.put('/api/projects/1/settings', json={'riskBufferDays': 5}).status_code == 403
    assert c.put('/api/projects/1/stage_targets', json={'stageName': 'Этап 4', 'targetDate': '2026-09-01'}).status_code == 403
    assert c.post('/api/projects/1/services/reorder', json={'orders': []}).status_code == 403


def test_manager_can_modify_only_assigned_project(make_user):
    c = make_user('mgr', project_id=1, project_role='manager')
    assert c.post('/api/projects/1/services', json={'name': 'ok'}).status_code == 200
    assert c.post('/api/projects/2/services', json={'name': 'no'}).status_code == 403


# ─── Замеры ──────────────────────────────────────────────────────

def test_entry_crud_and_audit(admin):
    r = admin.post('/api/services/1/entries', json={'date': '2026-07-30', 'remaining': None, 'spent': None, 'estimate': None})
    eid = r.get_json()['id']
    assert admin.put(f'/api/entries/{eid}', json={'date': '2026-07-30', 'remaining': 300.5, 'spent': 260, 'estimate': 401}).status_code == 200
    entry = next(e for e in admin.get('/api/projects/1/data').get_json()['entries'] if e['id'] == eid)
    assert (entry['remaining'], entry['spent'], entry['estimate']) == (300.5, 260, 401)
    assert admin.delete(f'/api/entries/{eid}').status_code == 200
    log = audit(admin, page='data')
    assert {e['action'] for e in log} == {'create', 'update', 'delete'}
    assert all(e['entityName'] == 'Сервис "Работа с жалобами"' for e in log)


def test_entry_requires_date(admin):
    r = admin.post('/api/services/1/entries', json={'remaining': 1})
    assert r.status_code == 400 and 'error' in r.get_json()


def test_entry_for_missing_service(admin):
    assert admin.post('/api/services/999/entries', json={'date': '2026-07-30'}).status_code == 404
    assert admin.put('/api/entries/999', json={}).status_code == 404


def test_delete_service_cascades_entries(admin):
    admin.delete('/api/services/1')
    entries = admin.get('/api/projects/1/data').get_json()['entries']
    assert all(e['service_id'] != 1 for e in entries) and len(entries) == 20


# ─── Цели этапов и настройки ─────────────────────────────────────

def test_stage_target_upsert(admin):
    assert admin.put('/api/projects/1/stage_targets', json={'stageName': 'Этап 4', 'targetDate': '2026-09-10'}).status_code == 200
    assert admin.put('/api/projects/1/stage_targets', json={'stageName': 'Новая группа', 'targetDate': '2026-10-01'}).status_code == 200
    targets = admin.get('/api/projects/1/data').get_json()['stageTargets']
    assert targets['Этап 4'] == '2026-09-10' and targets['Новая группа'] == '2026-10-01'
    assert admin.put('/api/projects/1/stage_targets', json={'targetDate': '2026-10-01'}).status_code == 400


def test_settings_update_logs_real_changes(admin):
    r = admin.put('/api/projects/1/settings', json={'forecastMethod': 'average', 'riskBufferDays': 3,
                                                    'burnrateMethod': 'all', 'burnrateWindowDays': 14})
    assert r.status_code == 200
    assert admin.get('/api/projects/1/data').get_json()['settings']['forecastMethod'] == 'average'
    log = audit(admin, page='settings')
    assert [(e['fieldName'], e['oldValue'], e['newValue']) for e in log] == [('forecastMethod', 'linear', 'average')]


def test_settings_without_changes_not_logged(admin):
    admin.put('/api/projects/1/settings', json={'riskBufferDays': 3})
    assert audit(admin, page='settings') == []


# ─── Пользователи ────────────────────────────────────────────────

def test_create_user_validation(admin):
    assert admin.post('/api/users', json={'username': 'u1', 'password': ''}).status_code == 400
    assert admin.post('/api/users', json={'username': 'u1', 'password': 'p', 'role': 'superuser'}).status_code == 400
    assert admin.post('/api/users', json={'username': 'u1', 'password': 'p'}).status_code == 200
    dup = admin.post('/api/users', json={'username': 'u1', 'password': 'p'})
    assert dup.status_code == 400 and 'уже существует' in dup.get_json()['error']


def test_update_user(admin, client):
    uid = admin.post('/api/users', json={'username': 'u1', 'password': 'p'}).get_json()['id']
    assert admin.put(f'/api/users/{uid}', json={'displayName': 'Иван "Тест"', 'role': 'manager', 'password': 'new-pass'}).status_code == 200
    user = next(u for u in admin.get('/api/users').get_json() if u['id'] == uid)
    assert (user['displayName'], user['role']) == ('Иван "Тест"', 'manager')
    assert login(client, 'u1', 'new-pass').status_code == 200
    assert admin.put(f'/api/users/{uid}', json={'role': 'root'}).status_code == 400
    assert admin.put(f'/api/users/{uid}', json={'username': ' '}).status_code == 400
    assert admin.put(f'/api/users/{uid}', json={'username': 'admin'}).status_code == 400


def test_admin_cannot_demote_or_delete_self(admin):
    me = admin.get('/api/auth/me').get_json()['user']['id']
    assert admin.put(f'/api/users/{me}', json={'role': 'user'}).status_code == 400
    assert admin.delete(f'/api/users/{me}').status_code == 400


def test_delete_user_and_audit(admin):
    uid = admin.post('/api/users', json={'username': 'gone', 'password': 'p'}).get_json()['id']
    assert admin.delete(f'/api/users/{uid}').status_code == 200
    assert admin.delete(f'/api/users/{uid}').status_code == 404
    assert any(e['action'] == 'delete' and e['oldValue'] == 'gone' for e in audit(admin, page='users'))


def test_project_assignment_roles(admin):
    uid = admin.post('/api/users', json={'username': 'u1', 'password': 'p'}).get_json()['id']
    assert admin.post(f'/api/users/{uid}/projects', json={'projectId': 1, 'role': 'admin'}).status_code == 400
    assert admin.post(f'/api/users/{uid}/projects', json={'projectId': 1, 'role': 'manager'}).status_code == 200
    assert admin.post(f'/api/users/{uid}/projects', json={'projectId': 1, 'role': 'user'}).status_code == 200
    assert admin.get(f'/api/users/{uid}/projects').get_json() == [{'projectId': 1, 'projectName': 'АИСУЗ', 'role': 'user'}]
    assert admin.delete(f'/api/users/{uid}/projects/1').status_code == 200
    assert admin.get(f'/api/users/{uid}/projects').get_json() == []


# ─── Профиль ─────────────────────────────────────────────────────

def test_profile_photo_set_and_delete(make_user):
    c = make_user('u1')
    assert c.put('/api/profile', json={'photo': 'data:image/png;base64,AAAA'}).status_code == 200
    assert c.get('/api/auth/me').get_json()['user']['photo'] == 'data:image/png;base64,AAAA'
    # пустая строка удаляет фото
    assert c.put('/api/profile', json={'photo': ''}).status_code == 200
    assert c.get('/api/auth/me').get_json()['user']['photo'] == ''


def test_profile_password_change_keeps_photo(make_user, client):
    c = make_user('u1')
    c.put('/api/profile', json={'photo': 'data:image/png;base64,AAAA'})
    c.put('/api/profile', json={'password': 'changed'})
    assert c.get('/api/auth/me').get_json()['user']['photo'] == 'data:image/png;base64,AAAA'
    assert login(client, 'u1', 'changed').status_code == 200


# ─── Экспорт и реестр ────────────────────────────────────────────

def test_export_json(admin):
    r = admin.get('/api/projects/1/export')
    assert r.status_code == 200 and r.mimetype == 'application/json'
    assert 'attachment' in r.headers['Content-Disposition']
    data = r.get_json()
    assert data['project']['name'] == 'АИСУЗ' and len(data['services']) == 6 and len(data['entries']) == 24


def test_audit_filters(admin):
    admin.put('/api/services/1', json={'name': 'A'})
    admin.put('/api/projects/1/settings', json={'riskBufferDays': 7})
    data = admin.get('/api/audit').get_json()
    assert set(data['filters']['pages']) == {'services', 'settings'}
    assert data['filters']['usernames'] == ['admin']
    assert {e['page'] for e in audit(admin, page='settings')} == {'settings'}
    assert all(e['projectId'] == 1 for e in audit(admin, projectId='1'))
    assert audit(admin, projectId='2') == []


# ─── Валидация значений ──────────────────────────────────────────


@pytest.mark.parametrize('payload', [
    {'date': '2026-07-30', 'remaining': 'abc'},
    {'date': '2026-07-30', 'remaining': -1},
    {'date': '2026-07-30', 'spent': True},
    {'date': '2026-07-30', 'estimate': float('inf')},
    {'date': '30.07.2026'},
    {'date': '2026-02-30'},
])
def test_entry_invalid_values_rejected(admin, payload):
    r = admin.post('/api/services/1/entries', json=payload)
    assert r.status_code == 400 and r.get_json()['error']


def test_entry_numeric_strings_accepted(admin):
    eid = admin.post('/api/services/1/entries', json={'date': '2026-07-30', 'remaining': '12.5', 'spent': ''}).get_json()['id']
    entry = next(e for e in admin.get('/api/projects/1/data').get_json()['entries'] if e['id'] == eid)
    assert (entry['remaining'], entry['spent']) == (12.5, None)


def test_entry_duplicate_date_rejected(admin):
    r = admin.post('/api/services/1/entries', json={'date': '2026-07-29'})
    assert r.status_code == 400 and 'уже есть' in r.get_json()['error']
    # перенос существующего замера на занятую дату
    r = admin.put('/api/entries/1', json={'date': '2026-07-24', 'remaining': 509, 'spent': 186, 'estimate': 150})
    assert r.status_code == 400
    # тот же замер с прежней датой сохраняется
    assert admin.put('/api/entries/1', json={'date': '2026-07-17', 'remaining': 500, 'spent': 186, 'estimate': 150}).status_code == 200


def test_entry_update_audit_without_noise(admin):
    admin.put('/api/entries/1', json={'date': '2026-07-17', 'remaining': 509, 'spent': 186, 'estimate': 150})
    assert audit(admin, page='data') == []  # 509 и 509.0 — одно и то же значение
    admin.put('/api/entries/1', json={'date': '2026-07-17', 'remaining': 500, 'spent': 186, 'estimate': 150})
    assert [(e['fieldName'], e['oldValue'], e['newValue']) for e in audit(admin, page='data')] == [('remaining', '509.0', '500.0')]


def test_service_target_date_validation(admin):
    assert admin.put('/api/services/1', json={'targetDate': '31.08.2026'}).status_code == 400
    assert admin.put('/api/services/1', json={'targetDate': ''}).status_code == 200
    assert admin.post('/api/projects/1/services', json={'name': 'x', 'targetDate': 'завтра'}).status_code == 400
    assert admin.put('/api/projects/1/stage_targets', json={'stageName': 'Этап 4', 'targetDate': '2026-13-01'}).status_code == 400


@pytest.mark.parametrize('payload', [
    {'forecastMethod': 'magic'},
    {'burnrateMethod': 'median'},
    {'riskBufferDays': -1},
    {'riskBufferDays': 'три'},
    {'burnrateWindowDays': 0},
])
def test_settings_validation(admin, payload):
    assert admin.put('/api/projects/1/settings', json=payload).status_code == 400


# ─── Замер по всем эпикам ────────────────────────────────────────

def test_bulk_measurement_creates_and_updates(admin):
    r = admin.post('/api/projects/1/measurements', json={'date': '2026-07-29', 'items': [
        {'serviceId': 1, 'remaining': 340, 'spent': 260, 'estimate': 401},   # замер на дату уже есть → обновление
        {'serviceId': 2, 'remaining': None, 'spent': None, 'estimate': None},  # пустая строка пропускается
    ]})
    assert r.status_code == 200
    assert [(e['id'], e['remaining']) for e in r.get_json()['entries']] == [(19, 340)]

    r = admin.post('/api/projects/1/measurements', json={'date': '2026-07-31', 'items': [
        {'serviceId': 3, 'remaining': 100},
        {'serviceId': 4, 'remaining': '190.5', 'spent': 2010, 'estimate': 1310},
    ]})
    saved = r.get_json()['entries']
    assert [(e['service_id'], e['date'], e['remaining']) for e in saved] == [(3, '2026-07-31', 100), (4, '2026-07-31', 190.5)]
    entries = admin.get('/api/projects/1/data').get_json()['entries']
    assert len(entries) == 26
    log = audit(admin, page='data')
    assert {e['action'] for e in log} == {'create', 'update'}


def test_bulk_measurement_validation(admin, make_user):
    url = '/api/projects/1/measurements'
    assert admin.post(url, json={'items': [{'serviceId': 1, 'remaining': 1}]}).status_code == 400  # нет даты
    assert admin.post(url, json={'date': '2026-07-31', 'items': []}).status_code == 400
    assert admin.post(url, json={'date': '2026-07-31', 'items': [{'serviceId': 1}]}).status_code == 400  # всё пусто
    assert admin.post(url, json={'date': '2026-07-31', 'items': [{'serviceId': 999, 'remaining': 1}]}).status_code == 400
    # одна некорректная строка — не сохраняется ничего
    r = admin.post(url, json={'date': '2026-07-31', 'items': [{'serviceId': 1, 'remaining': 1}, {'serviceId': 2, 'remaining': -5}]})
    assert r.status_code == 400
    assert len(admin.get('/api/projects/1/data').get_json()['entries']) == 24
    viewer = make_user('viewer', project_id=1)
    assert viewer.post(url, json={'date': '2026-07-31', 'items': [{'serviceId': 1, 'remaining': 1}]}).status_code == 403


def test_bulk_measurement_rejects_service_from_other_project(admin):
    sid = admin.post('/api/projects/2/services', json={'name': 'Чужой'}).get_json()['id']
    r = admin.post('/api/projects/1/measurements', json={'date': '2026-07-31', 'items': [{'serviceId': sid, 'remaining': 1}]})
    assert r.status_code == 400


# ─── Реестр: постраничная загрузка ───────────────────────────────

def test_audit_pagination(admin):
    for i in range(5):
        admin.put('/api/services/1', json={'name': f'Имя {i}'})
    first = admin.get('/api/audit?limit=2').get_json()
    assert len(first['entries']) == 2 and first['hasMore'] is True
    last_id = first['entries'][-1]['id']
    rest = admin.get(f'/api/audit?limit=10&beforeId={last_id}').get_json()
    assert len(rest['entries']) == 3 and rest['hasMore'] is False
    assert all(e['id'] < last_id for e in rest['entries'])
    assert admin.get('/api/audit?projectId=abc').status_code == 200  # мусор в фильтре игнорируется


# ─── Безопасность и инфраструктура ───────────────────────────────

def test_session_cookie_flags(client):
    r = login(client, *ADMIN)
    cookie = r.headers['Set-Cookie']
    assert 'HttpOnly' in cookie and 'SameSite=Lax' in cookie


def test_secret_key_persisted_next_to_db(tmp_path, monkeypatch):
    import app as app_module
    monkeypatch.delenv('SECRET_KEY', raising=False)
    monkeypatch.setattr(app_module, 'DB_PATH', str(tmp_path / 'x.db'))
    key1 = app_module.load_secret_key()
    key_file = tmp_path / '.secret_key'
    assert key_file.read_text() == key1 and len(key1) == 64
    assert oct(key_file.stat().st_mode & 0o777) == '0o600'
    assert app_module.load_secret_key() == key1  # повторный запуск — тот же ключ
    monkeypatch.setenv('SECRET_KEY', 'k' * 40)
    assert app_module.load_secret_key() == 'k' * 40
    # ключ из примера README / слишком короткий — не используется: берётся случайный ключ из файла
    monkeypatch.setenv('SECRET_KEY', 'ваш-секретный-ключ-замените-это')
    assert app_module.load_secret_key() == key1


def test_indexes_created(db_path):
    import sqlite3
    conn = sqlite3.connect(db_path)
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")}
    conn.close()
    assert {'ux_entries_service_date', 'idx_services_project', 'idx_audit_project'} <= names


def test_migrate_old_database_keeps_data(db_path, monkeypatch):
    """Старая база без новых колонок и индексов дополняется без потери данных."""
    import sqlite3
    import app as app_module
    conn = sqlite3.connect(db_path)
    for idx in ('ux_entries_service_date', 'idx_services_project', 'idx_audit_project'):
        conn.execute(f'DROP INDEX {idx}')
    conn.execute('ALTER TABLE services DROP COLUMN is_done')
    conn.execute('ALTER TABLE services DROP COLUMN stage_group')
    conn.commit()
    conn.close()
    app_module.migrate_db()
    conn = sqlite3.connect(db_path)
    cols = [r[1] for r in conn.execute('PRAGMA table_info(services)')]
    assert 'is_done' in cols and 'stage_group' in cols
    assert conn.execute('SELECT COUNT(*) FROM entries').fetchone()[0] == 24
    assert conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE name = 'ux_entries_service_date'").fetchone()[0] == 1
    conn.close()


# ─── Устойчивость к некорректным запросам ────────────────────────

def test_deleted_user_session_is_invalidated(admin, make_user):
    c = make_user('temp', project_id=1)
    assert c.get('/api/projects').status_code == 200
    admin.delete(f'/api/users/{c.user_id}')
    assert c.get('/api/projects').status_code == 401
    assert c.get('/api/projects/1/data').status_code == 401


def test_deleted_admin_session_is_invalidated(admin, flask_app):
    other = flask_app.test_client()
    uid = admin.post('/api/users', json={'username': 'admin2', 'password': 'p', 'role': 'admin'}).get_json()['id']
    assert login(other, 'admin2', 'p').status_code == 200
    admin.delete(f'/api/users/{uid}')
    assert other.get('/api/users').status_code == 401


def test_non_string_fields_do_not_crash(client, admin):
    assert client.post('/api/auth/login', json={'username': 123, 'password': 456}).status_code in (400, 401)
    assert admin.post('/api/projects', json={'name': 42}).get_json()['name'] == '42'
    assert admin.post('/api/users', json={'username': 7, 'password': 8}).status_code == 400


@pytest.mark.parametrize('photo, status', [
    ('data:image/png;base64,AAAA', 200),
    ('', 200),
    ('javascript:alert(1)', 400),
    ('https://example.com/a.png', 400),
    ('data:image/png;base64,' + 'A' * 2_300_000, 400),
], ids=['png', 'empty', 'javascript-url', 'http-url', 'too-big'])  # короткие имена: огромная строка в имени теста раздувает лог
def test_photo_validation(make_user, photo, status):
    c = make_user('u1')
    assert c.put('/api/profile', json={'photo': photo}).status_code == status


def test_profile_personal_fields_only_for_admin(make_user, admin):
    c = make_user('u1')
    c.put('/api/profile', json={'displayName': 'Взлом', 'email': 'x@y'})
    user = next(u for u in admin.get('/api/users').get_json() if u['username'] == 'u1')
    assert user['displayName'] == '' and user['email'] == ''
    admin.put('/api/profile', json={'displayName': 'Главный'})
    assert admin.get('/api/auth/me').get_json()['user']['displayName'] == 'Главный'


def test_assign_to_missing_project_or_user(admin):
    uid = admin.post('/api/users', json={'username': 'u1', 'password': 'p'}).get_json()['id']
    assert admin.post(f'/api/users/{uid}/projects', json={'projectId': 999}).status_code == 404
    assert admin.post('/api/users/999/projects', json={'projectId': 1}).status_code == 404


@pytest.mark.parametrize('orders', [None, 'x', [{'id': 1}], [{'id': '1', 'sortOrder': 1}]])
def test_reorder_validation(admin, orders):
    assert admin.post('/api/projects/1/services/reorder', json={'orders': orders}).status_code == 400


def test_settings_null_values_are_ignored(admin, db_path):
    import sqlite3
    conn = sqlite3.connect(db_path)
    conn.execute('UPDATE settings SET risk_buffer_days = NULL WHERE project_id = 1')
    conn.commit()
    conn.close()
    # пустое значение из БД не отдаётся фронтенду (там подставится значение по умолчанию)
    assert 'riskBufferDays' not in admin.get('/api/projects/1/data').get_json()['settings']
    # и null в запросе не ломает сохранение остальных настроек
    r = admin.put('/api/projects/1/settings', json={'forecastMethod': 'average', 'riskBufferDays': None})
    assert r.status_code == 200
    assert admin.get('/api/projects/1/data').get_json()['settings']['forecastMethod'] == 'average'


def test_export_matches_project_data(admin):
    exported = admin.get('/api/projects/1/export').get_json()
    data = admin.get('/api/projects/1/data').get_json()
    assert {k: exported[k] for k in data} == data
    assert admin.get('/api/projects/999/export').status_code == 404


def test_index_redirects_deleted_user(admin, make_user):
    c = make_user('temp')
    assert c.get('/').status_code == 200
    admin.delete(f'/api/users/{c.user_id}')
    r = c.get('/')
    assert r.status_code == 302 and r.headers['Location'].endswith('/login')


def test_audit_text_changes_without_none_strings(admin):
    admin.put('/api/profile', json={'email': 'a@b.ru', 'position': ''})
    log = audit(admin, page='profile')
    assert [(e['fieldName'], e['oldValue'], e['newValue']) for e in log] == [('email', None, 'a@b.ru')]
    uid = admin.post('/api/users', json={'username': 'u1', 'password': 'p'}).get_json()['id']
    admin.put(f'/api/users/{uid}', json={'position': 'Аналитик', 'email': ''})
    changes = [(e['fieldName'], e['oldValue'], e['newValue']) for e in audit(admin, page='users') if e['action'] == 'update']
    assert changes == [('position', None, 'Аналитик')]


def test_unchanged_stage_target_not_logged(admin):
    admin.put('/api/projects/1/stage_targets', json={'stageName': 'Этап 4', 'targetDate': '2026-08-31'})
    assert audit(admin, page='stages') == []
    admin.put('/api/projects/1/stage_targets', json={'stageName': 'Этап 4', 'targetDate': '2026-09-02'})
    assert [(e['oldValue'], e['newValue']) for e in audit(admin, page='stages')] == [('2026-08-31', '2026-09-02')]
