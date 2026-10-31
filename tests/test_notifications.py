"""Уведомления: настройки отправки, привязка Telegram, подписки, дайджест, оповещения и очередь."""

import re
from datetime import datetime, timezone

import pytest

import app as app_module
import forecast
import notify
from fake_notify import FakeSMTP, FakeTelegram

SMTP = {'host': 'smtp.example.ru', 'port': 587, 'security': 'starttls', 'login': 'burndown', 'password': 'smtp-pass',
        'sender': 'burndown@example.ru'}
MONDAY_0930_MSK = datetime(2026, 11, 2, 6, 30, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def fake_smtp(monkeypatch):
    FakeSMTP.reset()
    monkeypatch.setattr(notify.smtplib, 'SMTP', FakeSMTP)
    monkeypatch.setattr(notify.smtplib, 'SMTP_SSL', FakeSMTP)
    return FakeSMTP


@pytest.fixture
def telegram(flask_app, monkeypatch):
    server = FakeTelegram()
    monkeypatch.setitem(flask_app.config, 'TELEGRAM_API', server.url)
    yield server
    server.stop()


@pytest.fixture
def mail_ready(admin):
    assert admin.put('/api/admin/notify-settings', json={'smtp': SMTP, 'publicUrl': 'https://burndown.example.ru/'}).status_code == 200
    admin.put('/api/profile', json={'displayName': 'Администратор', 'email': 'admin@example.ru', 'position': 'Админ'})


def subscribe(client, pid=1, **over):
    body = {'viaEmail': True, 'viaTelegram': False,
            'digest': {'enabled': True, 'days': [1], 'time': '09:00', 'timezone': 'Europe/Moscow', 'pdf': False},
            'alerts': {'enabled': True, 'types': list(app_module.ALERT_TYPES), 'chanceThreshold': 50, 'scopeThreshold': 10}}
    body.update(over)
    return client.put(f'/api/projects/{pid}/notifications', json=body)


def run(flask_app, fn, *args):
    with flask_app.app_context():
        return fn(*args)


# ─── Настройки отправки (администратор) ──────────────────────────

def test_notify_settings_roundtrip_hides_secrets(admin, flask_app):
    r = admin.put('/api/admin/notify-settings', json={'smtp': SMTP, 'publicUrl': 'https://burndown.example.ru/'})
    data = r.get_json()
    assert r.status_code == 200 and data['smtp']['hasPassword'] and 'password' not in data['smtp']
    assert data['smtp']['host'] == 'smtp.example.ru' and data['publicUrl'] == 'https://burndown.example.ru'
    with flask_app.app_context():
        stored = dict(app_module.get_db().execute("SELECT key, value FROM app_settings").fetchall())
    assert 'smtp-pass' not in stored['smtp_password']  # хранится зашифрованным
    # пустой пароль — прежний сохраняется; clearPassword — удаляет
    admin.put('/api/admin/notify-settings', json={'smtp': dict(SMTP, password='')})
    assert admin.get('/api/admin/notify-settings').get_json()['smtp']['hasPassword']
    admin.put('/api/admin/notify-settings', json={'smtp': dict(SMTP, password='', clearPassword=True)})
    assert not admin.get('/api/admin/notify-settings').get_json()['smtp']['hasPassword']
    audit = admin.get('/api/audit?page=integrations').get_json()['entries']
    assert audit and all('smtp-pass' not in (a['newValue'] or '') for a in audit)


@pytest.mark.parametrize('over, message', [
    ({'smtp': dict(SMTP, security='tls13')}, 'Защита'),
    ({'smtp': dict(SMTP, port=70000)}, 'Порт'),
    ({'smtp': dict(SMTP, sender='не адрес')}, 'отправителя'),
    ({'smtp': SMTP, 'publicUrl': 'burndown.local'}, 'http'),
], ids=['security', 'port', 'sender', 'url'])
def test_notify_settings_validation(admin, over, message):
    r = admin.put('/api/admin/notify-settings', json=over)
    assert r.status_code == 400 and message in r.get_json()['error']


def test_notify_settings_admin_only(make_user):
    manager = make_user('manager1', project_id=1, project_role='manager')
    assert manager.get('/api/admin/notify-settings').status_code == 403
    assert manager.put('/api/admin/notify-settings', json={}).status_code == 403
    assert manager.get('/api/admin/notify-log').status_code == 403


def test_test_email_and_log(admin, fake_smtp, mail_ready):
    assert admin.post('/api/admin/notify-settings/test-email', json={'to': 'ops@example.ru'}).status_code == 200
    msg = fake_smtp.sent[-1]
    assert msg['To'] == 'ops@example.ru' and 'тестовое' in msg['Subject']
    conn = fake_smtp.connections[-1]
    assert (conn.host, conn.port, conn.tls, conn.user) == ('smtp.example.ru', 587, True, 'burndown')
    fake_smtp.fail = 'auth'
    r = admin.post('/api/admin/notify-settings/test-email', json={'to': 'ops@example.ru'})
    assert r.status_code == 502 and 'логин или пароль' in r.get_json()['error']
    log = admin.get('/api/admin/notify-log').get_json()
    assert [(e['channel'], e['status']) for e in log[:2]] == [('email', 'error'), ('email', 'ok')]


# ─── Telegram ────────────────────────────────────────────────────

def test_telegram_bot_and_linking(admin, telegram, make_user):
    r = admin.put('/api/admin/notify-settings', json={'telegram': {'token': '123:ABC'}})
    assert r.get_json()['telegram'] == {'hasToken': True, 'botName': 'burndown_test_bot'}
    assert admin.post('/api/admin/notify-settings/test-telegram').get_json() == {'botName': 'burndown_test_bot'}
    user = make_user('petrova', project_id=1)
    link = user.post('/api/profile/telegram/link').get_json()
    assert link['url'] == f"https://t.me/burndown_test_bot?start={link['code']}"
    assert user.post('/api/profile/telegram/check').get_json() == {'linked': False}  # ещё не нажал Start
    telegram.user_starts('чужой-код', chat_id=1)
    telegram.user_starts(link['code'], chat_id=777)
    assert user.post('/api/profile/telegram/check').get_json() == {'linked': True}
    assert telegram.state['messages'][-1]['chat_id'] == '777'
    assert user.get('/api/projects/1/notifications').get_json()['channels']['telegramLinked']
    assert user.delete('/api/profile/telegram').get_json() == {'linked': False}
    assert not user.get('/api/projects/1/notifications').get_json()['channels']['telegramLinked']


def test_telegram_wrong_token(admin, telegram):
    admin.put('/api/admin/notify-settings', json={'telegram': {'token': 'wrong'}})
    assert admin.get('/api/admin/notify-settings').get_json()['telegram'] == {'hasToken': True, 'botName': ''}
    r = admin.post('/api/admin/notify-settings/test-telegram')
    assert r.status_code == 502 and 'токен' in r.get_json()['error']


# ─── Подписка ────────────────────────────────────────────────────

def test_subscription_requires_ready_channel(admin, make_user, mail_ready):
    r = subscribe(admin, viaEmail=False)
    assert r.status_code == 400 and 'канал' in r.get_json()['error']
    r = subscribe(admin, viaEmail=False, viaTelegram=True)
    assert r.status_code == 400 and 'Telegram' in r.get_json()['error']
    viewer = make_user('viewer1', project_id=1)  # email не указан
    r = subscribe(viewer)
    assert r.status_code == 400 and 'email' in r.get_json()['error']
    assert subscribe(viewer, digest={'enabled': False}, alerts={'enabled': False}).status_code == 200
    stranger = make_user('stranger')
    assert stranger.get('/api/projects/1/notifications').status_code == 403


@pytest.mark.parametrize('over, message', [
    ({'digest': {'enabled': True, 'days': [8], 'time': '09:00'}}, 'Дни недели'),
    ({'digest': {'enabled': True, 'days': [], 'time': '09:00'}}, 'день'),
    ({'digest': {'enabled': True, 'days': [1], 'time': '9:00'}}, 'ЧЧ:ММ'),
    ({'alerts': {'enabled': True, 'types': ['weather']}}, 'вид'),
    ({'alerts': {'enabled': True, 'types': ['status'], 'chanceThreshold': 0}}, 'Порог'),
], ids=['day', 'no-days', 'time', 'type', 'threshold'])
def test_subscription_validation(admin, mail_ready, over, message):
    r = subscribe(admin, **over)
    assert r.status_code == 400 and message in r.get_json()['error']


def test_subscription_roundtrip(admin, mail_ready):
    data = subscribe(admin).get_json()
    assert data['digest']['enabled'] and data['digest']['days'] == [1] and data['digest']['nextAt'].endswith('T09:00')
    assert data['alerts']['types'] == list(app_module.ALERT_TYPES) and data['channels']['emailReady']
    assert admin.get('/api/projects/1/notifications').get_json() == data


# ─── Дайджест ────────────────────────────────────────────────────

def test_digest_preview_matches_forecast(admin, mail_ready):
    data = admin.get('/api/projects/1/notifications/preview').get_json()
    ov = data['overview']
    epic = next(e for e in ov['epics'] if e['id'] == 1)
    rows = admin.get('/api/projects/1/data').get_json()['entries']
    rows = sorted((e for e in rows if e['service_id'] == 1), key=lambda e: e['date'])
    expected = forecast.compute_status([e['date'] for e in rows], [e['remaining'] for e in rows], '2026-08-31',
                                       forecast.settings_dict(None))
    assert epic['status'] == expected['status'] and epic['probability'] == expected['mc']['probability']
    assert ov['kpi']['epics'] == 6 and data['subject'].startswith('Burndown · АИСУЗ: ')
    assert 'Работа с жалобами' in data['html'] and 'Открыть Burndown' in data['html']


def test_digest_test_send_with_pdf_and_telegram(admin, telegram, fake_smtp, mail_ready):
    admin.put('/api/admin/notify-settings', json={'smtp': dict(SMTP, password=''), 'telegram': {'token': '123:ABC'},
                                                  'publicUrl': 'https://burndown.example.ru'})
    link = admin.post('/api/profile/telegram/link').get_json()
    telegram.user_starts(link['code'], chat_id=42)
    admin.post('/api/profile/telegram/check')
    subscribe(admin, viaTelegram=True, digest={'enabled': True, 'days': [1], 'time': '09:00', 'pdf': True})
    r = admin.post('/api/projects/1/notifications/test')
    assert r.status_code == 200 and [x['status'] for x in r.get_json()['results']] == ['ok', 'ok']
    msg = fake_smtp.sent[-1]
    assert msg['To'] == 'admin@example.ru' and msg['Subject'].startswith('Burndown · АИСУЗ')
    names = [a.get_filename() for a in msg.iter_attachments()]
    assert len(names) == 1 and names[0].endswith('.pdf')
    assert 'Сервис' in msg.get_body(('html',)).get_content()
    assert telegram.state['messages'][-1]['chat_id'] == '42' and 'АИСУЗ' in telegram.state['messages'][-1]['text']
    assert telegram.state['documents'][-1]['name'].endswith('.pdf')
    sub = admin.get('/api/projects/1/notifications').get_json()['digest']
    assert sub['lastStatus'] == 'ok' and 'почта: отправлено; Telegram: отправлено' == sub['lastMessage']


def test_digest_schedule_once_per_day(admin, flask_app, fake_smtp, mail_ready):
    subscribe(admin)
    assert run(flask_app, app_module.run_due_digests, datetime(2026, 11, 2, 5, 59, tzinfo=timezone.utc)) == 0
    assert run(flask_app, app_module.run_due_digests, MONDAY_0930_MSK) == 1
    assert run(flask_app, app_module.run_due_digests, MONDAY_0930_MSK) == 0
    assert run(flask_app, app_module.run_due_digests, datetime(2026, 11, 3, 6, 30, tzinfo=timezone.utc)) == 0  # вторник
    assert len(fake_smtp.sent) == 1
    # второй дайджест показывает изменения с прошлого
    admin.post('/api/projects/1/measurements', json={'date': '2026-08-03', 'items': [
        {'serviceId': 1, 'remaining': 300, 'spent': 300, 'estimate': 420}]})
    assert run(flask_app, app_module.run_due_digests, datetime(2026, 11, 9, 6, 30, tzinfo=timezone.utc)) == 1
    assert 'остаток -46\xa0ч' in fake_smtp.sent[-1].get_body(('plain',)).get_content()


def test_digest_skipped_without_access(admin, flask_app, fake_smtp, make_user, mail_ready):
    user = make_user('ivanov', project_id=1, project_role='manager')
    admin.put(f'/api/users/{user.user_id}', json={'email': 'ivanov@example.ru', 'role': 'user'})
    assert subscribe(user).status_code == 200
    admin.delete(f'/api/users/{user.user_id}/projects/1')
    assert run(flask_app, app_module.run_due_digests, MONDAY_0930_MSK) == 0 and not fake_smtp.sent


# ─── Оповещения ──────────────────────────────────────────────────

def test_alerts_on_status_and_chance(admin, flask_app, fake_smtp, mail_ready):
    subscribe(admin, digest={'enabled': False})
    assert run(flask_app, app_module.run_alert_checks) == 0      # первая проверка — только запоминает состояние
    assert run(flask_app, app_module.run_alert_checks) == 0      # без изменений проект не пересчитывается
    admin.put('/api/services/1', json={'etap': 'Этап 4', 'name': 'Сервис "Работа с жалобами"', 'targetDate': '2026-08-01'})
    assert run(flask_app, app_module.run_alert_checks) == 1
    assert run(flask_app, app_module.deliver_outbox) == 1
    text = fake_smtp.sent[-1].get_body(('plain',)).get_content()
    assert 'Эпик «Сервис "Работа с жалобами"»: Успеваем → Не успеваем' in text
    assert re.search(r'шанс успеть \d+% → 0% \(порог 50%\)', text)
    assert run(flask_app, app_module.deliver_outbox) == 0


def test_alert_events_rules():
    sub = {'alert_types': 'status,chance,scope,problems', 'chance_threshold': 50, 'scope_threshold': 10}
    old = {'epic:1': {'name': 'А', 'status': 'risk', 'probability': 0.6, 'scope': 100, 'problems': 1, 'kind': 'epic'},
           'stage:Этап 1': {'name': 'Этап 1', 'status': 'ok', 'probability': 0.9, 'scope': None, 'problems': None,
                            'kind': 'stage'}}
    new = {'epic:1': {'name': 'А', 'status': 'ok', 'probability': 0.4, 'scope': 115, 'problems': 3, 'kind': 'epic'},
           'stage:Этап 1': {'name': 'Этап 1', 'status': 'ok', 'probability': 0.9, 'scope': None, 'problems': None,
                            'kind': 'stage'},
           'epic:2': {'name': 'Новый', 'status': 'danger', 'probability': 0, 'scope': None, 'problems': 5, 'kind': 'epic'}}
    kinds = [k for k, _ in app_module.alert_events(old, new, sub)]
    assert kinds == ['chance', 'scope', 'problems']  # статус улучшился; новый эпик — без сравнения
    assert app_module.alert_events(old, new, dict(sub, alert_types='status')) == []
    assert app_module.alert_events(old, new, dict(sub, scope_threshold=20, alert_types='scope')) == []


def test_autofill_error_queues_alert(admin, flask_app, fake_smtp, mail_ready):
    from fake_jira import FakeJira
    from test_jira import connect, create_button
    jira_server = FakeJira()
    try:
        connect(admin, jira_server)
        bid = create_button(admin).get_json()['id']
        admin.post(f'/api/jira/buttons/{bid}/snapshot')
        subscribe(admin, digest={'enabled': False}, alerts={'enabled': True, 'types': ['autofill']})
        jira_server.configure(token='rotated')
        assert admin.post(f'/api/jira/buttons/{bid}/schedule/run').status_code == 502
    finally:
        jira_server.stop()
    assert run(flask_app, app_module.deliver_outbox) == 1
    assert 'Автозаполнение «Срез из релиза» не выполнено' in fake_smtp.sent[-1].get_body(('plain',)).get_content()


def test_subscriptions_removed_with_user(admin, flask_app, make_user, mail_ready):
    user = make_user('ivanov', project_id=1)
    admin.put(f'/api/users/{user.user_id}', json={'email': 'ivanov@example.ru', 'role': 'user'})
    subscribe(user)
    admin.delete(f'/api/users/{user.user_id}')
    with flask_app.app_context():
        assert app_module.get_db().execute('SELECT COUNT(*) FROM notification_subscriptions').fetchone()[0] == 0


def test_percent_rounding_matches_interface():
    assert app_module._pct(0.905) == '91%' and app_module._pct(0.125) == '13%' and app_module._pct(None) == '—'
    assert app_module._ru_num(1188.3) == '1 188,3' and app_module._ru_num(-46.0) == '-46'
