"""Корректность аналитики: найденные при аудите расхождения и падения закреплены здесь."""

import io
from datetime import datetime, timezone

import pytest
from pypdf import PdfReader

import app as app_module
import jira_integration as jira
from fake_jira import FakeJira
from test_notifications import SMTP, fake_smtp, mail_ready, subscribe  # noqa: F401 — фикстуры уведомлений


@pytest.fixture
def fake():
    server = FakeJira()
    yield server
    server.stop()


def make_epic(admin, pid, name, rows, target='', stage='Этап 1', done=False):
    sid = admin.post(f'/api/projects/{pid}/services', json={'name': name, 'etap': stage, 'targetDate': target,
                                                            'isDone': done}).get_json()['id']
    for date, remaining, spent, estimate in rows:
        assert admin.post(f'/api/services/{sid}/entries', json={'date': date, 'remaining': remaining, 'spent': spent,
                                                                'estimate': estimate}).status_code == 200
    return sid


def new_project(admin, name='Аналитика'):
    return admin.post('/api/projects', json={'name': name}).get_json()['id']


def overview(flask_app, pid, today=None):
    with flask_app.app_context():
        return app_module.project_overview(app_module.get_db(), pid, today)


# ─── Прогноз: ровный остаток, завершённый эпик, этап ─────────────

def test_flat_fractional_remaining_does_not_break_digest(admin, flask_app, mail_ready):
    """Ровный дробный остаток (наклон регрессии — шум округления) раньше ронял дайджест и оповещения."""
    pid = new_project(admin)
    make_epic(admin, pid, 'Заблокирован', [('2026-09-01', 120.7, 5, 130), ('2026-09-08', 120.7, 5, 130),
                                            ('2026-09-15', 120.7, 5, 130)], target='2026-10-01')
    epic = overview(flask_app, pid)['epics'][0]
    assert epic['status'] == 'danger' and epic['forecast'] is None and epic['noBurn']
    r = admin.get(f'/api/projects/{pid}/notifications/preview')
    assert r.status_code == 200 and 'не сгорает' in r.get_json()['html']


def test_finished_epic_stays_ok_after_zero_measurements(admin, flask_app):
    pid = new_project(admin)
    make_epic(admin, pid, 'Сдан вовремя', [('2026-09-01', 100, 0, 100), ('2026-09-08', 50, 50, 100),
                                           ('2026-09-15', 0, 100, 100), ('2026-09-22', 0, 100, 100),
                                           ('2026-09-29', 0, 100, 100)], target='2026-09-19')
    epic = overview(flask_app, pid)['epics'][0]
    assert (epic['status'], epic['forecast'], epic['probability']) == ('ok', '2026-09-15', 1.0)


def test_stage_ignores_empty_dates_and_late_first_measurement(admin, flask_app):
    """Этап из двух эпиков, замеряемых в разные дни, и пустой строки «+ Новая дата»: сумма не «растёт»."""
    pid = new_project(admin)
    a = make_epic(admin, pid, 'A', [('2026-09-01', 100, 0, 100), ('2026-09-08', 90, 10, 100), ('2026-09-15', 80, 20, 100)],
                  target='2026-12-01')
    make_epic(admin, pid, 'B', [('2026-09-03', 100, 0, 100), ('2026-09-10', 90, 10, 100), ('2026-09-17', 80, 20, 100)],
              target='2026-12-01')
    assert admin.post(f'/api/services/{a}/entries', json={'date': '2026-09-22'}).status_code == 200  # пустая дата
    admin.put(f'/api/projects/{pid}/stage_targets', json={'stageName': 'Этап 1', 'targetDate': '2026-12-01'})
    stage = overview(flask_app, pid)['stages'][0]
    assert stage['status'] == 'ok' and stage['forecast'] < '2026-12-01'
    assert stage['remaining'] == 160


# ─── Дайджест, оповещения, PDF ───────────────────────────────────

def test_digest_pdf_has_same_epics_as_digest(admin, flask_app, monkeypatch, mail_ready):
    pid = new_project(admin)
    active = make_epic(admin, pid, 'Активный', [('2026-09-01', 100, 0, 100), ('2026-09-08', 60, 40, 100)])
    make_epic(admin, pid, 'Сданный', [('2026-09-01', 40, 0, 40), ('2026-09-08', 20, 20, 40)], done=True)
    calls = []
    real = app_module.build_project_pdf

    def spy(db, project, page_type, epics_arg=None, show_done=False):
        calls.append(epics_arg)
        return real(db, project, page_type, epics_arg, show_done)
    monkeypatch.setattr(app_module, 'build_project_pdf', spy)
    subscribe(admin, pid, digest={'enabled': True, 'days': [1], 'time': '09:00', 'timezone': 'Europe/Moscow', 'pdf': True})
    assert admin.post(f'/api/projects/{pid}/notifications/test').status_code == 200
    assert calls == [str(active)]


def test_digest_date_in_subscriber_timezone(flask_app):
    assert overview(flask_app, 1, '2026-11-02')['date'] == '2026-11-02'


def test_problem_count_counts_issues_not_codes(admin, flask_app):
    issue = {'key': 'ABC-1', 'summary': 'x', 'status': 'Готово', 'category': 'done', 'remaining': 5, 'spent': 10,
             'estimate': 3, 'container': False, 'problems': ['done_with_remaining', 'overspent']}
    with flask_app.app_context():
        db = app_module.get_db()
        app_module.store_issue_snapshots(db, 1, [{'serviceId': 1, 'issues': [issue]}], '2026-10-28')
        db.commit()
        _history, latest = app_module.jira_composition(db, 1)
    assert latest[1]['problemIssues'] == 1 and sum(latest[1]['problems'].values()) == 2
    assert overview(flask_app, 1)['epics'][0]['problems'] == 1


def test_scope_alert_is_strictly_greater_than_threshold():
    old = {'epic:1': {'name': 'A', 'status': 'ok', 'probability': None, 'scope': 100, 'problems': 0, 'kind': 'epic'}}
    sub = {'alert_types': 'scope', 'chance_threshold': 50, 'scope_threshold': 10}
    exactly = {'epic:1': dict(old['epic:1'], scope=110)}
    more = {'epic:1': dict(old['epic:1'], scope=110.5)}
    assert app_module.alert_events(old, exactly, sub) == []  # «больше чем на 10 %»
    assert [k for k, _t in app_module.alert_events(old, more, sub)] == ['scope']


def test_numbers_round_half_up_like_interface():
    assert app_module._ru_num(10.125) == '10,13' and app_module._ru_num(1188.3) == '1 188,3'
    assert app_module._fmt_num(0.25) == '0.3' and app_module._fmt_num(None) == '—'


def test_pdf_composition_rounding_and_far_forecast():
    def pt(date, scope, done):
        return {'date': date, 'scope': scope, 'done': done, 'total': 1, 'doneCount': 0,
                'counts': {'new': 0, 'indeterminate': 1, 'done': 0}}
    history = [pt('2026-09-01', 8, 0), pt('2026-09-08', 8, 1)]
    row = app_module._composition_pdf_row('Эпик', history, dict(history[-1], problems={}, problemIssues=0), None)
    assert row[4] == '13%'  # 1 из 8 ч — 12,5 %, как в интерфейсе (половина — вверх)
    slow = [pt('2026-09-01', 2000, 0), pt('2026-09-11', 2000, 1)]  # 0,1 ч/день против 1999 ч
    assert app_module.burnup_forecast(slow)['tooFar']
    assert app_module._composition_pdf_row('Эпик', slow, dict(slow[-1], problems={}, problemIssues=0), None)[6] == '> 3 лет'


# ─── Срез из Jira ────────────────────────────────────────────────

def test_container_flag_does_not_depend_on_mapping_order(fake):
    """ABC-1 — контейнер только там, где в сумму вошли её вложенные задачи."""
    client = jira.JiraClient(fake.url, 'server', '', 'secret-pat')
    with_subtree = {'serviceId': 1, 'issueKeys': 'ABC-1', 'includeSubtree': True}
    only_root = {'serviceId': 2, 'issueKeys': 'ABC-1', 'includeSubtree': False}
    for order in ([with_subtree, only_root], [only_root, with_subtree]):
        items = {it['serviceId']: it for it in jira.compute_snapshot(client, 7, order)}
        root_only = items[2]['issues'][0]
        assert root_only['key'] == 'ABC-1' and not root_only['container']
        assert next(i for i in items[1]['issues'] if i['key'] == 'ABC-1')['container']


def test_history_restore_replaces_earlier_restored_composition(admin, flask_app, fake, monkeypatch):
    """Повторное восстановление истории заменяет ранее восстановленный состав (настоящий срез — не трогает)."""
    import fake_jira
    from test_history import make_button, restore
    monkeypatch.setitem(flask_app.config, 'HISTORY_SYNC', True)
    bid = make_button(admin, fake)
    assert restore(admin, bid, keepExisting=False).status_code in (200, 202)
    monkeypatch.setitem(fake_jira.ISSUES[101], 'summary', 'Новая тема')
    assert restore(admin, bid, keepExisting=False).status_code in (200, 202)
    with flask_app.app_context():
        rows = app_module.get_db().execute("SELECT summary, restored FROM jira_issue_snapshots WHERE issue_key = 'ABC-1' "
                                           "AND snap_date = '2026-10-26'").fetchall()
    assert [tuple(r) for r in rows] == [('Новая тема', 1)]
