"""Состав эпиков по задачам Jira: сохранение среза по задачам, burn-up и проблемы качества данных."""

from datetime import datetime, timezone

import pytest

import app as app_module
import jira_integration as jira
from fake_jira import FakeJira
from test_jira import connect, create_button


@pytest.fixture
def fake():
    server = FakeJira()
    yield server
    server.stop()


def snap(client, bid, date=None):
    r = client.post(f'/api/jira/buttons/{bid}/snapshot', json={'date': date} if date else {})
    assert r.status_code == 200, r.get_json()
    return r.get_json()


@pytest.fixture
def button(admin, fake):
    connect(admin, fake)
    return create_button(admin, mappings=[
        {'serviceId': 1, 'issueKeys': 'ABC-1', 'includeSubtree': True},       # поддерево в структуре
        {'serviceId': 2, 'issueKeys': 'ABC-4, ABC-5', 'includeSubtree': False},
    ]).get_json()['id']


# ─── Чистая логика ───────────────────────────────────────────────

def issue(**kw):
    base = {'key': 'X-1', 'category': 'new', 'container': False, 'remaining': None, 'spent': None, 'estimate': None}
    base.update(kw)
    return base


@pytest.mark.parametrize('kw, expected', [
    (dict(category='new'), ['no_estimate']),
    (dict(category='new', container=True), []),                       # эпик-контейнер без своей оценки — норма
    (dict(category='new', estimate=8, remaining=8), []),
    (dict(category='done', remaining=2, estimate=4, spent=3), ['done_with_remaining']),
    (dict(category='done'), []),
    (dict(category='indeterminate', remaining=0, estimate=5, spent=5), ['zero_remaining']),
    (dict(category='indeterminate', remaining=1, estimate=5, spent=9), ['overspent']),
], ids=['no-estimate', 'container', 'ok', 'done-remaining', 'done-empty', 'zero-remaining', 'overspent'])
def test_issue_problems(kw, expected):
    assert jira.issue_problems(issue(**kw), jira.DEFAULT_FIELD_MAP) == expected


def test_issue_problems_respect_unmapped_fields():
    only_remaining = {'remaining': {'field': 'timeestimate', 'unit': 'seconds'},
                      'spent': {'field': '', 'unit': 'seconds'}, 'estimate': {'field': '', 'unit': 'seconds'}}
    # без сопоставленной оценки и затрат нельзя судить о перерасходе и «остаток 0 при оценке»
    assert jira.issue_problems(issue(category='indeterminate', remaining=0, spent=9), only_remaining) == ['no_estimate']
    assert jira.issue_problems(issue(category='new', remaining=3), only_remaining) == []


def test_issue_detail_and_helpers():
    f = {'__key': 'ABC-7', 'summary': 'Тема', 'status': {'name': 'Код-ревью', 'statusCategory': {'key': 'indeterminate'}},
         'timeestimate': 7200, 'timespent': 3600, 'timeoriginalestimate': None}
    d = jira.issue_detail(f, jira.DEFAULT_FIELD_MAP)
    assert d == {'key': 'ABC-7', 'summary': 'Тема', 'status': 'Код-ревью', 'category': 'indeterminate',
                 'container': False, 'remaining': 2.0, 'spent': 1.0, 'estimate': None, 'problems': []}
    assert jira.issue_detail({'__key': 'A-1', 'status': {'statusCategory': {'key': 'undefined'}}},
                             jira.DEFAULT_FIELD_MAP)['category'] == 'new'
    assert sorted(['A-10', 'A-2', 'B-1'], key=jira.issue_sort_key) == ['A-2', 'A-10', 'B-1']
    assert jira.parent_issue_ids(jira.parse_forest_formula('1:0:101,2:1:102,3:1:5/9,4:2:103,5:0:104')) == {101}
    assert jira.issue_volume({'estimate': 5, 'spent': 9}) == 5
    assert jira.issue_volume({'estimate': None, 'spent': 2, 'remaining': 3}) == 5


# ─── Сохранение и API ────────────────────────────────────────────

def test_snapshot_stores_issues_and_keeps_response_light(admin, button):
    data = snap(admin, button, '2026-11-02')
    assert all('issues' not in it for it in data['items'])
    comp = admin.get('/api/projects/1/jira/composition').get_json()
    latest = comp['latest']['1']
    # ABC-1 — контейнер (в счёт задач не входит, его оценка — в объёме), ABC-2 готова, ABC-3 в работе; ABC-9 скрыта
    assert latest['date'] == '2026-11-02'
    assert latest['counts'] == {'new': 0, 'indeterminate': 1, 'done': 1} and latest['total'] == 2
    assert latest['volume'] == {'new': 0, 'indeterminate': 13.0, 'done': 3.0}
    assert (latest['scope'], latest['done']) == (16.0, 3.0)
    assert latest['problems'] == {'done_with_remaining': 1}
    assert comp['latest']['2']['counts'] == {'new': 2, 'indeterminate': 0, 'done': 0}
    assert comp['baseUrl'].startswith('http://127.0.0.1') and 'no_estimate' in comp['problemLabels']


def test_issue_list_for_epic(admin, button):
    snap(admin, button, '2026-11-02')
    data = admin.get('/api/services/1/jira/issues').get_json()
    assert data['date'] == '2026-11-02'
    assert [i['key'] for i in data['issues']] == ['ABC-1', 'ABC-2', 'ABC-3']
    abc2 = data['issues'][1]
    assert abc2 == {'key': 'ABC-2', 'summary': 'Интеграция с ЕИС', 'status': 'Готово', 'category': 'done',
                    'remaining': 2.0, 'spent': 1.0, 'estimate': 3.0, 'container': False,
                    'problems': ['done_with_remaining'], 'prev': None}
    assert data['issues'][0]['container'] is True
    assert admin.get('/api/services/3/jira/issues').get_json()['issues'] == []
    assert admin.get('/api/services/999/jira/issues').status_code == 404


def test_history_one_point_per_day(admin, fake, button):
    snap(admin, button, '2026-11-02')
    snap(admin, button, '2026-11-02')                                  # повторный срез за день заменяет состав
    fake_issues = __import__('fake_jira').ISSUES
    fake_issues[103]['category'] = 'done'
    try:
        snap(admin, button, '2026-11-03')
    finally:
        fake_issues[103]['category'] = 'indeterminate'
    history = admin.get('/api/projects/1/jira/composition').get_json()['history']['1']
    assert [(p['date'], p['done'], p['doneCount'], p['total']) for p in history] == [
        ('2026-11-02', 3.0, 1, 2), ('2026-11-03', 4.0, 2, 2)]
    with app_module.app.app_context():
        n = app_module.get_db().execute("SELECT COUNT(*) FROM jira_issue_snapshots WHERE snap_date = '2026-11-02' "
                                        'AND service_id = 1').fetchone()[0]
    assert n == 3


def test_subtasks_mode_marks_parent_as_container(admin, fake):
    connect(admin, fake)
    bid = create_button(admin, structureId=None,
                        mappings=[{'serviceId': 3, 'issueKeys': 'ABC-1', 'includeSubtree': True}]).get_json()['id']
    snap(admin, bid, '2026-11-02')
    issues = {i['key']: i for i in admin.get('/api/services/3/jira/issues').get_json()['issues']}
    assert set(issues) == {'ABC-1', 'ABC-6'} and issues['ABC-1']['container'] and not issues['ABC-6']['container']


def test_composition_access(admin, make_user, button):
    snap(admin, button, '2026-11-02')
    viewer = make_user('viewer1', project_id=1)
    assert viewer.get('/api/projects/1/jira/composition').status_code == 200
    assert viewer.get('/api/services/1/jira/issues').status_code == 200
    stranger = make_user('stranger')
    assert stranger.get('/api/projects/1/jira/composition').status_code == 403
    assert stranger.get('/api/services/1/jira/issues').status_code == 403


def test_bad_snapshot_date(admin, button):
    r = admin.post(f'/api/jira/buttons/{button}/snapshot', json={'date': '31.12.2026'})
    assert r.status_code == 400


def test_autofill_stores_composition(admin, flask_app, button):
    snap(admin, button, '2026-11-01')
    admin.put(f'/api/jira/buttons/{button}/schedule', json={
        'enabled': True, 'days': [1], 'time': '09:00', 'timezone': 'Europe/Moscow', 'overwrite': True})
    with flask_app.app_context():
        assert app_module.run_due_schedules(datetime(2026, 11, 2, 6, 30, tzinfo=timezone.utc)) == 1
    history = admin.get('/api/projects/1/jira/composition').get_json()['history']['1']
    assert [p['date'] for p in history] == ['2026-11-01', '2026-11-02']


def test_composition_deleted_with_epic(admin, flask_app, button):
    snap(admin, button, '2026-11-02')
    assert admin.delete('/api/services/1').status_code == 200
    with flask_app.app_context():
        db = app_module.get_db()
        assert db.execute('SELECT COUNT(*) FROM jira_issue_snapshots WHERE service_id = 1').fetchone()[0] == 0
        assert db.execute('SELECT COUNT(*) FROM jira_issue_snapshots').fetchone()[0] == 2  # эпик 2 остался


def test_empty_composition(admin):
    data = admin.get('/api/projects/1/jira/composition').get_json()
    assert data == {'baseUrl': '', 'latest': {}, 'history': {}, 'problemLabels': jira.PROBLEMS}


def test_dashboard_pdf_includes_jira_composition(admin, button):
    from pypdf import PdfReader
    import io
    snap(admin, button, '2026-11-02')
    r = admin.get('/api/projects/1/pdf?page=dashboard')
    assert r.status_code == 200
    text = ''.join(p.extract_text() for p in PdfReader(io.BytesIO(r.data)).pages)
    assert 'Задачи Jira по эпикам' in text and '02.11.2026' in text
    # без срезов раздела нет
    r = admin.get('/api/projects/2/pdf?page=dashboard')
    assert 'Задачи Jira' not in ''.join(p.extract_text() for p in PdfReader(io.BytesIO(r.data)).pages)



# ─── Что изменилось между срезами ────────────────────────────────

def test_scope_changes_decomposition():
    from app import scope_changes
    def i(key, est, cat='new', **kw):
        return dict({'key': key, 'summary': key, 'status': '', 'category': cat, 'estimate': est,
                     'remaining': None, 'spent': None}, **kw)
    before = {'A-1': i('A-1', 10), 'A-2': i('A-2', 5, 'done'), 'A-3': i('A-3', 8), 'A-4': i('A-4', 3, 'done')}
    after = {'A-1': i('A-1', 14, 'done'), 'A-2': i('A-2', 5, 'indeterminate'), 'A-3': i('A-3', 6),
             'A-5': i('A-5', 20), 'A-6': i('A-6', None, spent=2, remaining=1)}
    c = scope_changes(before, after)
    assert (c['scopeFrom'], c['scopeTo']) == (26, 48)
    assert (c['added']['count'], c['added']['volume']) == (2, 23)
    assert (c['removed']['count'], c['removed']['volume']) == (1, -3)
    assert (c['reestimated']['count'], c['reestimated']['volume']) == (2, 2)
    assert c['reestimated']['issues'][0] == {'key': 'A-1', 'summary': 'A-1', 'status': '', 'category': 'done',
                                             'volume': 14, 'before': 10, 'delta': 4}
    assert c['scopeFrom'] + c['added']['volume'] + c['removed']['volume'] + c['reestimated']['volume'] == c['scopeTo']
    assert [x['key'] for x in c['closed']['issues']] == ['A-1'] and [x['key'] for x in c['reopened']['issues']] == ['A-2']
    assert (c['doneFrom'], c['doneTo']) == (8, 14)


def test_changes_api(admin, button):
    import fake_jira
    snap(admin, button, '2026-11-02')
    assert admin.get('/api/services/1/jira/changes').get_json() == {
        'dates': ['2026-11-02'], 'from': None, 'to': '2026-11-02', 'baseUrl': admin.get(
            '/api/projects/1/jira/composition').get_json()['baseUrl']}
    old = dict(fake_jira.ISSUES[103])
    fake_jira.ISSUES[103].update(category='done', timeoriginalestimate=3 * 3600)
    try:
        snap(admin, button, '2026-11-09')
    finally:
        fake_jira.ISSUES[103] = old
    c = admin.get('/api/services/1/jira/changes').get_json()
    assert (c['from'], c['to'], c['scopeFrom'], c['scopeTo']) == ('2026-11-02', '2026-11-09', 16, 18)
    assert c['reestimated']['issues'][0]['key'] == 'ABC-3' and c['reestimated']['volume'] == 2
    assert c['closed']['count'] == 1 and c['added']['count'] == 0
    assert admin.get('/api/services/1/jira/changes?from=2026-11-09&to=2026-11-02').status_code == 400
    assert admin.get('/api/services/1/jira/changes?from=2026-11-03&to=2026-11-09').status_code == 400
    # в списке задач — значения прошлого среза для сравнения
    issues = {i['key']: i for i in admin.get('/api/services/1/jira/issues').get_json()['issues']}
    assert issues['ABC-3']['prev'] == {'remaining': 0.5, 'spent': None, 'estimate': 1.0, 'category': 'indeterminate'}
    data = admin.get('/api/services/1/jira/issues?date=2026-11-02').get_json()
    assert data['prevDate'] is None and all(i['prev'] is None for i in data['issues'])
