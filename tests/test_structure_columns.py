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
    assert fm['remaining']['structure'] == {'attribute': jira.normalize_attribute(SUM_REMAINING), 'unit': 'ms', 'label': 'Σ остаток',
                                            'afterFiltering': False}
    assert fm['spent'] == {'field': '', 'unit': 'seconds', 'structure': {'attribute': FORMULA_SPENT, 'unit': 'hours', 'label': '',
                                                                         'afterFiltering': False}}
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
    assert 'ABC-6: задачи нет в структуре' in warnings and 'стоит в структуре 2 раз(а)' in warnings
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
        'ms': 12.5, 'seconds': 12500.0, 'minutes': 750000.0, 'hours': 45000000.0, 'days': 360000000.0},
        'places': [{'rowId': 1, 'depth': 0, 'chosen': True, 'path': [], 'raw': 45000000}]}]
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


# ─── Колонка, заданная как в Structure: поле или формула, Σ, дубликаты (1.9.1) ──

USER_FORMULA = '''WITH rest = orig - spent :

if (
  rest < 0 OR (
    status != "Backlog" AND
    status != "В работе" AND
    status != "Reopened"
  ),
  0,
  rest
)'''
USER_VARS = {'orig': {'field': 'timeoriginalestimate', 'format': 'duration'},
             'spent': {'field': 'timespent', 'format': 'duration'},
             'status': {'field': 'status', 'format': 'text'}}
WITH_DUPLICATE = '1:0:101,2:1:102,6:1:105,3:1:5/9,4:2:103,9:2:102,5:0:104'  # ABC-2 стоит под ABC-1 дважды


def test_column_editor_builds_structure_specs():
    field = jira.normalize_column_editor({'mode': 'field', 'field': 'timespent', 'format': 'duration', 'sum': True})
    assert field['distinct'] is True  # «Исключать дубликаты» — по умолчанию, как в Structure
    assert jira.build_structure_attribute(field) == {
        'id': 'expr', 'format': 'number',
        'params': {'formula': 'SUM{\nvalue\n}', 'variables': {'value': {'id': 'timespent', 'format': 'duration'}}}}
    with_dups = jira.build_structure_attribute(dict(field, distinct=False))
    assert with_dups['id'] == 'sum' and with_dups['params']['attribute']['params']['formula'] == 'value'
    own = jira.build_structure_attribute(dict(field, sum=False))
    assert own['params']['formula'] == 'value'
    custom = jira.normalize_column_editor({'mode': 'field', 'field': 'customfield_10050', 'format': 'number'})
    assert jira.build_structure_attribute(custom)['params']['variables']['value'] == {
        'id': 'customfield', 'format': 'number', 'params': {'fieldId': 10050}}
    formula = jira.normalize_column_editor({'mode': 'formula', 'formula': USER_FORMULA, 'variables': USER_VARS, 'sum': True})
    spec = jira.build_structure_attribute(formula)
    assert spec['params']['formula'].startswith('SUM{\nWITH rest') and spec['params']['variables']['status'] == {
        'id': 'status', 'format': 'text'}
    stored = jira.normalize_column_editor(field)  # сохранённые настройки читаются повторно
    assert stored == field


def test_column_editor_validation():
    cases = [('x', 'Тип колонки'), ({'mode': 'formula', 'formula': ' '}, 'Введите формулу'),
             ({'mode': 'formula', 'formula': 'a' * 20001}, 'большая'),
             ({'mode': 'formula', 'formula': 'a', 'variables': []}, 'объект'),
             ({'mode': 'formula', 'formula': 'a', 'variables': {'1a': {'field': 'x'}}}, 'имя переменной'),
             ({'mode': 'formula', 'formula': 'a', 'variables': {'a': 'x'}}, 'выберите поле'),
             ({'mode': 'formula', 'formula': 'a', 'variables': {'a': {'field': ''}}}, 'Выберите поле Jira для переменной «a»'),
             ({'mode': 'formula', 'formula': 'a', 'variables': {'a': {'field': 'bad id'}}}, 'некорректное поле'),
             ({'mode': 'formula', 'formula': 'a', 'variables': {'a': {'field': 'status', 'format': 'json'}}}, 'формат'),
             ({'mode': 'field', 'field': ''}, 'Выберите поле')]
    for raw, message in cases:
        with pytest.raises(ValueError, match=message):
            jira.normalize_column_editor(raw)


def test_formula_fields_list():
    fields = jira.formula_fields(fake_jira.FIELDS + [{'id': 'status', 'name': 'Status', 'custom': False, 'schema': {'type': 'status'}},
                                                     {'id': 'duedate', 'name': 'Due', 'schema': {'type': 'date'}}, {'id': 'bad id'}, 'x'])
    by_id = {f['id']: f['format'] for f in fields}
    assert [f['id'] for f in fields[:4]] == ['timeoriginalestimate', 'timespent', 'timeestimate', 'status']
    assert by_id['status'] == 'text' and by_id['duedate'] == 'time' and by_id['customfield_10050'] == 'number'
    assert by_id['timespent'] == 'duration' and 'bad id' not in by_id


def test_user_formula_with_status_and_duplicates(fake, monkeypatch):
    """Формула пользователя (WITH, if, статусы) с «Σ по вложенным» и «Исключить дубликаты» — как в Structure."""
    monkeypatch.setattr(fake_jira, 'FORMULA', WITH_DUPLICATE)
    fm = jira.normalize_field_map({
        'remaining': {'field': 'timeestimate', 'unit': 'seconds', 'structure': {
            'editor': {'mode': 'formula', 'formula': USER_FORMULA, 'variables': USER_VARS, 'sum': True, 'distinct': True},
            'unit': 'ms', 'label': 'Σ Остаток (с обнулением)'}},
        'spent': {'field': 'timespent', 'unit': 'seconds', 'structure': {
            'editor': {'mode': 'field', 'field': 'timespent', 'format': 'duration', 'sum': True, 'distinct': True}, 'unit': 'ms'}},
        'estimate': {'field': 'timeoriginalestimate', 'unit': 'seconds', 'structure': {
            'editor': {'mode': 'field', 'field': 'timeoriginalestimate', 'format': 'duration', 'sum': True, 'distinct': False},
            'unit': 'ms'}}})
    items = jira.compute_snapshot(client_for(fake), 7, [{'serviceId': 1, 'issueKeys': 'ABC-1', 'includeSubtree': True}], fm)
    item = items[0]
    # ABC-1: 12 − 5 = 7 ч (в работе); ABC-2 готово → 0; ABC-3: 1 − 0 = 1 ч; повтор ABC-2 не считается
    assert item['remaining'] == 8.0
    assert item['spent'] == 6.0  # 5 + 1, дубликат ABC-2 исключён
    assert item['estimate'] == 19.0  # 12 + 3 + 1 + 3: без «Исключить дубликаты» ABC-2 посчитана дважды


def test_preview_with_editor_shows_variants(admin, fake, monkeypatch):
    connect(admin, fake)
    monkeypatch.setattr(fake_jira, 'FORMULA', WITH_DUPLICATE)
    body = {'structureId': 7, 'unit': 'ms', 'keys': 'ABC-1, ABC-4',
            'editor': {'mode': 'field', 'field': 'timespent', 'format': 'duration', 'sum': True, 'distinct': True}}
    data = admin.post('/api/projects/1/jira/structure-preview', json=body).get_json()
    first = data['values'][0]
    assert first['key'] == 'ABC-1' and first['hours'] == 6.0
    assert first['variants'] == {'own': 5 * 3600000, 'distinct': 6 * 3600000, 'all': 7 * 3600000}
    assert data['attribute']['params']['formula'] == 'SUM{\nvalue\n}'
    formula = dict(body, editor={'mode': 'formula', 'formula': USER_FORMULA, 'variables': USER_VARS, 'sum': True})
    assert admin.post('/api/projects/1/jira/structure-preview', json=formula).get_json()['values'][0]['hours'] == 8.0
    unbound = admin.post('/api/projects/1/jira/structure-preview', json=dict(body, editor={'mode': 'formula', 'formula': 'x'}))
    assert unbound.status_code == 502  # переменная без поля — ошибку формулы сообщает Structure
    bad = admin.post('/api/projects/1/jira/structure-preview', json=dict(body, editor={'mode': 'chart'}))
    assert bad.status_code == 400 and 'Тип колонки' in bad.get_json()['error']


def test_preview_variant_errors_do_not_hide_value(admin, fake, monkeypatch):
    connect(admin, fake)
    original = jira.JiraClient.structure_values

    def values(self, structure_id, rows, attributes, transformations=None, keep_format=False):
        if attributes[0]['id'] == 'sum':
            raise jira.JiraError('Unknown attribute: sum')  # вариант, которого нет в этой версии Structure
        if attributes[0]['id'] == 'status':
            raise jira.JiraError('Unknown attribute: status')
        return original(self, structure_id, rows, attributes, transformations, keep_format)
    monkeypatch.setattr(jira.JiraClient, 'structure_values', values)
    body = {'structureId': 7, 'unit': 'ms', 'keys': 'ABC-1',
            'editor': {'mode': 'field', 'field': 'timespent', 'format': 'duration', 'sum': True}}
    data = admin.post('/api/projects/1/jira/structure-preview', json=body).get_json()
    assert data['values'][0]['hours'] == 6.0 and data['values'][0]['variants']['all'] is None
    assert 'Σ с дубликатами' in data['warnings'][0]
    body['editor'] = {'mode': 'formula', 'formula': 'st', 'variables': {'st': {'field': 'status', 'format': 'text'}}}
    data = admin.post('/api/projects/1/jira/structure-preview', json=body).get_json()
    assert any('Переменная «st»: Structure не отдал значение поля' in w for w in data['warnings'])


def test_button_saves_editor_and_rebuilds_spec(admin, fake, flask_app):
    connect(admin, fake)
    fm = field_map(spent={'field': 'timespent', 'unit': 'seconds', 'structure': {
        'editor': {'mode': 'formula', 'formula': USER_FORMULA, 'variables': USER_VARS, 'sum': True, 'distinct': True},
        'attribute': {'id': 'ignored'}, 'unit': 'ms', 'label': 'Остаток'}})
    r = create_button(admin, fieldMap=fm)
    assert r.status_code == 200, r.get_json()
    saved = r.get_json()['fieldMap']['spent']['structure']
    assert saved['editor']['variables']['status'] == {'field': 'status', 'format': 'text'}
    assert saved['attribute']['params']['formula'].startswith('SUM{')  # спецификацию строит сервер, а не браузер
    snap = admin.post(f"/api/jira/buttons/{r.get_json()['id']}/snapshot", json={}).get_json()
    assert snap['items'][0]['spent'] == 8.0


def test_formula_fields_endpoint(admin, fake, make_user):
    connect(admin, fake)
    fields = admin.get('/api/projects/1/jira/formula-fields').get_json()
    assert fields[0]['id'] == 'timeoriginalestimate' and {'id': 'customfield_10060', 'name': 'Комментарий', 'format': 'text'} in fields
    assert make_user('viewer', project_id=1).get('/api/projects/1/jira/formula-fields').status_code == 403
    fake.configure(token='другой')
    assert admin.get('/api/projects/1/jira/formula-fields').status_code == 502


# ─── «После фильтрации» (After filtering): фильтр доски Structure ──

NOT_DONE = {'mode': 'jql', 'jql': 'status != "Готово"'}


def test_board_filter_normalization():
    assert jira.normalize_board_filter(None) is None and jira.normalize_board_filter({'mode': 'none'}) is None
    assert jira.normalize_board_filter(NOT_DONE) == {'mode': 'jql', 'jql': 'status != "Готово"', 'showSubItems': False}
    assert jira.board_transformations(jira.normalize_board_filter(dict(NOT_DONE, showSubItems=True))) == [
        {'module': 'com.almworks.jira.structure:filter-jql', 'params': {'jql': 'status != "Готово"', 'showSubItems': True}}]
    payload = json.dumps({'requests': [{'forestSpec': {'structureId': 7, 'transformations': [
        {'module': 'com.almworks.jira.structure:filter-sjql', 'params': {'query': 'child of resolved'}}]}, 'rows': [1]}]})
    pasted = jira.normalize_board_filter({'mode': 'json', 'transformations': payload})
    assert pasted == {'mode': 'json', 'transformations': [
        {'module': 'com.almworks.jira.structure:filter-sjql', 'params': {'query': 'child of resolved'}}]}
    assert jira.board_transformations(pasted) == pasted['transformations']
    assert jira.extract_transformations([{'module': 'a:b'}]) == [{'module': 'a:b'}]
    assert jira.extract_transformations({'module': 'a:b', 'params': {}}) == [{'module': 'a:b', 'params': {}}]
    assert jira.extract_transformations([[1], {'spec': {'transformations': [{'module': 'a:b'}]}}]) == [{'module': 'a:b'}]
    assert jira.board_transformations(None) is None
    cases = [('x', 'Фильтр доски'), ({'mode': 'chart'}, 'Фильтр доски'), ({'mode': 'jql', 'jql': ' '}, 'Введите JQL'),
             ({'mode': 'jql', 'jql': 'a' * 5001}, 'длинный'), ({'mode': 'json', 'transformations': '{не json'}, 'JSON из Structure'),
             ({'mode': 'json', 'transformations': {'structureId': 7}}, 'не найдены'),
             ({'mode': 'json', 'transformations': [{'module': 'плохой'}]}, 'Некорректная трансформация'),
             ({'mode': 'json', 'transformations': [{'module': 'a:b', 'params': []}]}, 'Некорректная трансформация'),
             ({'mode': 'json', 'transformations': [{'module': 'a:b'}] * 11}, 'Слишком много'),
             ({'mode': 'json', 'transformations': [{'module': 'a:b', 'params': {'x': 'y' * 20001}}]}, 'слишком большие')]
    for raw, message in cases:
        with pytest.raises(ValueError, match=message):
            jira.normalize_board_filter(raw)


def filtered_map():
    return jira.normalize_field_map({
        'remaining': {'field': 'timeestimate', 'unit': 'seconds'},
        'spent': {'field': 'timespent', 'unit': 'seconds', 'structure': {
            'editor': {'mode': 'field', 'field': 'timespent', 'format': 'duration', 'sum': True}, 'unit': 'ms',
            'afterFiltering': True}},
        'estimate': {'field': 'timeoriginalestimate', 'unit': 'seconds', 'structure': {
            'editor': {'mode': 'field', 'field': 'timeoriginalestimate', 'format': 'duration', 'sum': True}, 'unit': 'ms'}}})


def test_after_filtering_uses_board_filter(fake):
    """«Σ Time Spent» с After filtering — только видимые с фильтром доски задачи; «Σ Original Estimate» без неё — все."""
    tf = jira.board_transformations(jira.normalize_board_filter(NOT_DONE))
    items = jira.compute_snapshot(client_for(fake), 7, [{'serviceId': 1, 'issueKeys': 'ABC-1', 'includeSubtree': True},
                                                        {'serviceId': 2, 'issueKeys': 'ABC-4', 'includeSubtree': False}],
                                  filtered_map(), tf)
    first, second = items
    assert first['spent'] == 5.0  # ABC-2 (готово) скрыта фильтром — её 1 ч не входит
    assert first['estimate'] == 16.0  # без «После фильтрации» — по всей структуре: 12 + 3 + 1
    assert second['spent'] == 0.0 and second['estimate'] == 10.0  # ABC-4 видна: открыта, затрачено 0
    specs = fake.state['forest_specs']
    assert {'structureId': 7} in specs and {'structureId': 7, 'transformations': tf} in specs
    assert any(r['forestSpec'] == {'structureId': 7, 'transformations': tf}
               for b in fake.state['value_bodies'] for r in b['requests'])


def test_epic_hidden_by_board_filter_counts_as_zero(fake):
    tf = jira.board_transformations(jira.normalize_board_filter({'mode': 'jql', 'jql': 'status = "В работе"'}))
    item = jira.compute_snapshot(client_for(fake), 7, [{'serviceId': 2, 'issueKeys': 'ABC-4', 'includeSubtree': False}],
                                 filtered_map(), tf)[0]
    assert item['spent'] == 0.0 and item['estimate'] == 10.0
    assert any('скрыта фильтром доски' in w for w in item['warnings'])


def test_after_filtering_without_board_filter_warns(fake):
    item = jira.compute_snapshot(client_for(fake), 7, [{'serviceId': 1, 'issueKeys': 'ABC-1', 'includeSubtree': True}],
                                 filtered_map())[0]
    assert item['spent'] == 6.0 and any('фильтр доски у кнопки не задан' in w for w in item['warnings'])


def test_preview_with_board_filter(admin, fake):
    connect(admin, fake)
    body = {'structureId': 7, 'unit': 'ms', 'keys': 'ABC-1, ABC-4', 'afterFiltering': True,
            'boardFilter': {'mode': 'jql', 'jql': 'status = "В работе"'},
            'editor': {'mode': 'field', 'field': 'timespent', 'format': 'duration', 'sum': True}}
    data = admin.post('/api/projects/1/jira/structure-preview', json=body).get_json()
    first = data['values'][0]
    assert first['hours'] == 5.0  # с фильтром: ABC-1 (5 ч) и ABC-3 (0)
    assert first['variants']['distinct'] == 6 * 3600000 and first['variants']['filtered'] == 5 * 3600000
    assert data['values'][1]['raw'] is None and 'ABC-4: задача скрыта фильтром доски' in data['warnings']
    plain = admin.post('/api/projects/1/jira/structure-preview', json=dict(body, afterFiltering=False)).get_json()
    assert plain['values'][0]['hours'] == 6.0
    no_filter = admin.post('/api/projects/1/jira/structure-preview', json=dict(body, boardFilter=None)).get_json()
    assert no_filter['values'][0]['hours'] == 6.0 and 'фильтр доски у кнопки не задан' in ' '.join(no_filter['warnings'])
    bad = admin.post('/api/projects/1/jira/structure-preview', json=dict(body, boardFilter={'mode': 'jql', 'jql': ''}))
    assert bad.status_code == 400 and 'JQL' in bad.get_json()['error']
    broken = admin.post('/api/projects/1/jira/structure-preview',
                        json=dict(body, boardFilter={'mode': 'json', 'transformations': [{'module': 'x:unknown'}]}))
    assert broken.status_code == 502  # Structure не знает такую трансформацию — понятная ошибка


def test_button_keeps_board_filter(admin, fake, flask_app):
    connect(admin, fake)
    fm = field_map(spent={'field': 'timespent', 'unit': 'seconds', 'structure': {
        'editor': {'mode': 'field', 'field': 'timespent', 'format': 'duration', 'sum': True}, 'unit': 'ms', 'afterFiltering': True}})
    r = create_button(admin, fieldMap=fm, boardFilter=NOT_DONE)
    assert r.status_code == 200, r.get_json()
    button = r.get_json()
    assert button['boardFilter'] == {'mode': 'jql', 'jql': 'status != "Готово"', 'showSubItems': False}
    assert button['fieldMap']['spent']['structure']['afterFiltering'] is True
    snap = admin.post(f"/api/jira/buttons/{button['id']}/snapshot", json={}).get_json()
    assert snap['items'][0]['spent'] == 5.0
    with flask_app.app_context():
        db = app_module.get_db()
        row = db.execute('SELECT * FROM jira_snapshot_buttons WHERE id = ?', (button['id'],)).fetchone()
        status, _message = app_module.run_autofill(db, row, '2026-10-30', 'Автозаполнение')
        db.commit()
        spent = db.execute("SELECT spent FROM entries WHERE service_id = 1 AND date = '2026-10-30'").fetchone()[0]
    assert status == 'ok' and spent == 5.0
    updated = admin.put(f"/api/jira/buttons/{button['id']}", json={
        'name': 'Срез', 'structureId': 7, 'mappings': [{'serviceId': 1, 'issueKeys': 'ABC-1', 'includeSubtree': True}],
        'fieldMap': fm, 'boardFilter': {'mode': 'none'}}).get_json()
    assert updated['boardFilter'] is None
    assert create_button(admin, structureId=None, boardFilter=NOT_DONE).get_json()['error'] == \
        'Фильтр доски задаётся только у кнопки со структурой'
    assert create_button(admin, boardFilter={'mode': 'jql'}).status_code == 400
    with flask_app.app_context():
        assert app_module._button_board_filter({'board_filter': '{"mode": "jql"}'}) is None  # повреждённая запись


# ─── Эпик стоит в структуре несколько раз: раздел структуры (1.9.2) ──

# ABC-1 стоит в разделе «Договор развития» (под ним только ABC-2) и в разделе «4.1» (ABC-2, ABC-9, папка с ABC-3)
TWO_SECTIONS = '20:0:5/7,21:1:101,22:2:102,30:0:5/8,1:1:101,2:2:102,6:2:105,3:2:5/9,4:3:103,5:1:104'
IN_41 = [{'item': '5/8', 'title': '4.1'}]


def estimate_map():
    return jira.normalize_field_map({
        'remaining': {'field': 'timeestimate', 'unit': 'seconds'},
        'spent': {'field': '', 'structure': {'editor': {'mode': 'field', 'field': 'timeoriginalestimate', 'format': 'duration',
                                                         'sum': True}, 'unit': 'ms'}},
        'estimate': {'field': 'timeoriginalestimate', 'unit': 'seconds'}})


def test_epic_in_two_sections_uses_chosen_section(fake, monkeypatch):
    monkeypatch.setattr(fake_jira, 'FORMULA', TWO_SECTIONS)
    mappings = [{'serviceId': 1, 'issueKeys': 'ABC-1', 'includeSubtree': True}]
    first = jira.compute_snapshot(client_for(fake), 7, mappings, estimate_map())[0]
    # без раздела — первая строка (раздел «Договор развития»): ABC-1 + ABC-2 = 12 + 3
    assert first['spent'] == 15.0 and first['estimate'] == 15.0
    assert any('стоит в структуре 2 раз(а)' in w for w in first['warnings'])
    chosen = jira.compute_snapshot(client_for(fake), 7, mappings, estimate_map(), scope=IN_41)[0]
    # раздел «4.1»: и колонка Structure, и сумма поля Jira — по поддереву этой строки: 12 + 3 + 1
    assert chosen['spent'] == 16.0 and chosen['estimate'] == 16.0
    assert not any('раз(а)' in w for w in chosen['warnings'])
    missing = jira.compute_snapshot(client_for(fake), 7, mappings, estimate_map(), scope=[{'item': '5/404', 'title': ''}])[0]
    assert any('в выбранных разделах структуры задачи нет' in w for w in missing['warnings'])


# Как у SRM-4687: эпик ABC-1 стоит глубоко в «Договоре развития» (под ABC-4) и прямо в «4.1»; в разделах отмечен сам
# эпик под «4.1» (строка 1), а не папка над ним
NESTED = '20:0:5/7,23:1:104,21:2:101,22:3:102,30:0:5/8,1:1:101,2:2:102,6:2:105,3:2:5/9,4:3:103,5:1:106'
EPIC_IN_41 = [{'item': '101', 'title': '4.1 → ABC-1', 'row': 1}]


@pytest.mark.parametrize('formula, scope, spent', [
    (NESTED, EPIC_IN_41, 16.0),
    (NESTED, [{'item': '101', 'title': 'ABC-1'}], 16.0),  # сохранён до 1.9.3 без строки — эпик верхних уровней
    (NESTED, [{'item': '101', 'title': 'ABC-1', 'row': 777}], 16.0),  # строку пересоздали
    (NESTED, [{'item': '104', 'title': 'ABC-4', 'row': 23}], 15.0),  # отмечен раздел над эпиком
    (TWO_SECTIONS, EPIC_IN_41, 16.0),  # эпик на втором уровне в обоих разделах — различает строка
    (TWO_SECTIONS, [{'item': '101', 'title': 'ABC-1', 'row': 21}], 15.0),
])
def test_epic_row_chosen_as_section(fake, monkeypatch, formula, scope, spent):
    monkeypatch.setattr(fake_jira, 'FORMULA', formula)
    mappings = [{'serviceId': 1, 'issueKeys': 'ABC-1', 'includeSubtree': True}]
    got = jira.compute_snapshot(client_for(fake), 7, mappings, estimate_map(), scope=scope)[0]
    # и колонка Structure (spent), и сумма поля Jira (estimate) — по поддереву выбранной строки
    assert (got['spent'], got['estimate']) == (spent, spent)
    assert not any('в выбранных разделах' in w or 'раз(а)' in w for w in got['warnings'])


def test_preview_and_snapshot_take_epic_row_from_section(admin, fake, monkeypatch):
    connect(admin, fake)
    monkeypatch.setattr(fake_jira, 'FORMULA', NESTED)
    sections = admin.get('/api/projects/1/jira/structure-sections?structureId=7').get_json()
    assert [(x['item'], x['rowId'], x['parentTitle']) for x in sections] == [
        ('5/7', 20, None), ('104', 23, 'Договор развития'), ('5/8', 30, None), ('101', 1, '4.1')]
    body = {'structureId': 7, 'unit': 'ms', 'keys': 'ABC-1', 'structureScope': EPIC_IN_41,
            'editor': {'mode': 'field', 'field': 'timeoriginalestimate', 'format': 'duration', 'sum': True}}
    data = admin.post('/api/projects/1/jira/structure-preview', json=body).get_json()
    value = data['values'][0]
    assert value['hours'] == 16.0 and value['rowId'] == 1 and not data['warnings']
    assert [(p['path'], p['chosen']) for p in value['places']] == [(['Договор развития', 'ABC-4'], False), (['4.1'], True)]
    button = create_button(admin, fieldMap=estimate_map(), structureScope=EPIC_IN_41).get_json()
    assert button['structureScope'] == EPIC_IN_41
    snap = admin.post(f"/api/jira/buttons/{button['id']}/snapshot", json={}).get_json()
    assert snap['items'][0]['spent'] == 16.0 and snap['items'][0]['estimate'] == 16.0


def test_preview_lists_all_places_and_variables(admin, fake, monkeypatch):
    connect(admin, fake)
    monkeypatch.setattr(fake_jira, 'FORMULA', TWO_SECTIONS)
    body = {'structureId': 7, 'unit': 'ms', 'keys': 'ABC-1',
            'editor': {'mode': 'formula', 'formula': USER_FORMULA, 'variables': USER_VARS, 'sum': True}}
    data = admin.post('/api/projects/1/jira/structure-preview', json=body).get_json()
    first = data['values'][0]
    assert [(p['path'], p['chosen']) for p in first['places']] == [(['Договор развития'], True), (['4.1'], False)]
    assert [p['raw'] / 3600000 for p in first['places']] == [7.0, 8.0]  # у каждой строки своё поддерево
    assert first['vars'] == {'orig': 12 * 3600000, 'spent': 5 * 3600000, 'status': 'В работе'}
    assert any('стоит в структуре 2 раз(а)' in w for w in data['warnings'])
    scoped = admin.post('/api/projects/1/jira/structure-preview', json=dict(body, structureScope=IN_41)).get_json()
    assert scoped['values'][0]['hours'] == 8.0 and [p['chosen'] for p in scoped['values'][0]['places']] == [False, True]
    bad = admin.post('/api/projects/1/jira/structure-preview', json=dict(body, structureScope='x'))
    assert bad.status_code == 400


def test_structure_sections_endpoint(admin, fake, monkeypatch, make_user):
    connect(admin, fake)
    monkeypatch.setattr(fake_jira, 'FORMULA', TWO_SECTIONS)
    sections = admin.get('/api/projects/1/jira/structure-sections?structureId=7').get_json()
    assert [(x['item'], x['depth'], x['title']) for x in sections] == [
        ('5/7', 0, 'Договор развития'), ('101', 1, 'ABC-1'), ('5/8', 0, '4.1'), ('101', 1, 'ABC-1')]
    assert sections[2]['rows'] == 6 and sections[0]['issue'] is False
    assert admin.get('/api/projects/1/jira/structure-sections').status_code == 400
    assert make_user('viewer', project_id=1).get('/api/projects/1/jira/structure-sections?structureId=7').status_code == 403
    fake.configure(token='другой')
    assert admin.get('/api/projects/1/jira/structure-sections?structureId=7').status_code == 502


def test_button_keeps_structure_scope(admin, fake, flask_app, monkeypatch):
    connect(admin, fake)
    monkeypatch.setattr(fake_jira, 'FORMULA', TWO_SECTIONS)
    r = create_button(admin, fieldMap=estimate_map(), structureScope=IN_41)
    assert r.status_code == 200, r.get_json()
    button = r.get_json()
    assert button['structureScope'] == IN_41
    snap = admin.post(f"/api/jira/buttons/{button['id']}/snapshot", json={}).get_json()
    assert snap['items'][0]['spent'] == 16.0
    assert create_button(admin, structureScope=[{'item': 'a:b'}]).status_code == 400
    assert 'только у кнопки со структурой' in create_button(admin, structureId=None, structureScope=IN_41).get_json()['error']
    with flask_app.app_context():
        assert app_module._button_scope({'structure_scope': '"x"'}) is None  # повреждённая запись
        assert app_module._button_scope({'structure_scope': None}) is None


def test_row_titles_tolerate_errors(fake, monkeypatch):
    client = client_for(fake)
    monkeypatch.setattr(client, 'structure_values', lambda *a, **k: (_ for _ in ()).throw(jira.JiraError('нет')))
    assert jira.row_titles(client, 7, [1]) == {} and jira.row_titles(client, 7, []) == {}


# ─── Показатели задач в составе эпика (1.9.4) ────────────────────

def structure_only_map():
    """Как в АИСУЗ: показатели из колонок Structure, поле Jira для состава задач не задано."""
    return jira.normalize_field_map({
        'remaining': {'field': '', 'structure': {'editor': {'mode': 'formula', 'formula': USER_FORMULA, 'variables': USER_VARS,
                                                            'sum': True}, 'unit': 'ms'}},
        'spent': {'field': '', 'structure': {'editor': {'mode': 'field', 'field': 'timespent', 'format': 'duration',
                                                        'sum': True}, 'unit': 'ms'}},
        'estimate': {'field': '', 'structure': {'editor': {'mode': 'field', 'field': 'timeoriginalestimate',
                                                           'format': 'duration', 'sum': True}, 'unit': 'ms'}}})


def test_task_values_from_structure_columns(fake):
    mappings = [{'serviceId': 1, 'issueKeys': 'ABC-1', 'includeSubtree': True}]
    item = jira.compute_snapshot(client_for(fake), 7, mappings, structure_only_map())[0]
    assert (item['estimate'], item['spent']) == (16.0, 6.0)  # колонка у эпика — Σ по поддереву
    issues = {i['key']: i for i in item['issues']}
    # у каждой задачи — та же колонка по самой задаче: оценка, затрачено и формула остатка (rest < 0 или статус «Готово» → 0)
    assert {k: (i['estimate'], i['spent'], i['remaining']) for k, i in issues.items()} == {
        'ABC-1': (12.0, 5.0, 7.0), 'ABC-2': (3.0, 1.0, 0.0), 'ABC-3': (1.0, None, 1.0)}
    assert sum(i['estimate'] for i in issues.values()) == item['estimate']  # объём состава сходится с колонкой
    assert issues['ABC-3']['problems'] == [] and issues['ABC-1']['problems'] == []


def test_task_values_from_structure_in_chosen_section(fake, monkeypatch):
    monkeypatch.setattr(fake_jira, 'FORMULA', NESTED)
    mappings = [{'serviceId': 1, 'issueKeys': 'ABC-1', 'includeSubtree': True}]
    item = jira.compute_snapshot(client_for(fake), 7, mappings, structure_only_map(), scope=EPIC_IN_41)[0]
    assert sorted(i['key'] for i in item['issues']) == ['ABC-1', 'ABC-2', 'ABC-3']
    assert [i['estimate'] for i in sorted(item['issues'], key=lambda i: i['key'])] == [12.0, 3.0, 1.0]


def test_task_values_need_own_variant_of_column(fake):
    assert jira.own_attribute({'attribute': {'id': 'sum', 'params': {'attribute': {'id': 'timespent'}}}}) == {'id': 'timespent'}
    assert jira.own_attribute({'attribute': {'id': 'timespent'}}) == {'id': 'timespent'}
    assert jira.own_attribute({'attribute': {'id': 'expr', 'params': {'formula': 'SUM#all{ a }'}}})['params']['formula'] == 'a'
    assert jira.own_attribute({'attribute': {'id': 'expr', 'params': {'formula': 'a + 1'}}})['params']['formula'] == 'a + 1'
    assert jira.own_attribute({'attribute': {'id': 'expr', 'params': {'formula': 'a + SUM{b}'}}}) is None
    assert jira.own_attribute({'attribute': {'id': 'sum', 'params': {}}}) is None
    fm = jira.normalize_field_map({'remaining': {'field': 'timeestimate', 'unit': 'seconds'},
                                   'spent': {'field': '', 'structure': {'attribute': {'id': 'expr', 'params': {
                                       'formula': 'a + SUM{b}', 'variables': {'a': {'id': 'timespent'}, 'b': {'id': 'timespent'}}}},
                                       'unit': 'ms'}},
                                   'estimate': {'field': '', 'unit': 'seconds'}})
    item = jira.compute_snapshot(client_for(fake), 7, [{'serviceId': 1, 'issueKeys': 'ABC-1', 'includeSubtree': True}], fm)[0]
    assert all(i['spent'] is None for i in item['issues'])  # сумму из готовой формулы не убрать — по задачам пусто


def test_task_values_tolerate_structure_errors(fake, monkeypatch):
    client = client_for(fake)
    original = client.structure_values
    calls = []

    def flaky(structure_id, rows, attributes, *a, **k):
        calls.append(len(rows))
        if len(calls) > 1:  # колонки эпика посчитаны, значения задач — нет
            raise jira.JiraError('Structure недоступна')
        return original(structure_id, rows, attributes, *a, **k)
    monkeypatch.setattr(client, 'structure_values', flaky)
    item = jira.compute_snapshot(client, 7, [{'serviceId': 1, 'issueKeys': 'ABC-1', 'includeSubtree': True}],
                                 structure_only_map())[0]
    assert item['estimate'] == 16.0 and all(i['estimate'] is None for i in item['issues'])
    assert any('Показатели задач из колонок Structure не получены' in w for w in item['warnings'])


def test_snapshot_stores_task_values_from_structure(admin, fake):
    connect(admin, fake)
    button = create_button(admin, fieldMap=structure_only_map()).get_json()
    assert admin.post(f"/api/jira/buttons/{button['id']}/snapshot", json={}).status_code == 200
    issues = {i['key']: i for i in admin.get('/api/services/1/jira/issues').get_json()['issues']}
    assert issues['ABC-2']['estimate'] == 3.0 and issues['ABC-1']['remaining'] == 7.0
    latest = admin.get('/api/projects/1/jira/composition').get_json()['latest']['1']
    assert latest['scope'] == 16.0 and latest['done'] == 3.0  # объём и выполнено — не 0, как раньше


def test_jira_time_fields_always_in_seconds(fake):
    fm = jira.normalize_field_map({'remaining': {'field': 'aggregatetimeestimate', 'unit': 'hours'},
                                   'spent': {'field': 'timespent', 'unit': 'hours'},
                                   'estimate': {'field': 'customfield_10050', 'unit': 'hours'}})
    # учёт времени Jira (и Σ-поля) всегда в секундах — даже если раньше выбрали «часы»; своё поле — как выбрано
    assert [fm[m]['unit'] for m in jira.METRICS] == ['seconds', 'seconds', 'hours']
    fields = {f['id']: f for f in client_for(fake).list_fields()}
    assert fields['aggregatetimeoriginalestimate'] == {'id': 'aggregatetimeoriginalestimate', 'name': 'Σ Первоначальная оценка',
                                                       'custom': False, 'time': True}


@pytest.mark.parametrize('subtask, estimate, own', [(True, 18.0, 12.0), (False, 24.0, 18.0)])
def test_aggregate_fields_do_not_count_subtasks_twice(fake, monkeypatch, subtask, estimate, own):
    # Σ-оценка ABC-1 = её 12 ч + подзадача ABC-6 (6 ч); обе в составе эпика
    monkeypatch.setitem(fake_jira.ISSUES, 101, dict(fake_jira.ISSUES[101], aggregatetimeoriginalestimate=18 * 3600))
    monkeypatch.setitem(fake_jira.ISSUES, 107, dict(fake_jira.ISSUES[107], aggregatetimeoriginalestimate=6 * 3600,
                                                    subtask=subtask))
    monkeypatch.setattr(fake_jira, 'FORMULA', '1:0:101,2:1:107')
    fm = jira.normalize_field_map({'remaining': {'field': '', 'unit': 'seconds'}, 'spent': {'field': '', 'unit': 'seconds'},
                                   'estimate': {'field': 'aggregatetimeoriginalestimate', 'unit': 'hours'}})
    item = jira.compute_snapshot(client_for(fake), 7, [{'serviceId': 1, 'issueKeys': 'ABC-1', 'includeSubtree': True}], fm)[0]
    issues = {i['key']: i['estimate'] for i in item['issues']}
    # подзадача: у родителя остаётся своя часть; не подзадача (дочерняя задача эпика) — Σ-поле её не включает
    assert item['estimate'] == estimate and issues == {'ABC-1': own, 'ABC-6': 6.0}
