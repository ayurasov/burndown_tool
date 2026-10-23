"""Тесты REST API: авторизация, права доступа, CRUD, валидация и реестр изменений."""

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
