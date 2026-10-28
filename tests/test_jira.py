"""Связка с Jira: подключение, кнопки среза, расчёт среза из Structure (на фейковой Jira)."""

import pytest

import jira_integration as jira
from fake_jira import FakeJira


@pytest.fixture
def fake():
    server = FakeJira()
    yield server
    server.stop()


def connect(admin, fake, pid=1, **over):
    body = {'baseUrl': fake.url, 'instanceType': 'server', 'login': '', 'token': 'secret-pat',
            'acceptInvalidCerts': False}
    body.update(over)
    return admin.put(f'/api/projects/{pid}/jira/connection', json=body)


def create_button(admin, pid=1, **over):
    body = {'name': 'Срез из релиза', 'structureId': 7, 'structureName': 'Релиз 2026',
            'mappings': [{'serviceId': 1, 'issueKeys': 'ABC-1', 'includeSubtree': True}]}
    body.update(over)
    return admin.post(f'/api/projects/{pid}/jira/buttons', json=body)


# ─── Чистая логика ───────────────────────────────────────────────

def test_token_encryption_roundtrip():
    enc = jira.encrypt_token('key-1', 'p@ss')
    assert 'p@ss' not in enc
    assert jira.decrypt_token('key-1', enc) == 'p@ss'
    with pytest.raises(jira.JiraError, match='SECRET_KEY'):
        jira.decrypt_token('key-2', enc)


def test_parse_issue_keys():
    assert jira.parse_issue_keys('abc-1, ABC-2;\nXYZ_1-30  abc-1') == (['ABC-1', 'ABC-2', 'XYZ_1-30'], [])
    assert jira.parse_issue_keys('ABC-1 мусор 12-3') == (['ABC-1'], ['мусор', '12-3'])
    assert jira.parse_issue_keys('') == ([], [])


def test_forest_formula_and_subtree():
    rows = jira.parse_forest_formula('1:0:101,2:1:102,3:1:5/9,4:2:103:x,5:0:104,bad,7:z:1')
    assert rows == [(0, 101), (1, 102), (1, None), (2, 103), (0, 104)]
    assert jira.subtree_issue_ids(rows, 101) == {101, 102, 103}
    assert jira.subtree_issue_ids(rows, 103) == {103}
    assert jira.subtree_issue_ids(rows, 999) is None
    # задача встречается в структуре дважды — поддеревья объединяются
    rows = jira.parse_forest_formula('1:0:1,2:1:2,3:0:3,4:1:1,5:2:4')
    assert jira.subtree_issue_ids(rows, 1) == {1, 2, 4}


def test_seconds_to_hours():
    assert jira.seconds_to_hours([3600, None, 1800]) == 1.5
    assert jira.seconds_to_hours([None, None]) is None
    assert jira.seconds_to_hours([0]) == 0


# ─── Подключение ─────────────────────────────────────────────────

def test_connection_save_hides_token(admin, fake):
    assert admin.get('/api/projects/1/jira').get_json() == {'connection': None, 'buttons': []}
    r = connect(admin, fake)
    assert r.status_code == 200
    data = admin.get('/api/projects/1/jira').get_json()['connection']
    assert data == {'baseUrl': fake.url, 'instanceType': 'server', 'login': '', 'acceptInvalidCerts': False,
                    'hasToken': True}
    # токен не попадает ни в ответы, ни в реестр изменений
    log = admin.get('/api/audit?page=jira').get_json()['entries']
    assert log and all('secret-pat' not in str(e) for e in log)


def test_connection_stored_encrypted(admin, fake, db_path):
    import sqlite3
    connect(admin, fake)
    stored = sqlite3.connect(db_path).execute('SELECT token_encrypted FROM jira_connections').fetchone()[0]
    assert stored and 'secret-pat' not in stored


@pytest.mark.parametrize('over, message', [
    ({'baseUrl': 'jira.local'}, 'http'),
    ({'instanceType': 'magic'}, 'тип'),
    ({'instanceType': 'cloud', 'login': ''}, 'логин'),
    ({'token': ''}, 'токен'),
])
def test_connection_validation(admin, fake, over, message):
    r = connect(admin, fake, **over)
    assert r.status_code == 400 and message in r.get_json()['error'].lower()


def test_connection_update_keeps_token_but_url_change_requires_it(admin, fake):
    connect(admin, fake)
    assert connect(admin, fake, token='', acceptInvalidCerts=True).status_code == 200
    r = connect(admin, fake, token='', baseUrl='https://other.example.com')
    assert r.status_code == 400 and 'заново' in r.get_json()['error']


def test_test_connection_server_pat(admin, fake):
    connect(admin, fake)
    r = admin.post('/api/projects/1/jira/connection/test')
    assert r.status_code == 200 and r.get_json() == {'success': True, 'user': 'Тестовый Пользователь'}
    # проверка данных формы до сохранения: неверный токен
    r = admin.post('/api/projects/1/jira/connection/test',
                   json={'baseUrl': fake.url, 'instanceType': 'server', 'token': 'wrong'})
    assert r.status_code == 502 and '401' in r.get_json()['error']


def test_stored_token_not_sent_to_other_url(admin, fake):
    connect(admin, fake)
    other = FakeJira()
    try:
        r = admin.post('/api/projects/1/jira/connection/test',
                       json={'baseUrl': other.url, 'instanceType': 'server', 'token': ''})
        assert r.status_code == 502 and 'токен' in r.get_json()['error']
        assert other.state['requests'] == []
    finally:
        other.stop()


@pytest.mark.parametrize('mode', ['cloud', 'server_basic'])
def test_basic_auth_modes(admin, fake, mode):
    fake.configure(mode='cloud' if mode == 'cloud' else 'server', login='user@corp.ru', token='api-token')
    if mode == 'server_basic':
        fake.configure(mode='server_basic')
    connect(admin, fake, instanceType=mode, login='user@corp.ru', token='api-token')
    assert admin.post('/api/projects/1/jira/connection/test').status_code == 200
    expected_api = '/rest/api/3/myself' if mode == 'cloud' else '/rest/api/2/myself'
    assert ('GET', expected_api) in fake.state['requests']


def test_unreachable_jira(admin):
    r = admin.put('/api/projects/1/jira/connection', json={'baseUrl': 'http://127.0.0.1:9', 'instanceType': 'server',
                                                            'token': 't'})
    assert r.status_code == 200
    r = admin.post('/api/projects/1/jira/connection/test')
    assert r.status_code == 502 and 'Не удалось подключиться' in r.get_json()['error']


def test_list_structures(admin, fake):
    connect(admin, fake)
    assert admin.get('/api/projects/1/jira/structures').get_json() == [{'id': 7, 'name': 'Релиз 2026'},
                                                                       {'id': 8, 'name': 'Бэклог'}]
    fake.configure(structure=False)
    r = admin.get('/api/projects/1/jira/structures')
    assert r.status_code == 502 and 'Structure недоступен' in r.get_json()['error']


def test_disconnect(admin, fake):
    connect(admin, fake)
    assert admin.delete('/api/projects/1/jira/connection').status_code == 200
    assert admin.get('/api/projects/1/jira').get_json()['connection'] is None
    r = admin.post('/api/projects/1/jira/connection/test')
    assert r.status_code == 502 and 'не настроено' in r.get_json()['error']


# ─── Кнопки и ассоциации ─────────────────────────────────────────

def test_button_crud(admin, fake):
    connect(admin, fake)
    r = create_button(admin)
    assert r.status_code == 200
    button = r.get_json()
    assert button['mappings'] == [{'serviceId': 1, 'issueKeys': 'ABC-1', 'includeSubtree': True}]
    upd = admin.put(f"/api/jira/buttons/{button['id']}", json={
        'name': 'Новый срез', 'structureId': None,
        'mappings': [{'serviceId': 2, 'issueKeys': 'abc-4, abc-5', 'includeSubtree': False},
                     {'serviceId': 3, 'issueKeys': '   '}]})  # пустые задачи — эпик не привязан
    assert upd.status_code == 200
    assert upd.get_json()['mappings'] == [{'serviceId': 2, 'issueKeys': 'ABC-4, ABC-5', 'includeSubtree': False}]
    assert admin.get('/api/projects/1/jira').get_json()['buttons'][0]['name'] == 'Новый срез'
    assert admin.delete(f"/api/jira/buttons/{button['id']}").status_code == 200
    assert admin.get('/api/projects/1/jira').get_json()['buttons'] == []


@pytest.mark.parametrize('over, message', [
    ({'name': ' '}, 'название'),
    ({'structureId': 'abc'}, 'целое'),
    ({'mappings': []}, 'хотя бы'),
    ({'mappings': [{'serviceId': 999, 'issueKeys': 'ABC-1'}]}, 'не найден'),
    ({'mappings': [{'serviceId': 1, 'issueKeys': 'ABC-1, плохой'}]}, 'плохой'),
])
def test_button_validation(admin, over, message):
    r = create_button(admin, **over)
    assert r.status_code == 400 and message in r.get_json()['error'].lower()


def test_button_rejects_epic_from_other_project(admin):
    sid = admin.post('/api/projects/2/services', json={'name': 'Чужой'}).get_json()['id']
    r = create_button(admin, mappings=[{'serviceId': sid, 'issueKeys': 'ABC-1'}])
    assert r.status_code == 400


def test_viewer_has_no_access(admin, make_user, fake):
    connect(admin, fake)
    bid = create_button(admin).get_json()['id']
    viewer = make_user('viewer', project_id=1)
    assert viewer.get('/api/projects/1/jira').status_code == 403
    assert viewer.post(f'/api/jira/buttons/{bid}/snapshot').status_code == 403
    assert viewer.put('/api/projects/1/jira/connection', json={}).status_code == 403
    manager = make_user('mgr', project_id=1, project_role='manager')
    assert manager.post(f'/api/jira/buttons/{bid}/snapshot').status_code == 200


def test_deleting_epic_and_project_cascade(admin, fake, db_path):
    import sqlite3
    connect(admin, fake)
    create_button(admin, mappings=[{'serviceId': 1, 'issueKeys': 'ABC-1'}, {'serviceId': 2, 'issueKeys': 'ABC-4'}])
    admin.delete('/api/services/1')
    assert [m['serviceId'] for m in admin.get('/api/projects/1/jira').get_json()['buttons'][0]['mappings']] == [2]
    admin.delete('/api/projects/1')
    conn = sqlite3.connect(db_path)
    for table in ('jira_connections', 'jira_snapshot_buttons', 'jira_snapshot_mappings'):
        assert conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0] == 0


# ─── Срез ────────────────────────────────────────────────────────

def snapshot(admin, bid):
    r = admin.post(f'/api/jira/buttons/{bid}/snapshot')
    assert r.status_code == 200, r.get_json()
    return {i['serviceId']: i for i in r.get_json()['items']}


def test_snapshot_structure_subtree(admin, fake):
    connect(admin, fake)
    bid = create_button(admin, mappings=[
        {'serviceId': 1, 'issueKeys': 'ABC-1', 'includeSubtree': True},
        {'serviceId': 2, 'issueKeys': 'ABC-4, ABC-2', 'includeSubtree': False},
        {'serviceId': 3, 'issueKeys': 'ABC-5, ABC-404', 'includeSubtree': True},
    ]).get_json()['id']
    items = snapshot(admin, bid)
    # ABC-1 + ABC-2 + ABC-3 (через папку); скрытая ABC-9 и соседняя ABC-4 не входят
    assert (items[1]['remaining'], items[1]['spent'], items[1]['estimate']) == (12.5, 6, 16)
    assert items[1]['issueCount'] == 3
    assert items[1]['warnings'] == ['1 задач(и) структуры недоступны для просмотра — не учтены']
    # без вложенных — только сами задачи
    assert (items[2]['remaining'], items[2]['spent'], items[2]['estimate']) == (12, 1, 13)
    # задачи нет в структуре и задача не найдена — предупреждения, но значения считаются
    assert items[3]['remaining'] == 1 and items[3]['issueCount'] == 1
    assert items[3]['warnings'] == ['ABC-404: задача не найдена или нет прав на просмотр',
                                    'ABC-5: задачи нет в структуре — учтена только она сама']
    # дерево структуры загружается один раз на срез
    assert fake.state['requests'].count(('POST', '/rest/structure/2.0/forest/latest')) == 1


def test_snapshot_without_structure_sums_subtasks(admin, fake):
    connect(admin, fake)
    bid = create_button(admin, structureId=None, mappings=[
        {'serviceId': 1, 'issueKeys': 'ABC-1', 'includeSubtree': True},
        {'serviceId': 2, 'issueKeys': 'ABC-1', 'includeSubtree': False},
    ]).get_json()['id']
    items = snapshot(admin, bid)
    assert (items[1]['remaining'], items[1]['spent'], items[1]['estimate']) == (14, 7, 18)
    assert (items[2]['remaining'], items[2]['spent'], items[2]['estimate']) == (10, 5, 12)
    assert items[1]['issueCount'] == 2 and items[2]['issueCount'] == 1
    assert ('POST', '/rest/structure/2.0/forest/latest') not in fake.state['requests']
    assert fake.state['search_bodies'][-1]['jql'] == 'parent in (ABC-1)'


def test_snapshot_cloud_uses_search_jql(admin, fake):
    fake.configure(mode='cloud', login='me@corp.ru', token='api-token')
    connect(admin, fake, instanceType='cloud', login='me@corp.ru', token='api-token')
    bid = create_button(admin).get_json()['id']
    assert snapshot(admin, bid)[1]['remaining'] == 12.5
    assert ('POST', '/rest/api/3/search/jql') in fake.state['requests']


def test_snapshot_errors(admin, fake):
    bid = create_button(admin).get_json()['id']
    r = admin.post(f'/api/jira/buttons/{bid}/snapshot')
    assert r.status_code == 502 and 'не настроено' in r.get_json()['error']
    connect(admin, fake)
    admin.put(f'/api/jira/buttons/{bid}', json={'name': 'x', 'structureId': 99,
                                               'mappings': [{'serviceId': 1, 'issueKeys': 'ABC-1'}]})
    r = admin.post(f'/api/jira/buttons/{bid}/snapshot')
    assert r.status_code == 502 and 'Структура 99 не найдена' in r.get_json()['error']
    fake.configure(token='rotated')
    r = admin.post(f'/api/jira/buttons/{bid}/snapshot')
    assert r.status_code == 502 and '401' in r.get_json()['error']


def test_snapshot_does_not_write_entries(admin, fake):
    connect(admin, fake)
    bid = create_button(admin).get_json()['id']
    before = len(admin.get('/api/projects/1/data').get_json()['entries'])
    snapshot(admin, bid)
    assert len(admin.get('/api/projects/1/data').get_json()['entries']) == before


def test_old_database_gets_jira_tables(db_path):
    import sqlite3
    import app as app_module
    conn = sqlite3.connect(db_path)
    conn.executescript('DROP TABLE jira_snapshot_mappings; DROP TABLE jira_snapshot_buttons; DROP TABLE jira_connections;')
    conn.commit()
    conn.close()
    app_module.migrate_db()
    names = {r[0] for r in sqlite3.connect(db_path).execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {'jira_connections', 'jira_snapshot_buttons', 'jira_snapshot_mappings'} <= names


def test_without_jira_libraries_app_works(admin, fake, monkeypatch):
    """Обновили код, но не выполнили pip install: приложение работает, связь с Jira — с понятной ошибкой."""
    monkeypatch.setattr(jira, 'requests', None)
    monkeypatch.setattr(jira, 'Fernet', None)
    assert admin.get('/api/projects/1/data').status_code == 200
    assert admin.get('/api/projects/1/jira').status_code == 200
    r = connect(admin, fake)
    assert r.status_code == 502 and 'pip install -r requirements.txt' in r.get_json()['error']
    r = admin.post('/api/projects/1/jira/connection/test', json={'baseUrl': fake.url, 'instanceType': 'server', 'token': 'x'})
    assert r.status_code == 502 and 'pip install' in r.get_json()['error']
