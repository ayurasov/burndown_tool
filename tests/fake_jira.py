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

import ast
import base64
import operator
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
    {'id': 'status', 'name': 'Статус', 'custom': False, 'schema': {'type': 'status'}},
    {'id': 'timeestimate', 'name': 'Remaining Estimate', 'custom': False, 'schema': {'type': 'number'}},
    {'id': 'timespent', 'name': 'Time Spent', 'custom': False, 'schema': {'type': 'number'}},
    {'id': 'timeoriginalestimate', 'name': 'Original Estimate', 'custom': False, 'schema': {'type': 'number'}},
    {'id': 'customfield_10050', 'name': 'Трудозатраты, ч', 'custom': True, 'schema': {'type': 'number'}},
    {'id': 'customfield_10060', 'name': 'Комментарий', 'custom': True, 'schema': {'type': 'string'}},
    # поля для записи прогноза Burndown
    {'id': 'customfield_10100', 'name': 'Прогноз Burndown', 'custom': True, 'schema': {'type': 'date'}},
    {'id': 'customfield_10101', 'name': 'Статус сроков', 'custom': True, 'schema': {'type': 'option'}},
    {'id': 'customfield_10102', 'name': 'Шанс успеть', 'custom': True, 'schema': {'type': 'number'}},
    {'id': 'customfield_10103', 'name': 'Прогноз (дата и время)', 'custom': True, 'schema': {'type': 'datetime'}},
    {'id': 'customfield_10104', 'name': 'Метки', 'custom': True, 'schema': {'type': 'array'}},
]
OPTIONS = {'customfield_10101': {'Успевает', 'Риск', 'Не успевает', 'Нет цели', 'Нет прогноза', 'Завершён'}}
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


TIME_FIELD_IDS = ('timeestimate', 'timespent', 'timeoriginalestimate')
_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv}


def _forest(transformations=None):
    """[(rowId, depth, issueId|None)] по формуле структуры; transformations — фильтры доски (forestSpec)."""
    out = []
    for part in FORMULA.split(','):
        row, depth, item = part.split(':')[:3]
        out.append((int(row), int(depth), int(item) if item.isdigit() else None))
    for tf in transformations or []:
        if tf.get('module') != FILTER_JQL:
            raise ValueError('transformation')
        out = _filter_forest(out, (tf.get('params') or {}).get('jql') or '', bool((tf.get('params') or {}).get('showSubItems')))
    return out


FILTER_JQL = 'com.almworks.jira.structure:filter-jql'


def _jql_match(jql, issue):
    """Учебный JQL фильтра: status / statusCategory / key, операторы = != in (…) not in (…), связки AND / OR."""
    data = ISSUES.get(issue)
    if data is None or data.get('hidden'):
        return False
    values = {'status': STATUS_NAMES[data.get('category') or 'new'].lower(), 'key': data['key'].lower(),
              'statuscategory': {'new': 'to do', 'indeterminate': 'in progress', 'done': 'done'}[data.get('category') or 'new']}
    token = r'"[^"]*"|\'[^\']*\'|[^\s,()]+'

    def clean(v):
        return v.strip().strip('"').strip("'").lower()

    def cond(text):
        m = re.fullmatch(r'\s*(\w+)\s*(not\s+in|in|!=|=)\s*(\(.*\)|' + token + r')\s*', text, re.I)
        if not m:
            raise ValueError('jql')
        field, op, value = m.group(1).lower(), re.sub(r'\s+', ' ', m.group(2).lower()), m.group(3)
        options = [clean(v) for v in re.findall(token, value.strip('()'))] if op in ('in', 'not in') else [clean(value)]
        hit = values.get(field) in options
        return not hit if op in ('!=', 'not in') else hit
    return any(all(cond(c) for c in re.split(r'\s+AND\s+', part, flags=re.I)) for part in re.split(r'\s+OR\s+', jql, flags=re.I))


def _filter_forest(rows, jql, show_sub_items):
    """Фильтр доски Structure: остаются совпавшие задачи и их родители (с showSubItems — и вложенные в совпавшие)."""
    keep = set()
    for i, (rid, depth, item) in enumerate(rows):
        if item is None or not _jql_match(jql, item):
            continue
        keep.add(rid)
        d = depth
        for prid, pdepth, _pitem in reversed(rows[:i]):  # родители
            if pdepth < d:
                keep.add(prid)
                d = pdepth
        if show_sub_items:
            for crid, cdepth, _citem in rows[i + 1:]:
                if cdepth <= depth:
                    break
                keep.add(crid)
    return [r for r in rows if r[0] in keep]


def _subtree(row_id, forest=None):
    rows = forest if forest is not None else _forest()
    for i, (rid, depth, _item) in enumerate(rows):
        if rid == row_id:
            out = [rid]
            for rid2, depth2, _ in rows[i + 1:]:
                if depth2 <= depth:
                    break
                out.append(rid2)
            return out
    return []


def _attr_value(spec, row_id, forest=None):
    """Значение колонки Structure у строки, как считает учебный Structure: поле (длительности — в миллисекундах,
    как в Structure; пользовательское поле — {"id": "customfield", "params": {"fieldId": N}}), статус — его название,
    сумма по поддереву (sum — с дубликатами) и формула (expr) на языке Structure — см. _Expr."""
    forest = forest if forest is not None else _forest()
    item = {rid: it for rid, _d, it in forest}.get(row_id)
    data = ISSUES.get(item) if item else None
    attr = spec.get('id')
    params = spec.get('params') or {}
    if attr == 'sum':
        inner = params.get('attribute') or params.get('value')
        vals = [_attr_value(inner, r, forest) for r in _subtree(row_id, forest)]
        nums = [v for v in vals if isinstance(v, (int, float)) and not isinstance(v, bool)]
        return sum(nums) if nums else None
    if attr == 'expr':
        return _Expr(params.get('formula') or '', params.get('variables') or {}).value(row_id, forest)
    if attr == 'customfield':
        attr = f"customfield_{params['fieldId']}"
    if data is None or data.get('hidden'):
        return None
    if attr == 'status':
        return STATUS_NAMES[data.get('category') or 'new']
    if attr not in FIELD_IDS:
        raise KeyError(attr)
    value = data.get(attr)
    if attr in TIME_FIELD_IDS and value is not None:
        return value * 1000
    return value


class _Expr:
    """Учебный вычислитель языка формул Structure (Expr): числа, строки, переменные, WITH имя = … : …,
    if(условие, да, нет), + − × ÷, сравнения (= != < > <= >=), AND / OR / NOT, скобки и агрегаты SUM{…}
    (дубликаты — одна и та же задача в поддереве — не считаются) и SUM#all{…} (считаются)."""
    TOKEN = re.compile(r'\s*(?:(\d+(?:\.\d+)?)|"([^"]*)"|\'([^\']*)\'|(SUM#all|[A-Za-z_][A-Za-z0-9_]*)|(<=|>=|!=|[-+*/<>=(),:{}]))')

    def __init__(self, formula, variables):
        self.variables, self.tokens, pos = variables, [], 0
        formula = formula.strip()
        while pos < len(formula):
            m = self.TOKEN.match(formula, pos)
            if not m or m.end() == pos:
                raise ValueError('formula')
            num, s1, s2, name, op = m.groups()
            self.tokens.append(('num', float(num)) if num else ('str', s1 if s1 is not None else s2) if (s1 is not None or s2 is not None)
                               else ('name', name) if name else ('op', op))
            pos = m.end()
        self.pos = 0
        self.tree = self.with_expr()
        if self.pos != len(self.tokens):
            raise ValueError('formula')

    # ── разбор в дерево
    def peek(self, kind=None, value=None):
        if self.pos >= len(self.tokens):
            return None
        tok = self.tokens[self.pos]
        if kind and tok[0] != kind:
            return None
        if value is not None and (tok[1].upper() if tok[0] == 'name' else tok[1]) != value:
            return None
        return tok

    def take(self, kind=None, value=None):
        tok = self.peek(kind, value)
        if tok is None:
            raise ValueError('formula')
        self.pos += 1
        return tok

    def with_expr(self):
        if self.peek('name', 'WITH'):
            self.take()
            name = self.take('name')[1]
            self.take('op', '=')
            value = self.or_expr()
            self.take('op', ':')
            return ('with', name.lower(), value, self.with_expr())
        return self.or_expr()

    def binary(self, sub, ops, kind='op'):
        node = sub()
        while self.peek(kind) and (self.tokens[self.pos][1].upper() if kind == 'name' else self.tokens[self.pos][1]) in ops:
            op = self.take()[1].upper()
            node = (op, node, sub())
        return node

    def or_expr(self):
        return self.binary(self.and_expr, ('OR',), 'name')

    def and_expr(self):
        return self.binary(self.not_expr, ('AND',), 'name')

    def not_expr(self):
        if self.peek('name', 'NOT'):
            self.take()
            return ('NOT', self.not_expr())
        return self.binary(self.add, ('=', '!=', '<', '>', '<=', '>='))

    def add(self):
        return self.binary(self.mul, ('+', '-'))

    def mul(self):
        return self.binary(self.unary, ('*', '/'))

    def unary(self):
        if self.peek('op', '-'):
            self.take()
            return ('neg', self.unary())
        return self.primary()

    def primary(self):
        tok = self.take()
        if tok[0] in ('num', 'str'):
            return ('const', tok[1])
        if tok == ('op', '('):
            node = self.with_expr()
            self.take('op', ')')
            return node
        if tok[0] != 'name':
            raise ValueError('formula')
        name = tok[1]
        if name.upper() in ('SUM', 'SUM#ALL') and self.peek('op', '{'):
            self.take()
            body = self.with_expr()
            self.take('op', '}')
            return ('sum', name.upper() == 'SUM#ALL', body)
        if self.peek('op', '('):
            self.take()
            args = [self.with_expr()]
            while self.peek('op', ','):
                self.take()
                args.append(self.with_expr())
            self.take('op', ')')
            if name.lower() != 'if' or len(args) != 3:
                raise ValueError('formula')
            return ('if', *args)
        return ('var', name)

    # ── вычисление
    def value(self, row_id, forest):
        self.forest = forest
        return self.ev(self.tree, row_id, {})

    def ev(self, node, row, local):
        kind = node[0]
        if kind == 'const':
            return node[1]
        if kind == 'var':
            name = node[1]
            if name.lower() in local:
                return local[name.lower()]
            if name not in self.variables:
                raise KeyError(name)
            return _attr_value(self.variables[name], row, self.forest)
        if kind == 'with':
            return self.ev(node[3], row, dict(local, **{node[1]: self.ev(node[2], row, local)}))
        if kind == 'if':
            return self.ev(node[2] if self.ev(node[1], row, local) else node[3], row, local)
        if kind == 'sum':
            total, seen = 0, set()
            for r in _subtree(row, self.forest):
                item = {rid: it for rid, _d, it in self.forest}.get(r)
                if not node[1] and item is not None:
                    if item in seen:
                        continue  # та же задача в поддереве ещё раз — дубликат
                    seen.add(item)
                v = self.ev(node[2], r, local)
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    total += v
            return total
        if kind == 'NOT':
            return not self.ev(node[1], row, local)
        if kind == 'neg':
            return -(self.ev(node[1], row, local) or 0)
        a, b = self.ev(node[1], row, local), self.ev(node[2], row, local)
        if kind == 'AND':
            return bool(a) and bool(b)
        if kind == 'OR':
            return bool(a) or bool(b)
        if kind in ('=', '!='):
            return (a == b) == (kind == '=')
        a, b = a or 0, b or 0
        return {'+': lambda: a + b, '-': lambda: a - b, '*': lambda: a * b, '/': lambda: a / b,
                '<': lambda: a < b, '>': lambda: a > b, '<=': lambda: a <= b, '>=': lambda: a >= b}[kind]()


FIELD_IDS = {'summary', 'timeestimate', 'timespent', 'timeoriginalestimate', 'customfield_10050', 'customfield_10060',
             'duedate', 'key'}


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
        writing = request.method == 'PUT' and request.path.startswith('/rest/api/2/issue/')  # v2 есть и в Cloud
        if request.path.startswith('/rest/api/') and not request.path.startswith(api) and not writing:
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

    @app.put('/rest/api/2/issue/<key>')
    def update_issue(key):
        if not any(d['key'] == key and not d.get('hidden') for d in ISSUES.values()):
            return jsonify({'errorMessages': ['Issue does not exist or you do not have permission to see it.']}), 404
        fields = (request.get_json() or {}).get('fields') or {}
        known = {f['id']: f for f in FIELDS}
        errors = {}
        for fid, value in fields.items():
            if fid not in known or fid in state.get('readonly_fields', ()):
                errors[fid] = f"Field '{fid}' cannot be set. It is not on the appropriate screen, or unknown."
            elif fid in OPTIONS and value is not None and (value or {}).get('value') not in OPTIONS[fid]:
                errors[fid] = f"Option value '{(value or {}).get('value')}' is not valid"
            elif known[fid]['schema']['type'] == 'date' and value is not None and not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
                errors[fid] = 'Invalid date'
        if errors:
            return jsonify({'errorMessages': [], 'errors': errors}), 400
        state.setdefault('writes', []).append((key, fields))
        return '', 204

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
        spec = request.get_json()
        if spec.get('structureId') != 7:
            return jsonify({'message': 'structure not found'}), 404
        state.setdefault('forest_specs', []).append(spec)
        try:
            rows = _forest(spec.get('transformations'))
        except ValueError:
            return jsonify({'errorMessages': ['Invalid transformation or JQL']}), 400
        formula = ','.join(f"{r}:{d}:{it if it is not None else '5/9'}" for r, d, it in rows)
        return jsonify({'spec': spec, 'formula': formula,
                        'itemTypes': {'5': 'com.almworks.jira.structure:type-folder'},
                        'version': {'signature': 1, 'version': 3}})

    @app.post('/rest/structure/2.0/value')
    def values():
        if not state.get('value_api', True):
            return 'Not Found', 404
        body = request.get_json()
        state['value_bodies'].append(body)
        out = []
        for req in body.get('requests') or []:
            if (req.get('forestSpec') or {}).get('structureId') != 7:
                return jsonify({'message': 'structure not found'}), 404
            rows = req.get('rows') or []
            try:
                forest = _forest(req['forestSpec'].get('transformations'))
            except ValueError:
                return jsonify({'errorMessages': ['Invalid transformation or JQL']}), 400
            present = {rid for rid, _d, _it in forest}
            data = []
            for spec in req.get('attributes') or []:
                try:
                    vals = [_attr_value(spec, r, forest) if r in present else None for r in rows]
                except (KeyError, ValueError, ZeroDivisionError):
                    return jsonify({'errorMessages': [f"Unknown attribute: {spec.get('id')}"]}), 400
                data.append({'attribute': spec, 'values': vals, 'trailMode': 'INDEPENDENT', 'trails': ['' for _ in rows]})
            out.append({'forestSpec': req['forestSpec'], 'rows': rows, 'data': data,
                        'forestVersion': {'signature': 1, 'version': 3}})
        return jsonify({'responses': out, 'itemTypes': {}, 'itemsVersion': {'signature': 1, 'version': 1}})

    return app


class FakeJira:
    def __init__(self, mode='server', login='', token='secret-pat', structure=True):
        self.state = {'mode': mode, 'login': login, 'token': token, 'structure': structure,
                      'requests': [], 'search_bodies': [], 'value_bodies': []}
        self.server = make_server('127.0.0.1', 0, create_app(self.state), threaded=True)
        self.url = f'http://127.0.0.1:{self.server.server_port}'
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def configure(self, **kw):
        self.state.update(kw)

    def stop(self):
        self.server.shutdown()
        self.thread.join()
