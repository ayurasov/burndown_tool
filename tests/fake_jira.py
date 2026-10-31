"""Фейковая Jira (Server/DC и Cloud) с плагином Structure для тестов связки.

Структура 7:
    ABC-1 (101)            остаток 10 ч, затрачено 5 ч, оценка 12 ч
      ABC-2 (102)          2 ч / 1 ч / 3 ч
      ABC-9 (105)          скрыта от пользователя (не возвращается поиском)
      [папка]
        ABC-3 (103)        0,5 ч / — / 1 ч
    ABC-4 (104)            10 ч / 0 / 10 ч  (соседний корень, не входит в поддерево ABC-1)
ABC-6 (107) — подзадача ABC-1 вне структуры: 4 ч / 2 ч / 6 ч (с ней ABC-1 даёт 14 / 7 / 18).
ABC-5 (106) — вне структуры. customfield_10050 — числовое поле «Трудозатраты, ч» (в часах).
Статусы: ABC-1, ABC-3, ABC-6 — в работе; ABC-2 — готово (с ненулевым остатком); остальные — открыты.
"""

import base64
import re
import threading

from flask import Flask, jsonify, request
from werkzeug.serving import make_server

H = 3600
ISSUES = {
    101: {'key': 'ABC-1', 'category': 'indeterminate', 'timeestimate': 10 * H, 'timespent': 5 * H, 'timeoriginalestimate': 12 * H,
          'customfield_10050': 20, 'customfield_10060': 'текст'},
    102: {'key': 'ABC-2', 'category': 'done', 'summary': 'Интеграция с ЕИС', 'timeestimate': 2 * H, 'timespent': 1 * H, 'timeoriginalestimate': 3 * H},
    103: {'key': 'ABC-3', 'category': 'indeterminate', 'timeestimate': H // 2, 'timespent': None, 'timeoriginalestimate': 1 * H},
    104: {'key': 'ABC-4', 'timeestimate': 10 * H, 'timespent': 0, 'timeoriginalestimate': 10 * H},
    105: {'key': 'ABC-9', 'hidden': True, 'timeestimate': 100 * H},
    106: {'key': 'ABC-5', 'timeestimate': 1 * H, 'timespent': 1 * H, 'timeoriginalestimate': 2 * H},
    107: {'key': 'ABC-6', 'category': 'indeterminate', 'parent': 'ABC-1', 'timeestimate': 4 * H, 'timespent': 2 * H, 'timeoriginalestimate': 6 * H,
          'customfield_10050': 5.5},
}
FIELDS = [
    {'id': 'summary', 'name': 'Тема', 'custom': False, 'schema': {'type': 'string'}},
    {'id': 'timeestimate', 'name': 'Remaining Estimate', 'custom': False, 'schema': {'type': 'number'}},
    {'id': 'timespent', 'name': 'Time Spent', 'custom': False, 'schema': {'type': 'number'}},
    {'id': 'timeoriginalestimate', 'name': 'Original Estimate', 'custom': False, 'schema': {'type': 'number'}},
    {'id': 'customfield_10050', 'name': 'Трудозатраты, ч', 'custom': True, 'schema': {'type': 'number'}},
    {'id': 'customfield_10060', 'name': 'Комментарий', 'custom': True, 'schema': {'type': 'string'}},
]
FORMULA = '1:0:101,2:1:102,6:1:105,3:1:5/9,4:2:103,5:0:104'
STRUCTURES = [{'id': 7, 'name': 'Релиз 2026'}, {'id': 8, 'name': 'Бэклог'}]


STATUS_NAMES = {'new': 'Открыта', 'indeterminate': 'В работе', 'done': 'Готово'}
STATUS_IDS = {'new': '1', 'indeterminate': '3', 'done': '10001'}
CREATED_DEFAULT = '2026-09-01T10:00:00.000+0300'

# Журнал изменений (для восстановления истории):
#   ABC-3 создана 01.10; 20.10 — взята в работу, остаток 2 ч → 0,5 ч
#   ABC-2 создана 01.10; 25.10 — закрыта, затрачено 0 → 1 ч
#   ABC-6 создана 22.10 (раньше её не было)
HISTORY = {
    103: ('2026-10-01T10:00:00.000+0300', [
        {'created': '2026-10-20T12:00:00.000+0300', 'items': [
            {'field': 'status', 'fieldtype': 'jira', 'from': '1', 'fromString': 'Открыта', 'to': '3', 'toString': 'В работе'},
            {'field': 'timeestimate', 'fieldtype': 'jira', 'from': '7200', 'fromString': '7200', 'to': '1800', 'toString': '1800'}]}]),
    102: ('2026-10-01T10:00:00.000+0300', [
        {'created': '2026-10-25T15:00:00.000+0300', 'items': [
            {'field': 'status', 'fieldtype': 'jira', 'from': '3', 'fromString': 'В работе', 'to': '10001', 'toString': 'Готово'},
            {'field': 'timespent', 'fieldtype': 'jira', 'from': None, 'fromString': None, 'to': '3600', 'toString': '3600'}]}]),
    107: ('2026-10-22T09:00:00.000+0300', []),
}


def _field(data, name):
    """Поле задачи в формате Jira REST: parent и status — объекты."""
    if name == 'parent':
        return {'key': data['parent']} if data.get('parent') else None
    if name == 'status':
        category = data.get('category', 'new')
        return {'id': STATUS_IDS[category], 'name': data.get('status') or STATUS_NAMES[category],
                'statusCategory': {'key': category}}
    if name == 'created':
        return _history(data)[0]
    if name == 'summary':
        return data.get('summary') or f"Задача {data['key']}"
    return data.get(name)


def _history(data):
    for issue_id, item in ISSUES.items():
        if item is data:
            return HISTORY.get(issue_id, (CREATED_DEFAULT, []))
    return CREATED_DEFAULT, []


def _issue_json(issue_id, fields, changelog=False, truncate=False):
    data = ISSUES[issue_id]
    out = {'id': str(issue_id), 'key': data['key'], 'fields': {f: _field(data, f) for f in fields if f}}
    if changelog:
        histories = _history(data)[1]
        out['changelog'] = {'histories': [] if truncate else histories, 'total': len(histories),
                            'maxResults': 0 if truncate else len(histories)}
    return out


def create_app(state):
    app = Flask('fake_jira')

    @app.before_request
    def check_auth():
        state['requests'].append((request.method, request.path))
        auth = request.headers.get('Authorization', '')
        if state['mode'] == 'server':
            ok = auth == 'Bearer ' + state['token']
        else:
            expected = base64.b64encode(f"{state['login']}:{state['token']}".encode()).decode()
            ok = auth == 'Basic ' + expected
        if state.get('captcha'):  # Jira Server/DC после нескольких неудачных входов по паролю
            return jsonify({'errorMessages': ['Forbidden']}), 403, {
                'X-Authentication-Denied-Reason': 'CAPTCHA_CHALLENGE; login-url=/login.jsp'}
        if not ok:
            return jsonify({'errorMessages': ['Unauthorized']}), 401
        api = '/rest/api/3/' if state['mode'] == 'cloud' else '/rest/api/2/'
        if request.path.startswith('/rest/api/') and not request.path.startswith(api):
            return jsonify({'errorMessages': ['wrong api version']}), 404
        if request.path.startswith('/rest/structure/') and not state['structure']:
            return 'Not Found', 404

    @app.get('/rest/api/<v>/myself')
    def myself(v):
        return jsonify({'name': state['login'] or 'pat-user', 'displayName': 'Тестовый Пользователь'})

    @app.get('/rest/api/<v>/issue/<key>')
    def issue(v, key):
        fields = (request.args.get('fields') or '').split(',')
        for issue_id, data in ISSUES.items():
            if data['key'] == key and not data.get('hidden'):
                return jsonify(_issue_json(issue_id, fields, changelog=request.args.get('expand') == 'changelog'))
        return jsonify({'errorMessages': ['Issue does not exist or you do not have permission to see it.']}), 404

    def search():
        body = request.get_json()
        parents = re.match(r'parent in \(([^)]*)\)', body['jql'])
        by_keys = re.match(r'key in \(([^)]*)\)', body['jql'])
        if parents:
            keys = {k.strip() for k in parents.group(1).split(',')}
            ids = [i for i, d in ISSUES.items() if d.get('parent') in keys]
        elif by_keys:
            keys = {k.strip() for k in by_keys.group(1).split(',')}
            ids = [i for i, d in ISSUES.items() if d['key'] in keys]
            visible = {ISSUES[i]['key'] for i in ids if not ISSUES[i].get('hidden')}
            # как настоящая Jira: ключ, которого нет или который не виден, — ошибка всего запроса,
            # если не отключена проверка JQL (validateQuery: false, только Server/DC)
            if keys - visible and body.get('validateQuery') is not False:
                bad = sorted(keys - visible)[0]
                return jsonify({'errorMessages': [f"An issue with key '{bad}' does not exist for field 'key'."]}), 400
        else:
            ids = [int(x) for x in re.findall(r'\d+', body['jql'])]
        expand = body.get('expand') or []
        changelog = 'changelog' in (expand if isinstance(expand, list) else expand.split(','))
        # Cloud отдаёт в поиске урезанный журнал — полный читается через /issue/{key}/changelog
        issues = [_issue_json(i, body['fields'], changelog, truncate=state['mode'] == 'cloud')
                  for i in ids if i in ISSUES and not ISSUES[i].get('hidden')]
        state['search_bodies'].append(body)
        # постранично, как Jira: startAt и maxResults (Server/DC), nextPageToken (Cloud)
        start = int(body.get('nextPageToken') or body.get('startAt') or 0)
        size = int(body.get('maxResults') or 50)
        page = issues[start:start + size]
        more = start + size < len(issues)
        return jsonify({'issues': page, 'total': len(issues), 'startAt': start,
                        **({'nextPageToken': str(start + size)} if more else {})})

    @app.post('/rest/api/2/search')
    def search_server():
        return search()

    @app.post('/rest/api/3/search/jql')
    def search_cloud():
        return search()

    @app.get('/rest/api/<v>/field')
    def fields(v):
        return jsonify(FIELDS)

    @app.get('/rest/api/<v>/status')
    def statuses(v):
        return jsonify([{'id': STATUS_IDS[c], 'name': STATUS_NAMES[c], 'statusCategory': {'key': c}}
                        for c in STATUS_IDS])

    @app.get('/rest/api/3/issue/<key>/changelog')
    def changelog(key):
        start, size = int(request.args.get('startAt', 0)), int(request.args.get('maxResults', 100))
        for data in ISSUES.values():
            if data['key'] == key:
                histories = _history(data)[1]
                page = histories[start:start + size]
                return jsonify({'values': page, 'startAt': start, 'total': len(histories),
                                'isLast': start + len(page) >= len(histories)})
        return jsonify({'errorMessages': ['not found']}), 404

    @app.get('/rest/structure/2.0/structure')
    def structures():
        return jsonify({'structures': STRUCTURES})

    @app.post('/rest/structure/2.0/forest/latest')
    def forest():
        sid = request.get_json().get('structureId')
        if sid != 7:
            return jsonify({'message': 'structure not found'}), 404
        return jsonify({'spec': {'structureId': 7}, 'formula': FORMULA,
                        'itemTypes': {'5': 'com.almworks.jira.structure:type-folder'},
                        'version': {'signature': 1, 'version': 3}})

    return app


class FakeJira:
    def __init__(self, mode='server', login='', token='secret-pat', structure=True):
        self.state = {'mode': mode, 'login': login, 'token': token, 'structure': structure,
                      'requests': [], 'search_bodies': []}
        self.server = make_server('127.0.0.1', 0, create_app(self.state), threaded=True)
        self.url = f'http://127.0.0.1:{self.server.server_port}'
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def configure(self, **kw):
        self.state.update(kw)

    def stop(self):
        self.server.shutdown()
        self.thread.join()
