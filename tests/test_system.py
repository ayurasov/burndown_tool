"""Производственный календарь организации, состояние системы и оповещения администратору."""

import json
import sqlite3
from datetime import date, datetime, timedelta, timezone

import pytest

import app as app_module
import forecast as fc
import holidays_ru
from fake_jira import FakeJira
from test_jira import connect, create_button
from test_notifications import SMTP, fake_smtp, mail_ready  # noqa: F401 — фикстуры уведомлений


@pytest.fixture
def fake():
    server = FakeJira()
    yield server
    server.stop()


def health(admin):
    r = admin.get('/api/admin/health')
    assert r.status_code == 200
    data = r.get_json()
    return data, {i['key']: i for i in data['items']}


# ─── Производственный календарь ──────────────────────────────────

@pytest.mark.parametrize('year', sorted(holidays_ru.PRESETS))
def test_ru_presets_give_official_number_of_working_days(year):
    """В 2025–2027 годах по 247 рабочих дней при пятидневке — как в официальном производственном календаре."""
    preset = holidays_ru.PRESETS[year]
    cal = fc.calendar_for({'workdays': True, 'orgHolidays': preset['holidays'], 'orgExtraWorkdays': preset['extraWorkdays']})
    assert cal.span(f'{year - 1}-12-31', f'{year}-12-31') == 247
    for d in preset['holidays']:
        assert date.fromisoformat(d).isoweekday() <= 5, d  # в наборе — только будни
    for d in preset['extraWorkdays']:
        assert date.fromisoformat(d).isoweekday() >= 6, d


def test_org_calendar_defaults_api_and_merge(admin, make_user):
    data = admin.get('/api/admin/calendar').get_json()
    holidays, extra = holidays_ru.all_dates()
    assert data['holidays'] == holidays and data['extraWorkdays'] == extra and [p['year'] for p in data['presets']] == [2025, 2026, 2027]
    r = admin.put('/api/admin/calendar', json={'holidays': ['2026-11-05', '2026-11-04', '2026-11-04'], 'extraWorkdays': []})
    assert r.status_code == 200 and r.get_json()['holidays'] == ['2026-11-04', '2026-11-05']
    st = admin.get('/api/projects/1/data').get_json()['settings']
    assert st['orgHolidays'] == ['2026-11-04', '2026-11-05'] and st['orgExtraWorkdays'] == [] and st['holidays'] == []
    audit = admin.get('/api/audit', query_string={'page': 'calendar'}).get_json()['entries']
    assert {a['fieldName'] for a in audit} == {'holidays', 'extraWorkdays'}
    assert admin.put('/api/admin/calendar', json={'holidays': ['01.01.2026']}).status_code == 400
    assert admin.put('/api/admin/calendar', json={'holidays': '2026-01-01'}).status_code == 400
    user = make_user('ivanov', project_id=1, project_role='manager')
    assert user.get('/api/admin/calendar').status_code == 403
    assert user.put('/api/admin/calendar', json={}).status_code == 403


def test_org_calendar_used_in_forecast(admin, flask_app):
    # пн 02.11 — 50 ч, пт 06.11 — 30 ч; ср 04.11 — праздник общего календаря, 05.11 — проекта
    sid = admin.post('/api/projects/2/services', json={'name': 'Э', 'targetDate': '2026-11-30'}).get_json()['id']
    for d, rem in (('2026-11-02', 50), ('2026-11-06', 30)):
        admin.post(f'/api/services/{sid}/entries', json={'date': d, 'remaining': rem})
    admin.put('/api/admin/calendar', json={'holidays': ['2026-11-04'], 'extraWorkdays': []})
    admin.put('/api/projects/2/settings', json={'workdays': True, 'forecastMethod': 'average', 'holidays': ['2026-11-05']})
    with flask_app.app_context():
        settings = app_module.project_settings(app_module.get_db(), 2)
    cal = fc.calendar_for(settings)
    assert cal.span('2026-11-02', '2026-11-06') == 2  # вт и пт: ср — общий праздник, чт — праздник проекта
    st = fc.compute_status(['2026-11-02', '2026-11-06'], [50, 30], '2026-11-30', settings)
    assert st['burnRate'] == 10 and st['forecastZero'] == '2026-11-11'  # 30 ч по 10 ч/раб. день: пн 09 … ср 11


def test_broken_org_calendar_is_ignored(flask_app):
    with flask_app.app_context():
        db = app_module.get_db()
        for raw in ('не json', '[1]', '{"holidays": [5, "2026-01-01", "плохо"]}'):
            db.execute("UPDATE app_settings SET value = ? WHERE key = 'org_calendar'", (raw,))
            cal = app_module.org_calendar(db)
            assert cal['extraWorkdays'] == [] and cal['holidays'] in ([], ['2026-01-01'])


def test_migration_fills_org_calendar_once(tmp_path):
    from init_db import ensure_org_calendar
    conn = sqlite3.connect(str(tmp_path / 'x.db'))
    conn.execute('CREATE TABLE app_settings (key TEXT PRIMARY KEY, value TEXT)')
    ensure_org_calendar(conn)
    stored = json.loads(conn.execute("SELECT value FROM app_settings WHERE key = 'org_calendar'").fetchone()[0])
    assert stored['holidays'] == holidays_ru.all_dates()[0]
    conn.execute("UPDATE app_settings SET value = '{\"holidays\": []}' WHERE key = 'org_calendar'")
    ensure_org_calendar(conn)  # очищенный администратором календарь не заполняется снова
    assert conn.execute("SELECT value FROM app_settings WHERE key = 'org_calendar'").fetchone()[0] == '{"holidays": []}'


# ─── Состояние системы ───────────────────────────────────────────

def test_health_overview_and_rights(admin, make_user):
    data, items = health(admin)
    assert list(items) == ['app', 'scheduler', 'jira', 'autofill', 'history', 'email', 'telegram', 'outbox', 'data']
    assert items['app']['status'] == 'ok' and items['app']['summary'].startswith(f'Версия {app_module.APP_VERSION}')
    assert items['scheduler']['status'] == 'off' and items['jira']['status'] == 'off'
    assert items['data']['status'] == 'warn' and 'АИСУЗ' in items['data']['details'][0]  # демо-замеры старше 10 дней
    assert data['status'] == 'warn' and data['alerts'] == {'enabled': True, 'email': True, 'telegram': True}
    user = make_user('ivanov', project_id=1, project_role='manager')
    for method, url in (('get', '/api/admin/health'), ('put', '/api/admin/health/alerts'), ('post', '/api/admin/health/check-jira'),
                        ('post', '/api/admin/health/test-alert')):
        assert getattr(user, method)(url, json={}).status_code == 403


def test_health_scheduler_disk_and_queue(admin, flask_app, monkeypatch):
    monkeypatch.setenv('BURNDOWN_SCHEDULER', '1')
    monkeypatch.setattr(app_module, 'APP_STARTED', datetime.now(timezone.utc) - timedelta(hours=1))
    assert health(admin)[1]['scheduler']['status'] == 'error'  # отметок нет, служба работает давно
    with flask_app.app_context():
        app_module.mark_scheduler_tick()
    assert health(admin)[1]['scheduler']['status'] == 'ok'
    monkeypatch.setattr(app_module, 'APP_STARTED', datetime.now(timezone.utc))
    with flask_app.app_context():
        db = app_module.get_db()
        db.execute("DELETE FROM app_settings WHERE key = 'scheduler_tick'")
        db.execute("INSERT INTO notification_outbox (user_id, project_id, kind, subject, status, created_at) "
                   "VALUES (1, 1, 'alert', 'x', 'pending', '2020-01-01 00:00:00')")
        db.commit()
    _data, items = health(admin)
    assert items['scheduler']['summary'] == 'Запускается' and items['outbox']['status'] == 'warn'

    class Usage:
        total, free = 100 * 1024 ** 3, 512 * 1024 ** 2
    monkeypatch.setattr(app_module.shutil, 'disk_usage', lambda path: Usage)
    assert health(admin)[1]['app']['status'] == 'warn'
    monkeypatch.setattr(app_module.shutil, 'disk_usage', lambda path: (_ for _ in ()).throw(OSError('нет доступа')))
    assert 'нет доступа' in ' '.join(health(admin)[1]['app']['details'])


def test_health_jira_autofill_history(admin, flask_app, fake, monkeypatch):
    connect(admin, fake)
    bid = create_button(admin).get_json()['id']
    assert health(admin)[1]['jira']['summary'] == 'Последнее успешное обращение никогда'
    assert admin.post(f'/api/jira/buttons/{bid}/snapshot', json={}).status_code == 200
    assert health(admin)[1]['jira']['status'] == 'ok'
    fake.configure(token='other')
    assert admin.post(f'/api/jira/buttons/{bid}/snapshot', json={}).status_code == 502
    jira_item = health(admin)[1]['jira']
    assert jira_item['status'] == 'error' and 'ошибка' in jira_item['summary']
    fake.configure(token='secret-pat')
    data, items = health(admin)
    assert admin.post('/api/admin/health/check-jira', json={}).get_json()['status'] in ('ok', 'warn')
    with flask_app.app_context():
        db = app_module.get_db()
        db.execute("INSERT INTO jira_schedules (button_id, enabled, days, run_time, timezone, last_status, last_message) "
                   "VALUES (?, 1, '1,2,3,4,5,6,7', '00:00', 'UTC', 'error', 'Jira не отвечает')", (bid,))
        db.execute("INSERT INTO jira_history_jobs (button_id, status, message, created_at, updated_at) "
                   "VALUES (?, 'error', 'сбой', ?, ?)", (bid, app_module._utc_now_iso(), app_module._utc_now_iso()))
        db.commit()
    _data, items = health(admin)
    assert items['autofill']['status'] == 'error' and 'Jira не отвечает' in items['autofill']['details'][0]
    assert items['history']['status'] == 'warn' and 'сбой' in items['history']['details'][0]
    with flask_app.app_context():
        db = app_module.get_db()
        db.execute("UPDATE jira_schedules SET last_status = 'ok', last_run_date = '2000-01-01'")
        db.commit()
    assert health(admin)[1]['autofill']['status'] == 'warn'  # сегодня не выполнено, хотя время прошло
    admin.delete('/api/admin/jira-connection')
    assert health(admin)[1]['jira']['status'] == 'error'  # кнопки есть, подключения нет


def test_health_mail_and_telegram(admin, flask_app, mail_ready):
    assert health(admin)[1]['email']['summary'] == 'Отправлено за неделю: 0'
    with flask_app.app_context():
        db = app_module.get_db()
        app_module.notify_log(db, None, None, 'digest', 'email', 'error', 'SMTP отклонил')
        db.commit()
    assert health(admin)[1]['email']['status'] == 'error'
    with flask_app.app_context():
        db = app_module.get_db()
        app_module.notify_log(db, None, None, 'digest', 'email', 'ok', 'отправлено')
        db.commit()
    item = health(admin)[1]['email']
    assert item['status'] == 'warn' and 'SMTP отклонил' in item['details'][0]


# ─── Оповещения администратору ───────────────────────────────────

def test_admin_alerts_on_transitions(admin, flask_app, fake, mail_ready, fake_smtp):
    connect(admin, fake)
    bid = create_button(admin).get_json()['id']
    admin.post(f'/api/jira/buttons/{bid}/snapshot', json={})
    with flask_app.app_context():
        assert app_module.run_admin_alerts() == 0  # ошибок нет — писать не о чем
    fake.configure(token='other')
    admin.post(f'/api/jira/buttons/{bid}/snapshot', json={})
    with flask_app.app_context():
        assert app_module.run_admin_alerts() == 1
        assert app_module.run_admin_alerts() == 0  # та же ошибка повторно не приходит
    sent = fake_smtp.sent[-1]
    assert sent['To'] == 'admin@example.ru' and 'проблема — Jira' in sent['Subject']
    assert 'Состояние системы' in sent.get_body(('plain',)).get_content()
    fake.configure(token='secret-pat')
    admin.post(f'/api/jira/buttons/{bid}/snapshot', json={})
    with flask_app.app_context():
        assert app_module.run_admin_alerts() == 1
    assert fake_smtp.sent[-1]['Subject'] == 'Burndown: неполадки устранены'
    log = admin.get('/api/admin/notify-log').get_json()
    entries = log['entries'] if isinstance(log, dict) else log
    assert any(e.get('kind') == 'admin' for e in entries)


def test_admin_alerts_settings_and_test_message(admin, flask_app, fake, mail_ready, fake_smtp):
    r = admin.put('/api/admin/health/alerts', json={'enabled': False, 'email': True, 'telegram': False})
    assert r.status_code == 200 and r.get_json()['alerts']['enabled'] is False
    assert admin.put('/api/admin/health/alerts', json={'enabled': 'да'}).status_code == 400
    connect(admin, fake, token='wrong')
    bid = create_button(admin).get_json()['id']
    admin.post(f'/api/jira/buttons/{bid}/snapshot', json={})
    with flask_app.app_context():
        assert app_module.run_admin_alerts() == 0  # выключено
    data = admin.post('/api/admin/health/test-alert', json={}).get_json()
    assert data['sent'] == 1 and 'проверка' in fake_smtp.sent[-1]['Subject']
    admin.put('/api/admin/health/alerts', json={'email': False})
    r = admin.post('/api/admin/health/test-alert', json={})
    assert r.status_code == 400 and 'Некому отправить' in r.get_json()['error']


def test_admin_alert_delivery_errors_are_logged(admin, flask_app, mail_ready, monkeypatch):
    import notify

    def refuse(*a, **k):
        raise notify.NotifyError('SMTP недоступен')
    monkeypatch.setattr(notify, 'send_email', refuse)
    with flask_app.app_context():
        db = app_module.get_db()
        assert app_module.send_admin_alert(db, 'Тема', ['строка']) == 0
        row = db.execute("SELECT status, message FROM notification_log WHERE kind = 'admin'").fetchone()
    assert tuple(row) == ('error', 'SMTP недоступен')


def test_scheduler_runs_admin_alerts(flask_app, monkeypatch):
    calls = []
    for name in ('run_alert_checks', 'deliver_outbox', 'run_admin_alerts'):
        monkeypatch.setattr(app_module, name, lambda name=name: calls.append(name))
    monkeypatch.setattr(app_module, 'run_due_digests', lambda now=None: calls.append('digests'))
    with flask_app.app_context():
        app_module.run_notifications()
    assert calls == ['run_alert_checks', 'digests', 'deliver_outbox', 'run_admin_alerts']
