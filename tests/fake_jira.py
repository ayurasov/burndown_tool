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


def _field(data, name):
    """Поле задачи в формате Jira REST: parent и status — объекты."""
    if name == 'parent':
        return {'key': data['parent']} if data.get('parent') else None
    if name == 'status':
        category = data.get('category', 'new')
        return {'name': data.get('status') or STATUS_NAMES[category], 'statusCategory': {'key': category}}
    if name == 'summary':
        return data.get('summary') or f"Задача {data['key']}"
    return data.get(name)


def _issue_json(issue_id, fields):
    data = ISSUES[issue_id]
    return {'id': str(issue_id), 'key': data['key'], 'fields': {f: _field(data, f) for f in fields}}


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
                return jsonify(_issue_json(issue_id, fields))
        return jsonify({'errorMessages': ['Issue does not exist or you do not have permission to see it.']}), 404

    def search():
        body = request.get_json()
        parents = re.match(r'parent in \(([^)]*)\)', body['jql'])
        if parents:
            keys = {k.strip() for k in parents.group(1).split(',')}
            ids = [i for i, d in ISSUES.items() if d.get('parent') in keys]
        else:
            ids = [int(x) for x in re.findall(r'\d+', body['jql'])]
        issues = [_issue_json(i, body['fields']) for i in ids if i in ISSUES and not ISSUES[i].get('hidden')]
        state['search_bodies'].append(body)
        return jsonify({'issues': issues, 'total': len(issues)})

    @app.post('/rest/api/2/search')
    def search_server():
        return search()

    @app.post('/rest/api/3/search/jql')
    def search_cloud():
        return search()

    @app.get('/rest/api/<v>/field')
    def fields(v):
        return jsonify(FIELDS)

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
