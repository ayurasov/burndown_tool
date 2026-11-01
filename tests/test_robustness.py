"""Отказоустойчивость: сбои Jira, почты и Telegram, планировщика; редкие ветки и отказ в доступе."""

import smtplib
from datetime import datetime, timezone

import pytest

import app as app_module
import forecast
import jira_integration as jira
import notify
from fake_jira import FakeJira
from fake_notify import FakeSMTP, FakeTelegram
from test_history import make_button, restore
from test_jira import connect, create_button
from test_notifications import SMTP, MONDAY_0930_MSK, fake_smtp, mail_ready, subscribe  # noqa: F401 — фикстуры


@pytest.fixture
def fake():
    server = FakeJira()
    yield server
    server.stop()


def ctx(flask_app, fn, *args, **kwargs):
    with flask_app.app_context():
        return fn(*args, **kwargs)


# ─── Jira: сеть, TLS, повторы ─────────────────────────────────────

@pytest.mark.parametrize('error, detailed, short', [
    ('SSLError', 'Ошибка TLS/сертификата', 'Ошибка TLS-сертификата Jira'),
    ('ConnectTimeout', 'Превышено время ожидания', 'Jira не отвечает'),
    ('ReadTimeout', 'не ответила вовремя', 'не ответила вовремя'),
    ('ConnectionError', 'Не удалось подключиться к https://jira.example.ru', 'Не удалось подключиться к Jira'),
])
def test_network_errors_have_clear_messages(monkeypatch, error, detailed, short):
    exc = getattr(jira.requests.exceptions, error)

    def boom(*args, **kwargs):
        raise exc('boom')
    for is_detailed, expected in ((True, detailed), (False, short)):
        client = jira.JiraClient('https://jira.example.ru', 'server', '', 't', detailed=is_detailed)
        monkeypatch.setattr(client.session, 'request', boom)
        with pytest.raises(jira.JiraError) as e:
            client.myself()
        assert expected in str(e.value)
        assert is_detailed or 'jira.example.ru' not in str(e.value)
    assert client.calls == 1 and client.elapsed >= 0 and 'запрос' in client.stats()


def test_rate_limit_is_retried_with_retry_after(monkeypatch):
    class Resp:
        def __init__(self, code):
            self.status_code, self.headers, self.is_redirect = code, {'Retry-After': '0'}, False

        def json(self):
            return {'displayName': 'Тест'}
    answers = [Resp(429), Resp(503), Resp(200)]
    client = jira.JiraClient('https://jira.example.ru', 'server', '', 't')
    monkeypatch.setattr(client.session, 'request', lambda *a, **k: answers.pop(0))
    monkeypatch.setattr(jira.time, 'sleep', lambda s: None)
    assert client.myself() == 'Тест' and client.calls == 3


def test_insecure_tls_option_and_unknown_type():
    client = jira.JiraClient('https://jira.example.ru', 'server', '', 't', accept_invalid_certs=True)
    assert client.session.verify is False
    with pytest.raises(jira.JiraError, match='тип'):
        jira.JiraClient('https://jira.example.ru', 'magic', '', 't')


def test_forbidden_and_not_found_messages(fake):
    client = jira.JiraClient(fake.url, 'server', '', 'secret-pat')
    with pytest.raises(jira.JiraError, match='Structure недоступен'):
        fake.configure(structure=False)
        client.list_structures()
    assert client.issue('NOPE-1', ['summary']) is None


# ─── Планировщик: сбой одной части не останавливает остальные ─────

def test_scheduler_survives_failing_steps(flask_app, monkeypatch):
    calls = []

    def fail():
        calls.append('alerts')
        raise RuntimeError('boom')
    monkeypatch.setattr(app_module, 'run_alert_checks', fail)
    monkeypatch.setattr(app_module, 'run_due_digests', lambda now=None: calls.append('digests'))
    monkeypatch.setattr(app_module, 'deliver_outbox', lambda: calls.append('outbox'))
    ctx(flask_app, app_module.run_notifications)
    assert calls == ['alerts', 'digests', 'outbox']


def test_scheduler_loop_and_start(db_path, monkeypatch):
    ticks = []

    def sleep(_seconds):
        if len(ticks) == 2:
            raise KeyboardInterrupt  # выход из бесконечного цикла теста
        ticks.append(1)

    def tick():
        raise RuntimeError('сбой одного тика')
    monkeypatch.setattr(app_module.time, 'sleep', sleep)
    monkeypatch.setattr(app_module, 'run_due_schedules', tick)
    with pytest.raises(KeyboardInterrupt):
        app_module._scheduler_loop()
    assert len(ticks) == 2  # после ошибки цикл продолжился
    monkeypatch.setenv('BURNDOWN_SCHEDULER', '1')
    monkeypatch.setattr(app_module, '_scheduler_loop', lambda: None)
    thread = app_module.start_scheduler()
    thread.join(1)
    assert thread.name == 'burndown-autofill'


def test_autofill_internal_error_is_recorded(admin, flask_app, fake, monkeypatch):
    bid = make_button(admin, fake)
    monkeypatch.setattr(app_module, 'run_autofill', lambda *a, **k: (_ for _ in ()).throw(RuntimeError('сломалось')))
    r = admin.post(f'/api/jira/buttons/{bid}/schedule/run')
    assert r.status_code == 502 and 'Внутренняя ошибка: сломалось' in r.get_json()['message']
    admin.put(f'/api/jira/buttons/{bid}/schedule', json={'enabled': True, 'days': [1, 2, 3, 4, 5, 6, 7], 'time': '00:00',
                                                         'timezone': 'UTC', 'overwrite': True})
    with flask_app.app_context():
        db = app_module.get_db()
        db.execute('UPDATE jira_schedules SET last_run_date = NULL')
        db.commit()
        assert app_module.run_due_schedules() == 1
        row = db.execute('SELECT last_status, last_message FROM jira_schedules WHERE button_id = ?', (bid,)).fetchone()
    assert tuple(row) == ('error', 'Внутренняя ошибка: сломалось')


def test_autofill_without_values_is_an_error(admin, flask_app, fake):
    connect(admin, fake)
    bid = create_button(admin, mappings=[{'serviceId': 1, 'issueKeys': 'ABC-404', 'includeSubtree': False}]).get_json()['id']
    with flask_app.app_context():
        db = app_module.get_db()
        row = db.execute('SELECT * FROM jira_snapshot_buttons WHERE id = ?', (bid,)).fetchone()
        assert app_module.run_autofill(db, row, '2026-10-28', 'Автозаполнение') == \
            ('error', 'Jira не вернула значений ни по одному эпику')


def test_history_job_internal_error_is_shown(admin, flask_app, fake, monkeypatch):
    monkeypatch.setitem(flask_app.config, 'HISTORY_SYNC', True)
    bid = make_button(admin, fake)
    monkeypatch.setattr(jira, 'compute_history', lambda *a, **k: (_ for _ in ()).throw(RuntimeError('сбой')))
    r = restore(admin, bid)
    job = admin.get(f"/api/jira/history-jobs/{r.get_json()['id']}").get_json()
    assert job['status'] == 'error' and 'сбой' in job['message']


def test_next_run_edge_cases():
    base = {'enabled': True, 'days': '1,2,3,4,5', 'timezone': 'Europe/Moscow', 'run_time': '09:00', 'last_run_date': None}
    monday_8 = datetime(2026, 11, 2, 5, 0, tzinfo=timezone.utc)
    assert app_module._next_run(base, monday_8) == '2026-11-02T09:00'
    assert app_module._next_run(dict(base, last_run_date='2026-11-02'), monday_8) == '2026-11-03T09:00'
    assert app_module._next_run(dict(base, days=''), monday_8) is None
    assert app_module._next_run(dict(base, timezone='Нет/Такого'), monday_8) is None
    assert app_module._next_run(dict(base, enabled=False), monday_8) is None


# ─── Почта и Telegram: ошибки доставки ────────────────────────────

def test_send_email_errors():
    cfg = dict(SMTP)
    with pytest.raises(notify.NotifyError, match='не настроена'):
        notify.send_email({}, 'a@b.ru', 's', 't')
    with pytest.raises(notify.NotifyError, match='не указан email'):
        notify.send_email(cfg, '', 's', 't')

    class Refusing(FakeSMTP):
        def send_message(self, msg):
            raise smtplib.SMTPRecipientsRefused({'x@y.ru': (550, b'no')})

    class Broken(FakeSMTP):
        def send_message(self, msg):
            raise smtplib.SMTPServerDisconnected('gone')
    for cls, text in ((Refusing, 'отклонил адрес получателя'), (Broken, 'Не удалось отправить письмо')):
        real = notify.smtplib.SMTP
        notify.smtplib.SMTP = cls
        try:
            with pytest.raises(notify.NotifyError, match=text):
                notify.send_email(cfg, 'x@y.ru', 's', 't', html_body='<b>t</b>',
                                  attachments=[('r.pdf', b'%PDF', 'application/pdf')])
        finally:
            notify.smtplib.SMTP = real
    FakeSMTP.reset()
    notify.send_email(dict(cfg, security='ssl', port=None), 'x@y.ru', 's', 't')  # SSL, порт по умолчанию 465
    assert FakeSMTP.connections[-1].port == 465 and not FakeSMTP.connections[-1].tls


def test_telegram_client_errors(monkeypatch):
    with pytest.raises(notify.NotifyError, match='не настроен'):
        notify.Telegram('')
    monkeypatch.setattr(notify, 'requests', None)
    with pytest.raises(notify.NotifyError, match='requests'):
        notify.Telegram('1:A')


def test_telegram_transport_errors(monkeypatch):
    server = FakeTelegram()
    try:
        bot = notify.Telegram('123:ABC', server.url)
        assert bot.find_start('нет-такого') is None
        bot.send_message('42', 'строка\n' * 1000)  # длинное сообщение обрезается по строкам
        assert server.state['messages'][-1]['text'].endswith('…') and len(server.state['messages'][-1]['text']) <= 4002
        with pytest.raises(notify.NotifyError, match='blocked'):
            bot.send_message('blocked', 'x')
        with pytest.raises(notify.NotifyError, match='отклонил токен'):
            notify.Telegram('wrong', server.url).bot_name()
    finally:
        server.stop()
    with pytest.raises(notify.NotifyError, match='недоступен'):
        notify.Telegram('1:A', 'http://127.0.0.1:9').bot_name()

    class Html:
        status_code = 502

        def json(self):
            raise ValueError
    monkeypatch.setattr(notify.requests, 'post', lambda *a, **k: Html())
    with pytest.raises(notify.NotifyError, match='не JSON'):
        notify.Telegram('1:A').bot_name()


def test_deliver_reports_each_channel(admin, flask_app, mail_ready, monkeypatch):
    subscribe(admin, digest={'enabled': True, 'days': [1], 'time': '09:00', 'timezone': 'Europe/Moscow', 'pdf': False})
    with flask_app.app_context():
        db = app_module.get_db()
        db.execute('UPDATE notification_subscriptions SET via_telegram = 1')
        db.commit()
        sub = db.execute('SELECT * FROM notification_subscriptions').fetchone()
        user = db.execute('SELECT * FROM users WHERE id = 1').fetchone()
        project = db.execute('SELECT * FROM projects WHERE id = 1').fetchone()
        monkeypatch.setattr(notify, 'send_email', lambda *a, **k: (_ for _ in ()).throw(notify.NotifyError('почта упала')))
        results = app_module.deliver(db, user, sub, project, 'digest', 's', 't', '<b>t</b>', 't')
    assert results == [{'channel': 'email', 'status': 'error', 'message': 'почта упала'},
                       {'channel': 'telegram', 'status': 'error', 'message': 'Telegram не привязан'}]


def test_digest_lists_autofill_errors(flask_app):
    with flask_app.app_context():
        ov = app_module.project_overview(app_module.get_db(), 1)
    ov['autofill'] = [{'button': 'Срез', 'lastStatus': 'error', 'lastMessage': 'Jira не отвечает', 'lastRunAt': None}]
    msg = app_module.render_digest(ov, None, None)
    assert 'Автозаполнение с ошибкой' in msg['text'] and 'Jira не отвечает' in msg['html']
    assert 'Автозаполнение с ошибкой' in msg['telegram']


def test_due_digests_skip_bad_timezone_revoked_access_and_failures(admin, flask_app, make_user, mail_ready, monkeypatch):
    user = make_user('petrova', project_id=1)
    admin.put(f'/api/users/{user.user_id}', json={'email': 'petrova@example.ru'})
    subscribe(user, digest={'enabled': True, 'days': [1, 2, 3, 4, 5, 6, 7], 'time': '00:00', 'timezone': 'UTC', 'pdf': False})
    subscribe(admin, digest={'enabled': True, 'days': [1, 2, 3, 4, 5, 6, 7], 'time': '00:00', 'timezone': 'UTC', 'pdf': False})
    with flask_app.app_context():
        db = app_module.get_db()
        db.execute("UPDATE notification_subscriptions SET last_digest_date = NULL")
        db.execute("UPDATE notification_subscriptions SET timezone = 'Нет/Такого' WHERE user_id = 1")
        db.commit()
    admin.delete(f'/api/users/{user.user_id}/projects/1')  # доступ отозван
    with flask_app.app_context():
        assert app_module.run_due_digests(MONDAY_0930_MSK) == 0
        db = app_module.get_db()
        db.execute("UPDATE notification_subscriptions SET timezone = 'UTC', last_digest_date = NULL WHERE user_id = 1")
        db.commit()
        monkeypatch.setattr(app_module, 'send_digest', lambda *a, **k: (_ for _ in ()).throw(RuntimeError('сбой')))
        assert app_module.run_due_digests(MONDAY_0930_MSK) == 1  # сбой одного дайджеста не роняет планировщик


# ─── Ключ, история версий, руководства, проверка ввода ────────────

def test_secret_key_file_created_by_other_worker(tmp_path, monkeypatch):
    monkeypatch.delenv('SECRET_KEY', raising=False)
    monkeypatch.setattr(app_module, 'DB_PATH', str(tmp_path / 'x.db'))
    (tmp_path / '.secret_key').write_text('')  # файл есть, но пуст — генерируем

    def racing_open(path, flags, mode=0o777):
        (tmp_path / '.secret_key').write_text('other-worker-key')
        raise FileExistsError
    monkeypatch.setattr(app_module.os, 'open', racing_open)
    assert app_module.load_secret_key() == 'other-worker-key'


def test_rekey_skips_undecryptable(db_path, monkeypatch):
    import sqlite3
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO app_settings (key, value) VALUES ('telegram_token', 'мусор')")
    conn.commit()
    monkeypatch.setenv('SECRET_KEY', 'short')
    app_module.rekey_secrets(conn)  # не расшифровывается ни новым, ни старым ключом — оставляем как есть
    assert conn.execute("SELECT value FROM app_settings WHERE key = 'telegram_token'").fetchone()[0] == 'мусор'
    conn.close()


def test_releases_and_manuals_missing(tmp_path, monkeypatch, admin):
    monkeypatch.setattr(app_module, 'APP_DIR', str(tmp_path))
    assert app_module._load_releases() == []
    monkeypatch.setattr(app_module, 'MANUALS_DIR', str(tmp_path))
    r = admin.get('/manuals/user-guide.pdf')
    assert r.status_code == 404 and 'не найден' in r.get_data(as_text=True)
    assert admin.get('/api/about').get_json()['manuals'] == []


def test_request_limits_and_helpers(admin, flask_app):
    r = admin.post('/api/projects/1/services', data=b'{"name": "' + b'x' * (5 * 1024 * 1024) + b'"}',
                   content_type='application/json')
    assert r.status_code == 413 and r.get_json()['error'] == 'Слишком большой запрос'
    with flask_app.test_request_context('/', method='POST', data=b'', content_type='application/json'):
        assert app_module.json_body() == {}
    with pytest.raises(app_module.ValidationError, match='от 0 до 5'):
        app_module.int_value(9, 'Число', 0, 5)
    assert app_module.stage_key({'etap': 'Этап 3.2'}) == 'Этап 3'  # объект без stage_group (старые записи)


@pytest.mark.parametrize('method, url, body, code', [
    ('put', '/api/projects/999', {'name': 'x'}, 404),
    ('put', '/api/services/999', {'name': 'x'}, 404),
    ('delete', '/api/entries/999', None, 404),
    ('put', '/api/users/999', {'username': 'x'}, 404),
    ('put', '/api/jira/buttons/999', {'name': 'x'}, 404),
    ('delete', '/api/jira/buttons/999', None, 404),
    ('post', '/api/jira/buttons/999/schedule/run', {}, 404),
    ('get', '/api/services/999/jira/changes', None, 404),
    ('get', '/api/projects/999/pdf', None, 404),
    ('post', '/api/users/2/projects', {'projectId': None}, 400),
    ('post', '/api/admin/notify-settings/test-email', {'to': 'не почта'}, 400),
    ('put', '/api/admin/notify-settings', {'smtp': {'host': 'smtp.x.ru', 'port': 'abc', 'sender': 'a@b.ru'}}, 400),
    ('put', '/api/users/1', {'password': 123}, 400),
    ('put', '/api/admin/jira-connection', {'baseUrl': 'https://jira.x.ru', 'instanceType': 'server', 'token': 5}, 400),
])
def test_not_found_and_validation_branches(admin, method, url, body, code):
    r = getattr(admin, method)(url, json=body) if body is not None else getattr(admin, method)(url)
    assert r.status_code == code, (url, r.get_data(as_text=True)[:200])


def test_viewer_cannot_use_editor_endpoints(admin, make_user, fake):
    connect(admin, fake)
    bid = create_button(admin).get_json()['id']
    viewer = make_user('viewer', project_id=1)
    for url in ('/api/projects/1/jira/structures', '/api/projects/1/jira/fields'):
        assert viewer.get(url).status_code == 403
    assert viewer.post('/api/projects/1/jira/buttons', json={'name': 'x'}).status_code == 403
    assert viewer.get('/api/projects/2/notifications/preview').status_code == 403
    assert viewer.post('/api/projects/2/notifications/test').status_code == 403
    assert viewer.put('/api/projects/2/notifications', json={}).status_code == 403
    assert viewer.get('/api/projects/2/export').status_code == 403
    assert viewer.get(f'/api/services/1/jira/changes').status_code in (200, 404)
    assert admin.get('/api/projects/1/jira/fields').status_code == 200
    fake.configure(token='other')  # Jira перестала принимать токен
    assert admin.get('/api/projects/1/jira/fields').status_code == 502


def test_subscription_edge_branches(admin, mail_ready):
    assert admin.post('/api/projects/2/notifications/test').status_code == 400  # подписки ещё нет
    r = admin.put('/api/projects/1/notifications', json={'viaEmail': True, 'alerts': {'enabled': True, 'types': []}})
    assert r.status_code == 400 and 'вид оповещений' in r.get_json()['error']
    r = admin.put('/api/projects/1/notifications', json={'viaEmail': True, 'alerts': {'chanceThreshold': 'abc'}})
    assert r.status_code == 400
    r = admin.put('/api/projects/1/notifications', json={'viaTelegram': True, 'digest': {'enabled': True, 'days': [1]}})
    assert r.status_code == 400 and 'Telegram' in r.get_json()['error']
    subscribe(admin)
    subscribe(admin, digest={'enabled': True, 'days': [2], 'time': '10:00', 'timezone': 'UTC', 'pdf': False})  # обновление
    assert admin.get('/api/projects/1/notifications').get_json()['digest']['days'] == [2]


def test_telegram_link_errors(admin, flask_app, monkeypatch):
    assert admin.post('/api/profile/telegram/link').status_code == 400  # бот не подключён
    assert admin.post('/api/profile/telegram/check').status_code == 400  # ссылки ещё нет
    with flask_app.app_context():
        db = app_module.get_db()
        app_module._set_setting(db, 'telegram_token', jira.encrypt_token(app_module.app.secret_key, '1:A'))
        db.commit()
    monkeypatch.setitem(flask_app.config, 'TELEGRAM_API', 'http://127.0.0.1:9')
    r = admin.post('/api/profile/telegram/link')
    assert r.status_code == 502 and 'недоступен' in r.get_json()['error']
    with flask_app.app_context():
        db = app_module.get_db()
        db.execute("UPDATE users SET telegram_link_code = 'abc' WHERE id = 1")
        db.commit()
    assert admin.post('/api/profile/telegram/check').status_code == 502


def test_forecast_helpers_edge_cases():
    s = {'forecastMethod': 'linear', 'riskBufferDays': 0, 'burnrateMethod': 'all', 'burnrateWindowDays': 14}
    assert forecast.avg_burn_rate(['2026-09-01', '2026-09-01'], [10, 5], s) is None  # одна и та же дата
    assert forecast.linear_forecast([(3, 10), (3, 5)]) is None  # вертикальная прямая
    assert forecast.compute_status([], [], None, s)['status'] == 'unknown'
    assert forecast.settings_dict(None)['forecastMethod'] == 'linear'
    mc = forecast.monte_carlo(['2026-09-01', '2026-09-08', '2026-09-15'], [100, 120, 140], '2026-10-01', s)
    assert mc['p50'] is None and mc['probability'] == 0  # остаток растёт — не сгорает
