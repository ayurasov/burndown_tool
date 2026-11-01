"""Отчёт в Confluence: подключение администратора, настройки проекта, публикация вручную и по расписанию."""

from datetime import datetime, timezone

import pytest

import app as app_module
import confluence
from fake_confluence import FakeConfluence


@pytest.fixture
def conf():
    server = FakeConfluence()
    yield server
    server.stop()


def connect(admin, conf, **over):
    body = {'baseUrl': conf.url, 'instanceType': 'server', 'login': '', 'token': 'conf-pat', 'acceptInvalidCerts': False}
    body.update(over)
    return admin.put('/api/admin/confluence-connection', json=body)


def configure(admin, pid=1, **over):
    body = {'enabled': True, 'spaceKey': 'PROJ', 'parentId': '', 'title': 'Burndown — {project}', 'mode': 'single',
            'weekday': 1, 'time': '09:00', 'timezone': 'Europe/Moscow'}
    body.update(over)
    return admin.put(f'/api/projects/{pid}/confluence', json=body)


# ─── Подключение (администратор) ─────────────────────────────────

def test_connection_roundtrip_and_test(admin, conf, flask_app, make_user):
    assert admin.get('/api/admin/confluence-connection').get_json() is None
    r = connect(admin, conf)
    assert r.status_code == 200 and r.get_json() == {'baseUrl': conf.url, 'instanceType': 'server', 'login': '',
                                                     'acceptInvalidCerts': False, 'hasToken': True}
    with flask_app.app_context():
        stored = app_module._app_setting(app_module.get_db(), 'conf_token')
    assert 'conf-pat' not in stored  # токен — зашифрован
    assert admin.post('/api/admin/confluence-connection/test', json={}).get_json() == {'user': 'Сервисная учётная запись'}
    bad = admin.post('/api/admin/confluence-connection/test', json={'baseUrl': conf.url, 'instanceType': 'server',
                                                                    'token': 'wrong-token'})
    assert bad.status_code == 502 and '401' in bad.get_json()['error']
    odd = admin.post('/api/admin/confluence-connection/test', json={'baseUrl': conf.url, 'instanceType': 'server',
                                                                    'token': 'неверный'})
    assert odd.status_code == 502 and 'недопустимые символы' in odd.get_json()['error']
    moved = admin.post('/api/admin/confluence-connection/test', json={'baseUrl': conf.url + '/x', 'instanceType': 'server'})
    assert moved.status_code == 502 and 'токен' in moved.get_json()['error']  # сохранённый токен — только на тот же адрес
    assert connect(admin, conf, token='').status_code == 200  # пустой токен — прежний сохраняется
    audit = admin.get('/api/audit?page=integrations').get_json()['entries']
    assert any(a['entityType'] == 'confluence_connection' for a in audit)
    assert make_user('ivan').get('/api/admin/confluence-connection').status_code == 403
    assert admin.delete('/api/admin/confluence-connection').get_json() == {'success': True}
    assert admin.get('/api/admin/confluence-connection').get_json() is None
    test = admin.post('/api/admin/confluence-connection/test', json={})
    assert test.status_code == 502 and 'не настроено' in test.get_json()['error']


def test_connection_validation(admin, conf, monkeypatch):
    for over, message in [({'baseUrl': 'ftp://x'}, 'http'), ({'baseUrl': 'https://u@host'}, 'без логина'),
                          ({'instanceType': 'x'}, 'тип'), ({'instanceType': 'cloud', 'login': ''}, 'email'),
                          ({'token': 5}, 'строка'), ({'token': ''}, 'токен')]:
        r = connect(admin, conf, **over)
        assert r.status_code == 400 and message in r.get_json()['error'], over
    connect(admin, conf)
    r = connect(admin, conf, baseUrl=conf.url + '/wiki', token='')
    assert r.status_code == 400 and 'заново' in r.get_json()['error']
    monkeypatch.setenv('CONFLUENCE_ALLOWED_HOSTS', '.company.ru')
    assert 'запрещено' in connect(admin, conf).get_json()['error']
    assert confluence.check_base_url('https://wiki.company.ru/') == 'https://wiki.company.ru'
    with pytest.raises(confluence.ConfluenceError, match='Некорректный'):
        confluence.check_base_url('https://host:99999')


def test_cloud_basic_auth(admin, flask_app):
    server = FakeConfluence(mode='cloud', login='pm@company.ru', token='api-token')
    try:
        r = admin.put('/api/admin/confluence-connection', json={'baseUrl': server.url, 'instanceType': 'cloud',
                                                                 'login': 'pm@company.ru', 'token': 'api-token'})
        assert r.status_code == 200
        assert admin.post('/api/admin/confluence-connection/test', json={}).get_json()['user']
    finally:
        server.stop()


# ─── Отчёт проекта ───────────────────────────────────────────────

def test_report_settings_validation(admin, conf, make_user):
    data = admin.get('/api/projects/1/confluence').get_json()
    assert data['connected'] is False and data['mode'] == 'single' and data['title'] == 'Burndown — {project}'
    assert 'не настроено' in configure(admin).get_json()['error']
    connect(admin, conf)
    for over, message in [({'spaceKey': 'Пробел и кириллица'}, 'пространства'), ({'parentId': 'abc'}, 'числовой'),
                          ({'mode': 'daily'}, 'Режим'), ({'weekday': 8}, 'День недели'), ({'weekday': True}, 'День недели'),
                          ({'time': '9:00'}, 'ЧЧ:ММ'), ({'timezone': 'Марс/Олимп'}, 'часовой пояс'),
                          ({'spaceKey': ''}, 'пространство')]:
        r = configure(admin, **over)
        assert r.status_code == 400 and message in r.get_json()['error'], over
    assert make_user('viewer', project_id=1).get('/api/projects/1/confluence').status_code == 403
    assert admin.get('/api/projects/999/confluence').status_code == 404
    r = configure(admin)
    assert r.status_code == 200 and r.get_json()['enabled'] and r.get_json()['spaceKey'] == 'PROJ'
    audit = admin.get('/api/audit?page=confluence').get_json()['entries']
    assert {a['fieldName'] for a in audit} >= {'enabled', 'spaceKey'}


def test_publish_creates_then_updates_single_page(admin, conf):
    connect(admin, conf)
    admin.put('/api/admin/notify-settings', json={'publicUrl': 'https://burndown.example.ru'})
    configure(admin)
    r = admin.post('/api/projects/1/confluence/publish')
    assert r.status_code == 200
    data = r.get_json()
    assert data['result']['status'] == 'ok' and data['result']['message'] == 'Создана страница «Burndown — АИСУЗ»'
    page = next(iter(conf.state['pages'].values()))
    assert page['title'] == 'Burndown — АИСУЗ' and page['space'] == 'PROJ'
    body = page['body']
    assert 'ac:name="status"' in body and '<h2>Эпики</h2>' in body and '<h2>Этапы</h2>' in body
    assert 'Главные риски' in body and 'https://burndown.example.ru/?project=1' in body
    assert data['pageId'] == page['id'] and data['pageUrl'].endswith(f"pageId={page['id']}")
    again = admin.post('/api/projects/1/confluence/publish').get_json()
    assert again['result']['message'] == 'Обновлена страница «Burndown — АИСУЗ»' and len(conf.state['pages']) == 1
    assert page['version'] == 2
    configure(admin, title='Сроки: {project}')  # переименование — та же страница (по id)
    admin.post('/api/projects/1/confluence/publish')
    assert len(conf.state['pages']) == 1 and page['title'] == 'Сроки: АИСУЗ'
    audit = admin.get('/api/audit?page=confluence').get_json()['entries']
    assert any(a['action'] == 'publish' for a in audit)


def test_publish_finds_existing_page_by_title(admin, conf, flask_app):
    connect(admin, conf)
    configure(admin)
    conf.state['pages']['77'] = {'id': '77', 'title': 'Burndown — АИСУЗ', 'space': 'PROJ', 'version': 5, 'body': '',
                                 'parent': None}
    assert admin.post('/api/projects/1/confluence/publish').get_json()['pageId'] == '77'
    assert conf.state['pages']['77']['version'] == 6
    del conf.state['pages']['77']  # страницу удалили в Confluence — создаётся заново
    assert admin.post('/api/projects/1/confluence/publish').get_json()['result']['message'].startswith('Создана')


def test_weekly_mode_creates_page_per_week_under_parent(admin, conf):
    connect(admin, conf)
    conf.state['pages']['500'] = {'id': '500', 'title': 'Отчёты', 'space': 'PROJ', 'version': 1, 'body': '', 'parent': None}
    configure(admin, mode='weekly', parentId='500')
    data = admin.post('/api/projects/1/confluence/publish').get_json()
    week = datetime.now().date().isocalendar()[1]
    created = [p for p in conf.state['pages'].values() if p['parent'] == '500']
    assert len(created) == 1 and created[0]['title'].startswith(f'Burndown — АИСУЗ — неделя {week} (')
    assert data['pageId'] is None  # в режиме «каждую неделю» страница не запоминается
    assert admin.post('/api/projects/1/confluence/publish').get_json()['result']['message'].startswith('Обновлена')
    assert app_module.report_page_title({'title': '{project}', 'mode': 'weekly'}, 'П', '2026-11-04') == \
        'П — неделя 45 (02.11–08.11.2026)'


def test_publish_errors(admin, conf):
    connect(admin, conf)
    configure(admin, spaceKey='NOPE')
    r = admin.post('/api/projects/1/confluence/publish')
    assert r.status_code == 502 and 'Родительская страница или пространство не найдены' in r.get_json()['result']['message']
    assert r.get_json()['lastStatus'] == 'error'
    configure(admin, parentId='404')
    assert 'не найдены' in admin.post('/api/projects/1/confluence/publish').get_json()['result']['message']
    conf.configure(readonly=True)
    configure(admin)
    assert '403' in admin.post('/api/projects/1/confluence/publish').get_json()['result']['message']
    configure(admin, enabled=False, spaceKey='')
    assert 'пространство' in admin.post('/api/projects/1/confluence/publish').get_json()['result']['message']


def test_scheduled_reports(admin, conf, flask_app, monkeypatch):
    connect(admin, conf)
    configure(admin, weekday=1, time='09:00')
    monday_10 = datetime(2026, 11, 2, 7, 0, tzinfo=timezone.utc)  # 10:00 МСК, понедельник
    monday_8 = datetime(2026, 11, 2, 5, 0, tzinfo=timezone.utc)
    tuesday = datetime(2026, 11, 3, 7, 0, tzinfo=timezone.utc)
    with flask_app.app_context():
        assert app_module.run_due_reports(monday_8) == 0
        assert app_module.run_due_reports(tuesday) == 0
        assert app_module.run_due_reports(monday_10) == 1
        assert app_module.run_due_reports(monday_10) == 0  # один раз в день
    assert len(conf.state['pages']) == 1
    audit = admin.get('/api/audit?page=confluence').get_json()['entries']
    assert any(a['username'] == app_module.CONFLUENCE_ACTOR for a in audit)
    with flask_app.app_context():
        db = app_module.get_db()
        db.execute("UPDATE confluence_reports SET timezone = 'Нет/Такого', last_run_date = NULL")
        db.commit()
        assert app_module.run_due_reports(monday_10) == 0
        db.execute("UPDATE confluence_reports SET timezone = 'Europe/Moscow'")
        db.commit()
    monkeypatch.setattr(app_module, 'publish_report', lambda *a, **k: 1 / 0)
    with flask_app.app_context():
        assert app_module.run_due_reports(monday_10) == 1  # сбой записан в журнал службы


def test_report_storage_escapes_and_panels():
    report = {'project': 'P', 'date': '01.11.2026', 'status': 'danger', 'headline': 'Сроки <сорваны>',
              'facts': [{'label': 'Остаток', 'value': '10 ч'}], 'top': [], 'health': [{'severity': 'info', 'text': 'x'}],
              'week': {'from': '25.10.2026', 'to': '01.11.2026', 'items': []},
              'stages': [], 'epics': [{'name': 'Эпик & Ко', 'status': 'zzz', 'remaining': '1 ч', 'forecast': '—',
                                       'target': '—', 'chance': '—'}], 'url': ''}
    body = confluence.report_storage(report)
    assert 'ac:name="warning"' in body and 'Сроки &lt;сорваны&gt;' in body and 'Эпик &amp; Ко' in body
    assert 'Рисков по срокам не видно' in body and 'Существенных изменений' in body and 'Нет прогноза' in body
    assert '<h2>Этапы</h2>' not in body and 'Открыть проект' not in body


def test_client_errors(conf, monkeypatch):
    client = confluence.ConfluenceClient(conf.url, 'server', '', 'conf-pat')
    assert client.space('PROJ') == {'key': 'PROJ', 'name': 'Пространство PROJ'}
    with pytest.raises(confluence.ConfluenceError, match='не найдено'):
        client.space('NOPE')
    assert client.get_page('1') is None
    with pytest.raises(confluence.ConfluenceError, match='Неизвестный тип'):
        confluence.ConfluenceClient(conf.url, 'x', '', 't')
    dead = confluence.ConfluenceClient('http://127.0.0.1:9', 'server', '', 't', timeout=(1, 1))
    with pytest.raises(confluence.ConfluenceError, match='Не удалось подключиться'):
        dead.current_user()
