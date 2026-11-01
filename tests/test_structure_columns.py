"""Показатели из колонок Structure (поле, сумма по вложенным, формула) — значения как в структуре, через Value API."""

import json

import pytest

import app as app_module
import fake_jira
import jira_integration as jira
from fake_jira import FakeJira
from test_jira import connect, create_button

SUM_REMAINING = {'id': 'sum', 'format': 'html', 'params': {'attribute': {'id': 'timeestimate', 'format': 'duration'}}}
FORMULA_SPENT = {'id': 'expr', 'format': 'text',
                 'params': {'formula': 'SUM{e} / 3600000 * 2', 'variables': {'e': {'id': 'timespent', 'format': 'duration'}}}}


@pytest.fixture
def fake():
    server = FakeJira()
    yield server
    server.stop()


def client_for(fake):
    return jira.JiraClient(fake.url, 'server', '', 'secret-pat')


def field_map(**over):
    fm = {'remaining': {'field': 'timeestimate', 'unit': 'seconds',
                        'structure': {'attribute': SUM_REMAINING, 'unit': 'ms', 'label': 'Σ остаток'}},
          'spent': {'field': '', 'unit': 'seconds', 'structure': {'attribute': FORMULA_SPENT, 'unit': 'hours'}},
          'estimate': {'field': 'timeoriginalestimate', 'unit': 'seconds'}}
    fm.update(over)
    return fm


# ─── Проверка спецификаций и карты полей ─────────────────────────

def test_attribute_spec_validation():
    assert jira.normalize_attribute({'id': 'timeestimate'}) == {'id': 'timeestimate', 'format': 'number'}
    assert jira.normalize_attribute(FORMULA_SPENT)['params']['variables']['e']['id'] == 'timespent'
    for bad, message in [('x', 'объект'), ({'format': 'number'}, 'id'), ({'id': 'a b'}, 'id'), ({'id': 'x', 'params': []}, 'params'),
                         ({'id': 'sum', 'params': {'attribute': {'id': 'плохо', 'format': 'number'}}}, 'id'),
                         ({'id': 'expr', 'params': {'formula': 'x' * 30000}}, 'большая')]:
        with pytest.raises(ValueError, match=message):
            jira.normalize_attribute(bad)


def test_field_map_with_structure_columns():
    fm = jira.normalize_field_map(field_map())
    assert fm['remaining']['structure'] == {'attribute': jira.normalize_attribute(SUM_REMAINING), 'unit': 'ms', 'label': 'Σ остаток'}
    assert fm['spent'] == {'field': '', 'unit': 'seconds', 'structure': {'attribute': FORMULA_SPENT, 'unit': 'hours', 'label': ''}}
    assert fm['estimate']['structure'] is None
    only_structure = {m: {'field': ''} for m in jira.METRICS}
    only_structure['remaining'] = {'field': '', 'structure': {'attribute': {'id': 'timeestimate'}}}
    assert jira.normalize_field_map(only_structure)['remaining']['structure']['unit'] == 'hours'
    with pytest.raises(ValueError, match='Единица колонки'):
        jira.normalize_field_map(field_map(spent={'field': '', 'structure': {'attribute': {'id': 'x'}, 'unit': 'weeks'}}))
    with pytest.raises(ValueError, match='Некорректная колонка'):
        jira.normalize_field_map(field_map(spent={'field': '', 'structure': 'x'}))
    with pytest.raises(ValueError, match='хотя бы одну'):
        jira.normalize_field_map({m: {'field': ''} for m in jira.METRICS})
    assert jira.normalize_field_map({'remaining': {'field': 'customfield_10050', 'unit': 'days'}})['remaining']['unit'] == 'days'


def test_forest_rows_keep_row_ids():
    assert jira.parse_forest_rows('1:0:101,3:1:5/9,bad,4:x:1,4:2:103') == [(1, 0, 101), (3, 1, None), (4, 2, 103)]


# ─── Срез ────────────────────────────────────────────────────────

def test_snapshot_takes_values_from_structure_columns(fake):
    items = jira.compute_snapshot(client_for(fake), 7, [{'serviceId': 1, 'issueKeys': 'ABC-1', 'includeSubtree': True},
                                                        {'serviceId': 2, 'issueKeys': 'ABC-4, ABC-1', 'includeSubtree': False}],
                                  jira.normalize_field_map(field_map()))
    first, second = items
    # Σ остатка по строкам ABC-1, ABC-2, ABC-3 (ABC-9 скрыта) — как в колонке; формула (5 + 1) × 2 = 12
    assert (first['remaining'], first['spent'], first['estimate']) == (12.5, 12.0, 16.0)
    assert (second['remaining'], second['spent']) == (22.5, 12.0)  # две задачи эпика — сумма их строк
    assert first['issues'][0]['spent'] is None  # у «Затрачено» нет поля Jira — состав задач без этого показателя
    bodies = fake.state['value_bodies']
    assert len(bodies) == 1 and len(bodies[0]['requests'][0]['attributes']) == 2  # одним запросом
    assert all(a['format'] == 'number' for a in bodies[0]['requests'][0]['attributes'])
    assert sum(1 for m, p in fake.state['requests'] if p.endswith('forest/latest')) == 1  # структура — один раз


def test_snapshot_structure_warnings(fake, monkeypatch):
    fm = jira.normalize_field_map(field_map(estimate={'field': '', 'structure': {'attribute': {'id': 'customfield_10060', 'format': 'text'},
                                                                                  'unit': 'hours', 'label': 'Тема'}}))
    monkeypatch.setattr(fake_jira, 'FORMULA', '1:0:101,2:1:102,8:0:101,5:0:104')  # ABC-1 дважды; ABC-6 вне структуры
    items = jira.compute_snapshot(client_for(fake), 7, [{'serviceId': 1, 'issueKeys': 'ABC-1, ABC-6', 'includeSubtree': False}], fm)
    warnings = ' '.join(items[0]['warnings'])
    assert 'ABC-6: задачи нет в структуре' in warnings and 'несколько раз' in warnings
    assert 'Колонка Structure «Тема» вернула не число' in warnings and items[0]['estimate'] is None
    no_structure = jira.compute_snapshot(client_for(fake), None, [{'serviceId': 1, 'issueKeys': 'ABC-1', 'includeSubtree': True}], fm)
    assert no_structure[0]['remaining'] is None and 'только у кнопки со структурой' in ' '.join(no_structure[0]['warnings'])


def test_structure_value_api_errors(fake):
    fm = jira.normalize_field_map(field_map(spent={'field': '', 'structure': {'attribute': {'id': 'нет'.encode().hex()}}}))
    with pytest.raises(jira.JiraError, match='Unknown attribute'):
        jira.compute_snapshot(client_for(fake), 7, [{'serviceId': 1, 'issueKeys': 'ABC-1', 'includeSubtree': True}], fm)
    fake.configure(value_api=False)
    with pytest.raises(jira.JiraError, match='API значений Structure недоступно'):
        jira.compute_snapshot(client_for(fake), 7, [{'serviceId': 1, 'issueKeys': 'ABC-1', 'includeSubtree': True}],
                              jira.normalize_field_map(field_map()))


def test_value_parsing():
    assert jira._number(' 1 234,5 ') == 1234.5 and jira._number(True) is None and jira._number('нет') is None
    assert jira._number(7) == 7 and jira._number(None) is None


# ─── API и редактор кнопки ───────────────────────────────────────

def test_button_with_structure_columns_snapshot_and_autofill(admin, flask_app, fake):
    connect(admin, fake)
    r = create_button(admin, fieldMap=field_map())
    assert r.status_code == 200, r.get_json()
    button = r.get_json()
    assert button['fieldMap']['remaining']['structure']['label'] == 'Σ остаток'
    snap = admin.post(f"/api/jira/buttons/{button['id']}/snapshot", json={}).get_json()
    assert (snap['items'][0]['remaining'], snap['items'][0]['spent']) == (12.5, 12.0)
    with flask_app.app_context():
        db = app_module.get_db()
        row = db.execute('SELECT * FROM jira_snapshot_buttons WHERE id = ?', (button['id'],)).fetchone()
        status, _message = app_module.run_autofill(db, row, '2026-10-30', 'Автозаполнение')
        db.commit()
        entry = db.execute("SELECT remaining, spent, estimate FROM entries WHERE service_id = 1 AND date = '2026-10-30'").fetchone()
    assert status == 'ok' and tuple(entry) == (12.5, 12.0, 16.0)


def test_structure_columns_need_structure(admin, fake):
    connect(admin, fake)
    r = create_button(admin, structureId=None, fieldMap=field_map())
    assert r.status_code == 400 and 'со структурой' in r.get_json()['error']
    r = create_button(admin, fieldMap=field_map(spent={'field': '', 'structure': {'attribute': {'id': ''}}}))
    assert r.status_code == 400 and 'id' in r.get_json()['error']


def test_structure_preview_endpoint(admin, make_user, fake):
    connect(admin, fake)
    body = {'structureId': 7, 'attribute': SUM_REMAINING, 'unit': 'ms', 'keys': 'ABC-1, ABC-5, ABC-404'}
    data = admin.post('/api/projects/1/jira/structure-preview', json=body).get_json()
    assert data['values'] == [{'key': 'ABC-1', 'rowId': 1, 'raw': 45000000, 'hours': 12.5, 'asUnits': {
        'ms': 12.5, 'seconds': 12500.0, 'minutes': 750000.0, 'hours': 45000000.0, 'days': 360000000.0}}]
    assert data['warnings'] == ['ABC-5: задачи нет в структуре', 'ABC-404: задача не найдена или нет прав на просмотр']
    for over, message in [({'structureId': None}, 'структуру'), ({'attribute': {'id': ''}}, 'id'), ({'unit': 'годы'}, 'единица'),
                          ({'keys': ''}, 'задачи'), ({'keys': 'не ключ'}, 'Некорректные')]:
        r = admin.post('/api/projects/1/jira/structure-preview', json=dict(body, **over))
        assert r.status_code == 400 and message in r.get_json()['error']
    viewer = make_user('viewer', project_id=1)
    assert viewer.post('/api/projects/1/jira/structure-preview', json=body).status_code == 403
    fake.configure(value_api=False)
    assert admin.post('/api/projects/1/jira/structure-preview', json=body).status_code == 502


def test_history_with_structure_columns_uses_jira_fields(admin, flask_app, fake, monkeypatch):
    monkeypatch.setitem(flask_app.config, 'HISTORY_SYNC', True)
    connect(admin, fake)
    bid = create_button(admin, fieldMap=field_map()).get_json()['id']
    assert admin.post(f'/api/jira/buttons/{bid}/snapshot', json={}).status_code == 200
    r = admin.post(f'/api/jira/buttons/{bid}/history', json={'from': '2026-10-19', 'to': '2026-10-26', 'step': 'week',
                                                            'timezone': 'Europe/Moscow', 'keepExisting': True})
    job = admin.get(f"/api/jira/history-jobs/{r.get_json()['id']}").get_json()
    assert job['status'] == 'done'
    assert 'по полям Jira, а не по колонкам Structure: остаток' in job['message']
    assert 'не восстановлено (колонка Structure без поля Jira): затрачено' in job['message']


def test_stored_field_map_round_trip(flask_app):
    row = {'field_map': json.dumps(field_map())}
    fm = app_module._button_field_map(row)
    assert fm['remaining']['structure']['unit'] == 'ms' and fm['estimate']['structure'] is None
    assert app_module._button_field_map({'field_map': '{"remaining": 5}'}) == jira.DEFAULT_FIELD_MAP
