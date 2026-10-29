"""Защита и устойчивость: сессии, CSRF, проверка входных данных, целостность замеров, адрес Jira."""

import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import app as app_module
import fake_jira
import jira_integration as jira
from conftest import ADMIN, login
from fake_jira import FakeJira
from test_history import make_button, restore
from test_jira import connect, create_button


@pytest.fixture
def fake():
    server = FakeJira()
    yield server
    server.stop()


def query(flask_app, sql, params=()):
    with flask_app.app_context():
        return [tuple(r) for r in app_module.get_db().execute(sql, params).fetchall()]


class _Reply(BaseHTTPRequestHandler):
    """Сервер «не Jira»: ответ задаётся классом-наследником (status, headers_map, body)."""
    status, headers_map, body = 200, {}, b''

    def _reply(self):
        self.send_response(self.status)
        for k, v in self.headers_map.items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(self.body)

    do_GET = do_POST = _reply

    def log_message(self, *args):
        pass


@pytest.fixture
def not_jira():
    servers = []

    def start(status, body, content_type='text/html', **headers):
        handler = type('H', (_Reply,), {'status': status, 'body': body,
                                        'headers_map': dict({'Content-Type': content_type}, **headers)})
        srv = ThreadingHTTPServer(('127.0.0.1', 0), handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        servers.append(srv)
        return f'http://127.0.0.1:{srv.server_port}'
    yield start
    for srv in servers:
        srv.shutdown()


# ─── Ключ и сессии ───────────────────────────────────────────────

def test_rekey_secrets_after_weak_key(db_path, monkeypatch):
    """Токены, зашифрованные коротким SECRET_KEY, перешифровываются новым ключом — подключения не теряются."""
    weak = 'ваш-секретный-ключ-замените-это'
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO jira_connections (project_id, base_url, instance_type, login, token_encrypted) "
                 "VALUES (1, 'https://jira.example.ru', 'server', '', ?)", (jira.encrypt_token(weak, 'pat-1'),))
    conn.execute("INSERT INTO app_settings (key, value) VALUES ('telegram_token', ?)", (jira.encrypt_token(weak, '1:TG'),))
    conn.commit()
    monkeypatch.setenv('SECRET_KEY', weak)
    monkeypatch.setattr(app_module.app, 'secret_key', 'n' * 64)
    app_module.rekey_secrets(conn)
    app_module.rekey_secrets(conn)  # повторный запуск (другой воркер) ничего не ломает
    token = conn.execute('SELECT token_encrypted FROM jira_connections WHERE project_id = 1').fetchone()[0]
    tg = conn.execute("SELECT value FROM app_settings WHERE key = 'telegram_token'").fetchone()[0]
    conn.close()
    assert jira.decrypt_token('n' * 64, token) == 'pat-1'
    assert jira.decrypt_token('n' * 64, tg) == '1:TG'


def test_password_change_ends_other_sessions(admin, make_user, flask_app):
    user = make_user('ivanov', project_id=1)
    second = flask_app.test_client()
    assert login(second, 'ivanov', 'pw-12345').status_code == 200
    # пользователь сменил пароль сам: текущая сессия остаётся, другая завершается
    assert user.put('/api/profile', json={'password': 'new-pass-1'}).status_code == 200
    assert user.get('/api/projects').status_code == 200
    assert second.get('/api/projects').status_code == 401
    # пароль сменил администратор — завершается и она
    assert admin.put(f'/api/users/{user.user_id}', json={'password': 'by-admin-2'}).status_code == 200
    assert user.get('/api/projects').status_code == 401
    # администратор сменил пароль себе — его сессия продолжается
    assert admin.put('/api/users/1', json={'password': 'admin-new-3'}).status_code == 200
    assert admin.get('/api/users').status_code == 200


def test_default_password_flag(admin, flask_app):
    assert admin.get('/api/auth/me').get_json()['defaultPassword'] is True
    assert admin.put('/api/profile', json={'password': 'Strong-pass-2026'}).get_json() == {'success': True}
    assert admin.get('/api/auth/me').get_json()['defaultPassword'] is False
    other = flask_app.test_client()
    assert login(other, 'admin', 'Strong-pass-2026').status_code == 200
    assert other.get('/api/auth/me').get_json()['defaultPassword'] is False


def test_login_throttling(client, make_user):
    make_user('petrova')
    for _ in range(10):
        assert login(client, 'petrova', 'wrong').status_code == 401
    r = login(client, 'petrova', 'pw-12345')
    assert r.status_code == 429 and '15 минут' in r.get_json()['error']
    assert login(client, *ADMIN).status_code == 200  # другой логин не заблокирован


def test_post_requires_json_and_security_headers(admin):
    r = admin.post('/api/projects/1/services', data={'name': 'x'})
    assert r.status_code == 415
    r = admin.post('/api/projects/1/jira/connection/test', data='{}', content_type='text/plain')
    assert r.status_code == 415
    r = admin.get('/api/projects')
    assert r.headers['X-Frame-Options'] == 'DENY' and r.headers['X-Content-Type-Options'] == 'nosniff'


# ─── Проверка входных данных ─────────────────────────────────────

@pytest.mark.parametrize('method, url, body', [
    ('post', '/api/auth/login', [1]),
    ('post', '/api/projects/1/services', {'name': {'a': 1}}),
    ('put', '/api/services/1', {'etap': [1]}),
    ('put', '/api/services/1', {'name': 'x' * 301}),
    ('put', '/api/services/1', {'name': 'A\ud800B'}),
    ('put', '/api/projects/1/stage_targets', {'stageName': {'a': 1}, 'targetDate': '2026-01-01'}),
    ('post', '/api/projects/1/services/reorder', {'orders': [{'id': 1, 'sortOrder': 10 ** 30}]}),
    ('put', '/api/projects/1/settings', {'riskBufferDays': 10 ** 30}),
    ('post', '/api/projects/1/measurements', {'date': '2026-08-01', 'items': [{'serviceId': [1], 'remaining': 1}]}),
    ('post', '/api/projects/1/jira/buttons', {'name': 'x', 'mappings': [{'serviceId': [1], 'issueKeys': 'ABC-1'}]}),
    ('put', '/api/profile', {'password': 12345}),
    ('post', '/api/users/2/projects', {'projectId': [1]}),
    ('post', '/api/services/1/entries', {'date': '2026-7-29'}),
    ('post', '/api/services/1/entries', {'date': '2026-07- 9'}),
])
def test_bad_input_is_400(admin, method, url, body):
    r = getattr(admin, method)(url, json=body)
    assert r.status_code == 400, (url, r.get_data(as_text=True)[:200])


def test_bad_numbers_in_url_and_query(admin):
    assert admin.get('/api/projects/100000000000000000000/data').status_code == 400
    assert admin.get('/api/projects/1/pdf?epics=%C2%B2').status_code == 200  # «²» — не число, игнорируется
    assert admin.get('/api/audit?limit=%C2%B2').status_code == 200
    assert admin.post('/api/projects/999/services', json={'name': 'x'}).status_code == 404


def test_pdf_fit_long_text_is_fast():
    import time
    from fpdf import FPDF
    import os
    pdf = FPDF()
    pdf.add_font('DejaVu', '', os.path.join(os.path.dirname(app_module.__file__), 'static', 'fonts', 'dejavu.ttf'))
    pdf.add_page()
    pdf.set_font('DejaVu', size=8)
    started = time.monotonic()
    fitted = app_module._pdf_fit(pdf, 'W' * 50000, 40)
    assert time.monotonic() - started < 0.5
    assert fitted.endswith('…') and pdf.get_string_width(fitted) <= 40


# ─── Замеры: даты, дубликаты, частичное сохранение ───────────────

def test_entry_put_keeps_missing_fields_and_bulk_keeps_empty(admin, flask_app):
    eid, *before = query(flask_app, "SELECT id, spent, estimate FROM entries WHERE service_id = 1 AND date = '2026-07-29'")[0]
    assert admin.put(f'/api/entries/{eid}', json={'remaining': 300}).status_code == 200
    assert query(flask_app, 'SELECT remaining, spent, estimate FROM entries WHERE id = ?', (eid,))[0] == (300, *before)
    # явный null по-прежнему очищает значение (так интерфейс стирает ячейку)
    assert admin.put(f'/api/entries/{eid}', json={'spent': None}).status_code == 200
    assert query(flask_app, 'SELECT spent FROM entries WHERE id = ?', (eid,))[0] == (None,)
    # замер по всем эпикам: пустое поле не затирает внесённое значение
    old = query(flask_app, "SELECT spent, estimate FROM entries WHERE service_id = 2 AND date = '2026-07-29'")[0]
    r = admin.post('/api/projects/1/measurements', json={'date': '2026-07-29', 'items': [
        {'serviceId': 2, 'remaining': 200, 'spent': None, 'estimate': None}]})
    assert r.status_code == 200
    assert query(flask_app, "SELECT remaining, spent, estimate FROM entries WHERE service_id = 2 "
                            "AND date = '2026-07-29'")[0] == (200, *old)


def test_one_entry_per_date(admin, flask_app):
    assert admin.post('/api/services/1/entries', json={'date': '2026-07-29'}).status_code == 400
    with pytest.raises(sqlite3.IntegrityError):
        with flask_app.app_context():
            app_module.get_db().execute("INSERT INTO entries (service_id, date) VALUES (1, '2026-07-29')")


def test_migration_normalizes_dates_and_merges_duplicates(db_path):
    conn = sqlite3.connect(db_path)
    conn.execute('DROP INDEX ux_entries_service_date')
    conn.execute("INSERT INTO entries (service_id, date, remaining, spent, estimate) VALUES (1, '2026-7-29', NULL, 7, 8)")
    conn.execute("INSERT INTO entries (service_id, date, remaining, spent, estimate) VALUES (1, '2026-8-3', 1, 2, 3)")
    conn.execute("UPDATE services SET target_date = '2026-9-1' WHERE id = 1")
    conn.commit()
    conn.close()
    app_module.migrate_db()
    conn = sqlite3.connect(db_path)
    rows = conn.execute("SELECT remaining, spent, estimate FROM entries WHERE service_id = 1 AND date = '2026-07-29'").fetchall()
    assert len(rows) == 1 and rows[0][0] == 346  # самая поздняя запись — с пустым остатком; остаток взят из прежней
    assert rows[0][1:] == (7, 8)
    assert conn.execute("SELECT COUNT(*) FROM entries WHERE date = '2026-08-03'").fetchone()[0] == 1
    assert conn.execute('SELECT target_date FROM services WHERE id = 1').fetchone()[0] == '2026-09-01'
    conn.close()


# ─── Адрес Jira и ответы не от Jira ──────────────────────────────

@pytest.mark.parametrize('url', ['http://127.0.0.1:9/secret?x=', 'http://user:pw@jira.example.ru', 'jira.example.ru',
                                 'https://jira.example.ru/#frag', 'https://jira.example.ru:99999'])
def test_jira_url_must_be_plain(admin, url):
    r = admin.put('/api/projects/1/jira/connection', json={'baseUrl': url, 'instanceType': 'server', 'token': 't'})
    assert r.status_code == 400
    r = admin.post('/api/projects/1/jira/connection/test', json={'baseUrl': url, 'instanceType': 'server', 'token': 't'})
    assert r.status_code == 502


def test_jira_allowed_hosts(admin, monkeypatch, fake):
    monkeypatch.setenv('JIRA_ALLOWED_HOSTS', 'jira.company.ru, .corp.ru')
    assert jira.check_base_url('https://jira.company.ru/jira') == 'https://jira.company.ru/jira'
    assert jira.check_base_url('https://tasks.corp.ru') == 'https://tasks.corp.ru'
    r = connect(admin, fake)
    assert r.status_code == 400 and 'JIRA_ALLOWED_HOSTS' in r.get_json()['error']


def test_jira_error_body_is_not_echoed(admin, not_jira):
    url = not_jira(500, b'INTERNAL-ONLY: db_password=hunter2')
    r = admin.post('/api/projects/1/jira/connection/test', json={'baseUrl': url, 'instanceType': 'server', 'token': 't'})
    assert r.status_code == 502 and 'hunter2' not in r.get_json()['error']


def test_jira_redirect_is_not_followed(admin, not_jira):
    target = not_jira(200, b'{"displayName": "attacker"}', 'application/json')
    url = not_jira(302, b'', Location=target + '/rest/api/2/myself')
    r = admin.post('/api/projects/1/jira/connection/test', json={'baseUrl': url, 'instanceType': 'server', 'token': 't'})
    assert r.status_code == 502 and 'перенаправляет' in r.get_json()['error']


def test_jira_sso_page_gives_clear_error_and_run_now_records_it(admin, flask_app, fake, not_jira):
    bid = make_button(admin, fake)
    sso = not_jira(200, b'<html>SSO login</html>')
    with flask_app.app_context():
        db = app_module.get_db()
        db.execute('UPDATE jira_connections SET base_url = ? WHERE project_id = 1', (sso,))
        db.commit()
    r = admin.post(f'/api/jira/buttons/{bid}/snapshot', json={})
    assert r.status_code == 502 and 'страница входа' in r.get_json()['error']
    r = admin.post(f'/api/jira/buttons/{bid}/schedule/run')
    assert r.status_code == 502
    status, message = query(flask_app, 'SELECT last_status, last_message FROM jira_schedules WHERE button_id = ?', (bid,))[0]
    assert status == 'error' and 'страница входа' in message


def test_history_totals_round_once(admin, flask_app, fake, monkeypatch):
    """Сумма восстановленной истории — как у среза: значения задач складываются до округления."""
    monkeypatch.setitem(flask_app.config, 'HISTORY_SYNC', True)
    monkeypatch.setitem(fake_jira.ISSUES[101], 'timeestimate', 1200)
    monkeypatch.setitem(fake_jira.ISSUES[107], 'timeestimate', 1200)
    bid = make_button(admin, fake, structureId=None)
    assert restore(admin, bid, **{'from': '2026-10-26', 'to': '2026-10-26'}).status_code in (200, 202)
    rem = query(flask_app, "SELECT remaining FROM entries WHERE service_id = 1 AND date = '2026-10-26'")[0][0]
    snap = query(flask_app, "SELECT remaining FROM entries WHERE service_id = 1 AND date = '2026-10-28'")
    assert rem == 0.67  # 2 × 1200 с = 0,667 ч; по округлённым задачам вышло бы 0,66
    assert not snap or snap[0][0] in (None, 0.67)
