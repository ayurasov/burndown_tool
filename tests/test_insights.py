"""«Выводы» (insights.py) на уровнях эпика, этапа, проекта и портфеля; API, личный вид, дайджест и PDF с выводами;
единицы и рабочие дни в текстах."""

import io
import sqlite3

import pytest
from pypdf import PdfReader

import app as app_module
import forecast as fc
import insights as ins
from test_notifications import SMTP, fake_smtp, mail_ready, subscribe  # noqa: F401 — фикстуры уведомлений

S = dict(fc.DEFAULT_SETTINGS)
NB = ' '


def weekly(start, values):
    return [fc.add_days(start, 7 * i) for i in range(len(values))], list(values)


def drivers(values, target, today, estimate=None, settings=None, scope=None, start='2026-09-01'):
    dates, rems = weekly(start, values)
    return ins.series_drivers(dates, rems, estimate or [None] * len(values), target, settings or S, today, scope)


def kinds(found):
    return [(f['severity'], f['kind']) for f in found]


def findings(values, target, today, **kw):
    settings = kw.pop('settings', S)
    drv = drivers(values, target, today, settings=settings, **kw)
    return drv, ins.series_findings(drv, settings, today)


# ─── Эпик: статус, темп, объём, сдвиг, свежесть ─────────────────

def test_on_track_with_pace_reserve():
    drv, found = findings([100, 80, 60, 40], '2026-11-30', '2026-09-23')
    assert drv['status'] == 'ok' and drv['forecast'] == '2026-10-06'
    assert kinds(found)[:2] == [('ok', 'ontrack'), ('ok', 'pace')]
    assert f'раньше цели на 55{NB}дней' in found[0]['text'] and 'шанс успеть — 100' in found[0]['text']
    assert 'для цели достаточно 0,58' in found[1]['text'] and 'текущий темп — 2,86' in found[1]['text']


def test_late_epic_needs_faster_pace():
    drv, found = findings([100, 90, 80, 70], '2026-10-15', '2026-09-23')
    assert drv['status'] == 'danger' and drv['lateDays'] > 0
    assert ('danger', 'late') in kinds(found) and ('danger', 'pace') in kinds(found)
    pace = next(f for f in found if f['kind'] == 'pace')
    assert 'в 2,1 раза быстрее' in pace['text']  # 70 ч за 23 дня против 10 ч в неделю
    assert found[0]['severity'] == 'danger'  # самое важное — первым


def test_no_burn_too_far_and_overdue():
    _drv, found = findings([100, 110, 120], '2026-12-01', '2026-09-15')
    assert kinds(found)[0] == ('danger', 'noburn') and 'не уменьшается' in found[0]['text']
    _drv, found = findings([10000, 10000, 9999.99], '2026-12-01', '2026-09-15')
    assert ('danger', 'toofar') in kinds(found)
    _drv, found = findings([100, 80, 60], '2026-09-10', '2026-09-20')
    assert kinds(found)[0] == ('danger', 'overdue') and 'Цель 10.09.2026 прошла, осталось 60' in found[0]['text']
    assert 'pace' not in [k for _s, k in kinds(found)]
    _drv, found = findings([100, 110, 120], None, '2026-09-15')
    assert ('risk', 'noburn') in kinds(found) and ('info', 'notarget') not in kinds(found)


def test_finished_and_few_measurements():
    _drv, found = findings([50, 20, 0, 0], '2026-09-20', '2026-10-01')
    assert kinds(found) == [('ok', 'finished')] and 'завершены 15.09.2026 — к цели' in found[0]['text']
    _drv, found = findings([50, 20, 0], '2026-09-10', '2026-10-01')
    assert kinds(found) == [('info', 'finished')] and f'позже цели на 5{NB}дней' in found[0]['text']
    _drv, found = findings([50], '2026-12-01', '2026-09-02')
    assert ('info', 'fewdata') in kinds(found)
    drv, found = findings([], '2026-12-01', '2026-09-02')
    assert kinds(found) == [('info', 'nodata')] and drv['measurements'] == 0


def test_slowdown_and_speedup():
    # 4 недели по 20 ч, затем 4 недели по 5 ч (окно сравнения — 28 дней)
    _drv, found = findings([300, 280, 260, 240, 220, 215, 210, 205, 200], '2027-06-01', '2026-10-27')
    slow = next(f for f in found if f['kind'] == 'slowdown')
    assert slow['severity'] == 'risk' and 'Темп снизился на 75' in slow['text']
    _drv, found = findings([300, 295, 290, 285, 280, 260, 240, 220, 200], '2027-06-01', '2026-10-27')
    assert ('ok', 'speedup') in kinds(found)


def test_scope_growth_from_estimate_and_jira():
    _drv, found = findings([100, 95, 90, 85, 80], '2027-06-01', '2026-09-29', estimate=[100, 100, 100, 115, 130])
    scope = next(f for f in found if f['kind'] == 'scope')
    assert scope['severity'] == 'risk' and scope['text'].startswith('Оценка выросла на 30') and '(+30' in scope['text']
    jira_scope = [('2026-09-01', 50), ('2026-09-29', 200)]  # срезы Jira точнее оценки в замерах
    _drv, found = findings([100, 95, 90, 85, 80], '2027-06-01', '2026-09-29', estimate=[100] * 5, scope=jira_scope)
    scope = next(f for f in found if f['kind'] == 'scope')
    assert scope['text'].startswith('Объём задач Jira вырос в 4 раза')
    _drv, found = findings([100, 95, 90, 85, 80], '2027-06-01', '2026-09-29', estimate=[100, 100, 100, 90, 70])
    assert ('info', 'scope') in kinds(found)


def test_forecast_slip_and_improvement():
    # прогноз на замер месяц назад — середина октября; сейчас темп упал — прогноз уехал
    drv, found = findings([200, 170, 140, 110, 100, 95, 90], '2027-03-01', '2026-10-13')
    assert drv['slipDays'] > 7 and ('risk', 'slip') in kinds(found)
    assert 'был ' in next(f for f in found if f['kind'] == 'slip')['text']
    drv, found = findings([200, 195, 190, 185, 140, 100, 60], '2027-03-01', '2026-10-13')
    assert drv['slipDays'] < -7 and ('ok', 'slip') in kinds(found)


def test_stale_data_respects_threshold():
    _drv, found = findings([100, 80, 60], '2027-01-01', '2026-10-01')
    stale = next(f for f in found if f['kind'] == 'stale')
    assert f'Последний замер 15.09.2026 — 16{NB}дней назад' in stale['text']
    _drv, found = findings([100, 80, 60], '2027-01-01', '2026-10-01', settings=dict(S, staleDays=30))
    assert 'stale' not in [k for _s, k in kinds(found)]


def test_low_chance_when_forecast_is_on_time():
    drv = {'measurements': 5, 'finished': False, 'remaining': 50, 'target': '2026-12-01', 'forecast': '2026-11-20',
           'status': 'ok', 'noBurn': False, 'tooFar': False, 'lateDays': -11, 'probability': 0.31, 'lo': '2026-11-01',
           'hi': '2027-01-10', 'confidence': 70, 'paceRatio': None, 'requiredRate': None, 'burnRate': 2, 'trendPct': None,
           'slipDays': None, 'slipFrom': None, 'scopePct': None, 'staleDays': 0, 'lastDate': '2026-11-01',
           'lastDecrease': '2026-11-01'}
    found = ins.series_findings(drv, S, '2026-11-01')
    assert kinds(found)[0] == ('risk', 'chance') and 'только 31' in found[0]['text']
    assert 'диапазон 70' in next(f for f in found if f['kind'] == 'ontrack')['text']
    assert ins.series_findings(dict(drv, probability=0.6), S, '2026-11-01')[0]['kind'] == 'ontrack'
    assert ins.series_findings(dict(drv, probability=0.6), dict(S, chanceThreshold=70), '2026-11-01')[0]['kind'] == 'chance'


def test_jira_data_quality_finding():
    rows = [{'date': d, 'remaining': r, 'spent': None, 'estimate': None}
            for d, r in zip(*weekly('2026-09-01', [100, 80, 60]))]
    epic = {'id': 1, 'name': 'A', 'target': '2026-12-01'}
    e = ins.epic_insight(epic, rows, S, '2026-09-16', None, {'problemIssues': 3, 'total': 10})
    q = next(f for f in e['findings'] if f['kind'] == 'quality')
    assert q['severity'] == 'risk' and q['text'].startswith('3 задачи Jira') and e['problems'] == 3
    e = ins.epic_insight(epic, rows, S, '2026-09-16', None, {'problemIssues': 1, 'total': 40})
    assert next(f for f in e['findings'] if f['kind'] == 'quality')['severity'] == 'info'


# ─── Этап, проект, портфель ──────────────────────────────────────

def _epic(eid, name, values, target, today='2026-09-23', start='2026-09-01'):
    dates, rems = weekly(start, values)
    rows = [{'date': d, 'remaining': r, 'spent': None, 'estimate': None} for d, r in zip(dates, rems)]
    return ins.epic_insight({'id': eid, 'name': name, 'target': target, 'stage': 'Этап 1'}, rows, S, today), rows


def test_stage_names_epic_that_defines_the_deadline():
    fast, r1 = _epic(1, 'Быстрый', [400, 300, 200, 100], '2026-12-01')
    slow, r2 = _epic(2, 'Медленный', [40, 38, 36, 34], '2026-12-01')
    dates, series = fc.aggregate_series([r1, r2])
    st = ins.stage_insight('Этап 1', '2026-12-01', [fast, slow], dates, series, S, '2026-09-23')
    assert st['status'] == 'ok' and st['critical'] == 2 and st['atRisk'] == [2]
    critical = next(f for f in st['findings'] if f['kind'] == 'critical')
    assert critical['severity'] == 'risk' and 'эпик «Медленный» — нет' in critical['text']
    assert 'Под угрозой 1 из 2 эпиков: «Медленный»' in next(f for f in st['findings'] if f['kind'] == 'epics')['text']


def test_project_summary_headline_top_and_health():
    ok, r1 = _epic(1, 'Хороший', [100, 80, 60, 40], '2026-12-01')
    late, r2 = _epic(2, 'Поздний', [100, 95, 90, 85], '2026-10-01')
    stale, r3 = _epic(3, 'Старый', [100, 80], '2026-12-01')
    nt, r4 = _epic(4, 'Без цели', [100, 80, 60, 40], None)
    epics = [ok, late, stale, nt]
    dates, series = fc.aggregate_series([r1, r2, r3, r4])
    stage = ins.stage_insight('Этап 1', None, epics, dates, series, S, '2026-09-23')
    drv = ins.series_drivers(dates, series['remaining'], series['estimate'], None, S, '2026-09-23', with_mc=False)
    summary = ins.project_summary(drv, epics, [stage], S, [{'button': 'Срез', 'lastStatus': 'error', 'lastMessage': 'Нет связи'}])
    assert summary['status'] == 'danger'
    assert summary['headline'] == 'Сроки под угрозой: не успевают 1 из 4 эпиков.'
    assert [f['subject'] for f in summary['top']] == ['Поздний', 'Этап 1']  # один вывод на эпик/этап — самый важный
    assert summary['top'][0]['ref'] == {'level': 'epic', 'id': 2}
    health = ' '.join(f['text'] for f in summary['health'])
    assert '«Старый»' in health and 'Без целевой даты: «Без цели»' in health and 'Автозаполнение «Срез» не выполнилось' in health
    labels = [f['label'] for f in summary['facts']]
    assert labels[:3] == ['Остаток', 'Темп', 'Работы при текущем темпе']


def test_project_summary_ok_and_empty():
    a, r1 = _epic(1, 'A', [100, 80, 60, 40], '2026-12-01', today='2026-09-23')
    b, r2 = _epic(2, 'B', [100, 80, 60, 40], '2026-12-01', today='2026-09-23')
    dates, series = fc.aggregate_series([r1, r2])
    st = ins.stage_insight('Этап 1', '2026-12-01', [a, b], dates, series, S, '2026-09-23')
    drv = ins.series_drivers(dates, series['remaining'], series['estimate'], None, S, '2026-09-23', with_mc=False)
    summary = ins.project_summary(drv, [a, b], [st], S)
    assert summary['status'] == 'ok' and summary['headline'] == 'Сроки в порядке: 2 эпика с целями успевают.'
    assert summary['top'] == [] and any(f['label'] == 'Прогноз завершения' for f in summary['facts'])
    empty = ins.project_summary(ins.series_drivers([], [], [], None, S, '2026-09-23'), [], [], S)
    assert empty['headline'] == 'В проекте нет несданных эпиков.' and empty['status'] == 'unknown'


def test_portfolio_orders_projects_by_severity():
    mk = lambda pid, name, status: {'id': pid, 'name': name, 'summary': {'status': status}}  # noqa: E731
    res = ins.portfolio_summary([mk(1, 'Б', 'ok'), mk(2, 'А', 'risk'), mk(3, 'В', 'danger'), mk(4, 'Г', 'unknown')])
    assert [p['name'] for p in res['projects']] == ['В', 'А', 'Б', 'Г']
    assert res['headline'] == 'Сроки под угрозой в 1 из 4 проектов: «В»; есть риски в 1: «А».'
    assert res['counts'] == {'danger': 1, 'risk': 1, 'ok': 1, 'unknown': 1}
    assert ins.portfolio_summary([mk(1, 'А', 'ok')])['headline'] == 'Сроки в порядке во всех проектах (1).'


def test_units_workdays_and_number_format():
    days = dict(S, units='days', hoursPerDay=8, workdays=True)
    assert ins.fmt_work(12, days) == f'1,5{NB}чел.-дн.' and ins.fmt_rate(4, days) == f'0,5{NB}чел.-дн./раб. день'
    assert ins.fmt_work(1234.345, S) == f'1{NB}234,35{NB}ч' and ins.fmt_work(None, S) == '—'
    assert ins.fmt_rate(2.5, S) == f'2,5{NB}ч/день'
    assert ins.ru_num(-0.001) == '0' and ins.ru_num(7.0, 0) == '7'
    assert [ins.days_text(n) for n in (1, 2, 5, 11, 21, 22)] == [
        f'1{NB}день', f'2{NB}дня', f'5{NB}дней', f'11{NB}дней', f'21{NB}день', f'22{NB}дня']


def test_workdays_change_required_pace():
    # пт 18.09 осталось 40 ч, цель — пт 25.09: 5 рабочих дней вместо 7 календарных
    dates = ['2026-09-04', '2026-09-11', '2026-09-18']
    cal_drv = ins.series_drivers(dates, [100, 70, 40], [None] * 3, '2026-09-25', S, '2026-09-18')
    work_drv = ins.series_drivers(dates, [100, 70, 40], [None] * 3, '2026-09-25', dict(S, workdays=True), '2026-09-18')
    assert cal_drv['requiredRate'] == pytest.approx(40 / 7) and work_drv['requiredRate'] == pytest.approx(40 / 5)
    assert work_drv['burnRate'] == pytest.approx(60 / 10)


# ─── API и личный вид ────────────────────────────────────────────

def test_insights_api_and_access(admin, make_user):
    r = admin.get('/api/projects/1/insights')
    assert r.status_code == 200
    data = r.get_json()
    assert {e['name'] for e in data['epics']} >= {'КСБО', 'ЦЗК'} and {s['name'] for s in data['stages']} == {'Этап 3', 'Этап 4'}
    assert data['summary']['headline'] and data['settings']['units'] == 'hours'
    assert all(e['headline'] for e in data['epics'])
    admin.put('/api/services/1', json={'isDone': True})  # сданный эпик в выводы не попадает
    assert 1 not in {e['id'] for e in admin.get('/api/projects/1/insights').get_json()['epics']}
    assert admin.get('/api/projects/999/insights').status_code == 404
    viewer = make_user('viewer', project_id=1)
    assert viewer.get('/api/projects/1/insights').status_code == 200
    assert viewer.get('/api/projects/2/insights').status_code == 403
    assert [p['id'] for p in viewer.get('/api/portfolio').get_json()['projects']] == [1]


def test_portfolio_api_for_admin(admin):
    data = admin.get('/api/portfolio').get_json()
    assert [p['name'] for p in data['projects']] == ['АИСУЗ', 'ИСУП']  # проект с рисками — первым
    first = data['projects'][0]
    assert first['epics'] == 6 and first['stages'] == 2 and len(first['summary']['top']) <= 3
    assert data['projects'][1]['summary']['status'] == 'unknown'


def test_user_prefs_hide_blocks(admin, client):
    me = admin.get('/api/auth/me').get_json()['user']
    assert me['prefs'] == {'hiddenBlocks': {'dashboard': [], 'stages': []}}
    r = admin.put('/api/profile/prefs', json={'hiddenBlocks': {'dashboard': ['drift', 'kpis', 'drift'], 'stages': ['stageCharts']}})
    assert r.status_code == 200 and r.get_json()['prefs']['hiddenBlocks']['dashboard'] == ['kpis', 'drift']
    assert admin.get('/api/auth/me').get_json()['user']['prefs']['hiddenBlocks']['stages'] == ['stageCharts']
    assert admin.put('/api/profile/prefs', json={'hiddenBlocks': {'dashboard': ['нет такого']}}).status_code == 400
    assert admin.put('/api/profile/prefs', json={'hiddenBlocks': ['kpis']}).status_code == 400
    assert admin.put('/api/profile/prefs', json={'hiddenBlocks': {'dashboard': 'kpis'}}).status_code == 400
    assert client.put('/api/profile/prefs', json={'hiddenBlocks': {}}).status_code == 401


def test_broken_prefs_fall_back_to_defaults(flask_app):
    with flask_app.app_context():
        db = app_module.get_db()
        for raw in ('не json', '[1, 2]', '{"hiddenBlocks": {"dashboard": ["kpis", "x"]}}'):
            db.execute('UPDATE users SET prefs = ? WHERE id = 1', (raw,))
            user = db.execute('SELECT * FROM users WHERE id = 1').fetchone()
            hidden = app_module.user_prefs(user)['hiddenBlocks']
            assert hidden['stages'] == [] and hidden['dashboard'] in ([], ['kpis'])


def test_migration_adds_settings_columns_and_prefs(tmp_path):
    from init_db import SETTINGS_COLUMNS_V17, upgrade_settings
    conn = sqlite3.connect(str(tmp_path / 'old.db'))
    conn.execute('CREATE TABLE settings (id INTEGER PRIMARY KEY, project_id INTEGER, forecast_method TEXT, '
                 'risk_buffer_days INTEGER, burnrate_method TEXT, burnrate_window_days INTEGER)')
    conn.execute('CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT)')
    conn.execute("INSERT INTO settings VALUES (1, 1, 'average', 5, 'rolling', 21)")
    upgrade_settings(conn)
    upgrade_settings(conn)  # повторный запуск ничего не ломает
    cols = {r[1] for r in conn.execute('PRAGMA table_info(settings)')}
    assert {c for c, _t in SETTINGS_COLUMNS_V17} <= cols
    assert 'prefs' in {r[1] for r in conn.execute('PRAGMA table_info(users)')}
    conn.row_factory = sqlite3.Row
    st = fc.settings_dict(conn.execute('SELECT * FROM settings').fetchone())
    assert (st['forecastMethod'], st['riskBufferDays'], st['burnrateWindowDays']) == ('average', 5, 21)
    assert st['rangeConfidence'] == 70 and st['workdays'] is False and st['holidays'] == []


# ─── Дайджест и PDF ──────────────────────────────────────────────

def test_digest_starts_with_insights(admin, mail_ready, fake_smtp):
    preview = admin.get('/api/projects/1/notifications/preview').get_json()
    assert 'Сроки под угрозой' in preview['html'] and 'insights' not in preview['overview']
    subscribe(admin, digest={'enabled': True, 'days': [1], 'time': '09:00', 'timezone': 'Europe/Moscow', 'pdf': False})
    assert admin.post('/api/projects/1/notifications/test').status_code == 200
    text = fake_smtp.sent[-1].get_body(('plain',)).get_content()
    assert 'Главное: Сроки под угрозой' in text and text.index('Главное:') < text.index('• Сервис "Работа с жалобами" —')


def test_digest_in_person_days(admin, mail_ready):
    admin.put('/api/projects/1/settings', json={'units': 'days', 'hoursPerDay': 8})
    html = admin.get('/api/projects/1/notifications/preview').get_json()['html']
    assert f'154,29{NB}чел.-дн.' in html  # 1234,34 ч / 8
    assert f'{NB}ч<' not in html


def pdf_text(client, **params):
    r = client.get('/api/projects/1/pdf', query_string=params)
    assert r.status_code == 200
    return '\n'.join(page.extract_text() for page in PdfReader(io.BytesIO(r.data)).pages)


def test_pdf_dashboard_has_insights_and_deadlines(admin):
    text = pdf_text(admin, page='dashboard')
    assert 'Выводы' in text and 'Главные риски' in text and 'Сроки по эпикам' in text
    assert 'Не успеваем' in text and 'не сгорает' in text
    selected = pdf_text(admin, page='dashboard', epics='3,6')
    assert 'Выводы по выбранным эпикам' in selected and 'ЦЗК' not in selected and 'Главные риски' not in selected


def test_pdf_stages_insights_and_units(admin):
    text = pdf_text(admin, page='stages')
    assert 'Выводы по этапам' in text and 'Этап 4 — ' in text
    admin.put('/api/projects/1/settings', json={'units': 'days', 'hoursPerDay': 8, 'workdays': True})
    text = pdf_text(admin, page='dashboard')
    assert 'Остаток, чел.-дн.' in text and '154.3' in text and 'чел.-дн./раб. день' in text
    assert 'Остаток, ч' in pdf_text(admin, page='data')  # данные по датам — в часах, как вводятся
