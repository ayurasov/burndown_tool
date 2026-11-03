"""Связка с Jira и плагином Structure: подключение и расчёт «среза» для замеров.

Подключение устроено так же, как в jira_mass_logger:
  - cloud        — Jira Cloud, email + API-токен (Basic), REST API v3;
  - server       — Jira Server/DC, Personal Access Token (Bearer), REST API v2;
  - server_basic — Jira Server, логин + пароль (Basic), REST API v2;
  - опция «не проверять TLS-сертификат» для корпоративных/самоподписанных сертификатов;
  - прокси берётся из переменных окружения сервера (HTTPS_PROXY и т. п.).

Срез по эпику: для заданных задач берутся поля Jira, сопоставленные метрикам кнопки
(по умолчанию учёт времени: timeestimate — остаток, timespent — затрачено,
timeoriginalestimate — оценка; в секундах), и суммируются в часы. Если у кнопки указана
структура и включено «с вложенными», суммируются все задачи поддерева этих задач в структуре
(каждая задача — один раз); без структуры «с вложенными» — задача вместе с подзадачами.

Показатель может браться и из колонки Structure — поля, суммы по поддереву или формулы, как она настроена
в структуре: значение колонки у строки задачи эпика читается через Value API Structure
(/rest/structure/2.0/value) и совпадает с тем, что видно в структуре.
"""

import json

import base64
import hashlib
import os
from datetime import datetime
import re
import time
from urllib.parse import quote, urlsplit

# Без этих библиотек (не выполнен pip install -r requirements.txt после обновления) приложение работает,
# недоступна только связь с Jira — с понятным сообщением вместо падения при запуске.
try:
    import requests
except ImportError:  # pragma: no cover
    requests = None
try:
    from cryptography.fernet import Fernet, InvalidToken
except ImportError:  # pragma: no cover
    Fernet = InvalidToken = None

INSTANCE_TYPES = ('cloud', 'server', 'server_basic')
ISSUE_KEY_RE = re.compile(r'^[A-Z][A-Z0-9_]*-\d+$')
METRICS = ('remaining', 'spent', 'estimate')
ISSUE_FIELDS = ('summary', 'status', 'parent', 'issuetype')  # для состава эпика по задачам
STATUS_CATEGORIES = ('new', 'indeterminate', 'done')
PROBLEMS = {
    'no_estimate': 'Не закрыта, нет оценки и остатка',
    'done_with_remaining': 'Закрыта, но остаток не обнулён',
    'zero_remaining': 'В работе, остаток 0 при ненулевой оценке',
    'overspent': 'Затрачено больше оценки',
}
TIME_FIELDS = {'remaining': 'timeestimate', 'spent': 'timespent', 'estimate': 'timeoriginalestimate'}
TIME_FIELD_NAMES = {'timeestimate': 'Оставшаяся оценка', 'timespent': 'Затраченное время',
                    'timeoriginalestimate': 'Первоначальная оценка', 'aggregatetimeestimate': 'Σ Оставшаяся оценка',
                    'aggregatetimespent': 'Σ Затраченное время', 'aggregatetimeoriginalestimate': 'Σ Первоначальная оценка'}
# Σ-поля Jira: значение задачи вместе с её подзадачами
AGGREGATE_FIELDS = {'aggregatetimeestimate', 'aggregatetimespent', 'aggregatetimeoriginalestimate'}
# единица значения поля Jira или колонки Structure → множитель для перевода в часы (рабочий день — 8 ч)
UNITS = {'ms': 1 / 3600000, 'seconds': 1 / 3600, 'minutes': 1 / 60, 'hours': 1.0, 'days': 8.0}
ATTRIBUTE_ID_RE = re.compile(r'^[A-Za-z0-9_.:\-]{1,200}$')
ATTRIBUTE_MAX_JSON = 20000
DEFAULT_FIELD_MAP = {m: {'field': TIME_FIELDS[m], 'unit': 'seconds', 'structure': None} for m in METRICS}
FIELD_ID_RE = re.compile(r'^[A-Za-z][A-Za-z0-9_]*$')
SEARCH_BATCH = 100  # задач в одном запросе Jira Cloud
SERVER_BATCH = 500  # Server/DC отдаёт до 1000 задач за запрос (если в Jira предел меньше — дочитываем постранично)
HISTORY_BATCH = 50  # задач в одном запросе с журналом изменений
SEARCH_LIMIT = 5000
MAX_ATTEMPTS = 3


class JiraError(Exception):
    """Ошибка обращения к Jira с понятным пользователю текстом."""


class JiraQueryError(JiraError):
    """Jira отклонила запрос (400) — например, JQL с ключом задачи, которой нет или которую не видно."""


def check_base_url(base_url):
    """Адрес Jira: http(s)://хост[:порт][/путь] — без логина в адресе, параметров и якоря (иначе путь запросов
    к API можно подменить). JIRA_ALLOWED_HOSTS (через запятую) ограничивает хосты: «jira.company.ru»
    или «.company.ru» — домен и все поддомены."""
    url = (base_url or '').strip().rstrip('/')
    if not re.match(r'^https?://', url, re.IGNORECASE):
        raise JiraError('Адрес Jira должен начинаться с http:// или https://')
    try:
        parts = urlsplit(url)
        parts.port  # noqa: B018 — проверка порта: ValueError, если он некорректный
    except ValueError:
        raise JiraError('Некорректный адрес Jira')
    if not parts.hostname or '@' in parts.netloc or parts.query or parts.fragment or '?' in url or '#' in url \
            or re.search(r'\s', url):
        raise JiraError('Адрес Jira — только https://хост[:порт][/путь], без логина, параметров «?» и «#»')
    allowed = [h.strip().lower() for h in os.environ.get('JIRA_ALLOWED_HOSTS', '').split(',') if h.strip()]
    host = parts.hostname.lower()
    if allowed and not any(host == h or (h.startswith('.') and (host.endswith(h) or host == h[1:]))
                           for h in allowed):
        raise JiraError(f'Подключение к {host} запрещено настройками сервера (JIRA_ALLOWED_HOSTS)')
    return url


def _require_libs():
    missing = [name for name, mod in (('requests', requests), ('cryptography', Fernet)) if mod is None]
    if missing:
        raise JiraError('На сервере не установлены библиотеки для связи с Jira (' + ', '.join(missing) +
                        '): выполните pip install -r requirements.txt и перезапустите приложение')


# ─── Хранение токена ─────────────────────────────────────────────

def _fernet(secret_key):
    _require_libs()
    key = base64.urlsafe_b64encode(hashlib.sha256(('jira-token:' + str(secret_key)).encode()).digest())
    return Fernet(key)


def encrypt_token(secret_key, token):
    return _fernet(secret_key).encrypt(token.encode()).decode()


def decrypt_token(secret_key, encrypted):
    try:
        return _fernet(secret_key).decrypt(encrypted.encode()).decode()
    except (InvalidToken, ValueError):
        raise JiraError('Сохранённый токен не удаётся расшифровать (сменился SECRET_KEY сервера) — введите токен заново')


# ─── Разбор ключей задач и дерева Structure ──────────────────────

def parse_issue_keys(text):
    """«ABC-1, abc-2\\nXYZ-3» → ['ABC-1', 'ABC-2', 'XYZ-3'] (без повторов); некорректные — отдельно."""
    keys, invalid = [], []
    for raw in re.split(r'[\s,;]+', text or ''):
        if not raw:
            continue
        key = raw.strip().upper()
        if not ISSUE_KEY_RE.match(key):
            invalid.append(raw.strip())
        elif key not in keys:
            keys.append(key)
    return keys, invalid


def parse_forest_rows(formula):
    """Формула Structure «rowId:depth:item,…» → [(rowId, depth, issueId|None)] — с номерами строк для Value API."""
    rows = []
    for part in (formula or '').split(','):
        pieces = part.split(':')
        if len(pieces) < 3:
            continue
        try:
            row_id, depth = int(pieces[0]), int(pieces[1])
        except ValueError:
            continue
        rows.append((row_id, depth, int(pieces[2]) if pieces[2].isdigit() else None))
    return rows


def parse_forest_items(formula):
    """Формула Structure → [(rowId, depth, issueId|None, элемент)]; элемент — как в формуле: «12345» у задачи,
    «тип/ID» у папки и прочих элементов. По элементам выбираются разделы структуры."""
    rows = []
    for part in (formula or '').split(','):
        pieces = part.split(':')
        if len(pieces) < 3:
            continue
        try:
            row_id, depth = int(pieces[0]), int(pieces[1])
        except ValueError:
            continue
        rows.append((row_id, depth, int(pieces[2]) if pieces[2].isdigit() else None, pieces[2]))
    return rows


def issue_occurrences(items):
    """Все места задач в структуре → {issueId: [{'row', 'depth', 'path': [элементы предков], 'pathRows'}]}.
    Одна и та же задача может стоять в структуре несколько раз, и под каждой строкой у неё своё поддерево."""
    stack, out = [], {}
    for row_id, depth, issue_id, item in items:
        while stack and stack[-1][0] >= depth:
            stack.pop()
        if issue_id is not None:
            out.setdefault(issue_id, []).append({'row': row_id, 'depth': depth, 'path': [x[2] for x in stack],
                                                 'pathRows': [x[1] for x in stack]})
        stack.append((depth, row_id, item))
    return out


def pick_occurrence(occurrences, scope_rows=None):
    """Строка задачи для расчёта: первая в выбранных разделах структуры, без разделов — первая в структуре.
    scope_rows — строки разделов (scope_rows()): место подходит, если раздел — сама строка задачи (отмечен эпик)
    или одна из строк над ней (отмечена папка). → (место | None, найдено ли в разделах)."""
    if not occurrences:
        return None, False
    if scope_rows is not None:
        for occ in occurrences:
            if occ['row'] in scope_rows or scope_rows.intersection(occ['pathRows']):
                return occ, True
        return occurrences[0], False
    return occurrences[0], True


def occurrence_warning(key, occurrences, picked, in_scope, scope_rows):
    """Предупреждение о задаче, которая стоит в структуре несколько раз (или не найдена в выбранных разделах)."""
    if scope_rows is not None and not in_scope:
        return f'{key}: в выбранных разделах структуры задачи нет — взята первая строка в структуре'
    if len(occurrences) > 1 and scope_rows is None:
        return (f'{key}: задача стоит в структуре {len(occurrences)} раз(а) — взята первая строка; чтобы взять строку из '
                'нужного раздела, отметьте его у кнопки («Раздел структуры»)')
    return None


def subtree_rows(items, row_id):
    """Строки поддерева строки row_id (включая её саму) → [(rowId, issueId|None)] в порядке структуры."""
    out, depth = [], None
    for rid, d, issue_id, _item in items:
        if depth is None:
            if rid == row_id:
                depth = d
                out.append((rid, issue_id))
            continue
        if d <= depth:
            break
        out.append((rid, issue_id))
    return out


def subtree_issues_at(items, row_id):
    """ID задач в поддереве строки row_id (включая её саму)."""
    return {issue_id for _rid, issue_id in subtree_rows(items, row_id) if issue_id is not None}


SCOPE_ITEM_RE = re.compile(r'^[^,:]{1,300}$')


def normalize_scope(raw):
    """Разделы структуры у кнопки: [{'item', 'title'[, 'row']}] (до 50) или None. row — строка, отмеченная в списке
    разделов: элемент (эпик, папка) может стоять в структуре несколько раз, строка различает места."""
    if not raw:
        return None
    if not isinstance(raw, list) or len(raw) > 50:
        raise ValueError('Разделы структуры — список (не больше 50)')
    out = []
    for x in raw:
        item = x.get('item') if isinstance(x, dict) else x
        if not isinstance(item, str) or not SCOPE_ITEM_RE.match(item):
            raise ValueError('Некорректный раздел структуры')
        title = x.get('title') if isinstance(x, dict) and isinstance(x.get('title'), str) else ''
        entry = {'item': item, 'title': title.strip()[:300]}
        row = x.get('row') if isinstance(x, dict) else None
        if row is not None:
            if not isinstance(row, int) or isinstance(row, bool) or row < 0:
                raise ValueError('Некорректная строка раздела структуры')
            entry['row'] = row
        out.append(entry)
    return out or None


def scope_rows(items, scope):
    """Строки выбранных разделов в текущей структуре (items — parse_forest_items) → set или None без разделов.
    Сохранённая строка берётся, если в ней тот же элемент; иначе (строку пересоздали, раздел сохранён до 1.9.3) —
    строки с этим элементом на верхних двух уровнях, как в списке разделов, а если таких нет — на любой глубине."""
    if not scope:
        return None
    item_at = {row_id: item for row_id, _d, _iid, item in items}
    out = set()
    for entry in scope:
        row = entry.get('row')
        if row is not None and item_at.get(row) == entry['item']:
            out.add(row)
            continue
        same = [(row_id, depth) for row_id, depth, _iid, item in items if item == entry['item']]
        out.update(row_id for row_id, depth in (([x for x in same if x[1] <= 1]) or same))
    return out


def parse_forest_formula(formula):
    """Формула Structure «rowId:depth:item[:semantic],…» → [(depth, issueId|None)].

    Задачи Jira закодированы числом (ID задачи), прочие элементы (папки, генераторы и т. п.) —
    «тип/ID» или «тип//строка»: для них issueId = None, но глубина учитывается при обходе поддерева.
    """
    rows = []
    for part in (formula or '').split(','):
        if not part:
            continue
        pieces = part.split(':')
        if len(pieces) < 3:
            continue
        try:
            depth = int(pieces[1])
        except ValueError:
            continue
        item = pieces[2]
        rows.append((depth, int(item) if item.isdigit() else None))
    return rows


def seconds_to_hours(values):
    """Сумма секунд в часы (2 знака); None, если ни у одной задачи поле не заполнено."""
    return sum_hours(values, 'seconds')


def sum_hours(values, unit):
    """Сумма числовых значений поля в часах (2 знака); None, если ни у одной задачи поле не заполнено."""
    filled = [v for v in values if isinstance(v, (int, float)) and not isinstance(v, bool)]
    return round(sum(filled) * UNITS[unit], 2) if filled else None


def normalize_attribute(spec, depth=0):
    """Спецификация колонки (атрибута) Structure: {'id', 'format', 'params'} — как её отправляет доска Structure.
    Вложенные спецификации (переменные формулы, атрибут суммы) проверяются так же. → ValueError при ошибке."""
    if depth > 6 or not isinstance(spec, dict):
        raise ValueError('Спецификация колонки Structure — объект JSON с полями id и format')
    attr_id = spec.get('id')
    if not isinstance(attr_id, str) or not ATTRIBUTE_ID_RE.match(attr_id):
        raise ValueError('В спецификации колонки Structure нет корректного id')
    out = {'id': attr_id, 'format': spec.get('format') if isinstance(spec.get('format'), str) else 'number'}
    params = spec.get('params')
    if params is not None:
        if not isinstance(params, dict):
            raise ValueError('Параметры колонки Structure (params) — объект JSON')
        for value in params.values():  # вложенные спецификации тоже проверяем
            if isinstance(value, dict) and 'id' in value and 'format' in value:
                normalize_attribute(value, depth + 1)
            if isinstance(value, dict) and all(isinstance(v, dict) and 'id' in v for v in value.values()) and value:
                for v in value.values():
                    normalize_attribute(v, depth + 1)
        out['params'] = params
    if depth == 0 and len(json.dumps(out, ensure_ascii=False)) > ATTRIBUTE_MAX_JSON:
        raise ValueError('Спецификация колонки Structure слишком большая')
    return out


# ─── Колонка Structure по настройкам, как в редакторе колонки Structure ──
# «Поле» или «Формула», «Σ по вложенным» (Sum over sub-items), «Исключить дубликаты» (Exclude duplicates).
# Σ с исключением дубликатов — агрегатная функция языка Structure SUM{…} (дубликаты она по умолчанию не считает);
# Σ с дубликатами — атрибут суммы sum (он, как колонка без «Exclude duplicates», считает строку столько раз, сколько
# раз задача стоит в поддереве). Значения считаются по всей структуре — без временных фильтров представления.

STRUCTURE_FORMATS = ('duration', 'number', 'text', 'time')
EDITOR_MODES = ('field', 'formula')
VARIABLE_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_]{0,63}$')
FORMULA_MAX = 20000
DURATION_FIELDS = {'timeestimate', 'timespent', 'timeoriginalestimate', 'aggregatetimeestimate', 'aggregatetimespent',
                   'aggregatetimeoriginalestimate'}


def field_spec(field_id, fmt):
    """Поле Jira как атрибут Structure: пользовательское поле — {"id": "customfield", "params": {"fieldId": N}}."""
    m = re.fullmatch(r'customfield_(\d+)', field_id)
    if m:
        return {'id': 'customfield', 'format': fmt, 'params': {'fieldId': int(m.group(1))}}
    return {'id': field_id, 'format': fmt}


def normalize_column_editor(raw):
    """Настройки колонки из редактора Burndown → проверенный словарь (ValueError с понятным текстом)."""
    if not isinstance(raw, dict) or raw.get('mode') not in EDITOR_MODES:
        raise ValueError('Тип колонки Structure — «Поле» или «Формула»')

    def variable(v, name):
        if not isinstance(v, dict):
            raise ValueError(f'Переменная «{name}»: выберите поле Jira')
        field = (v.get('field') or '').strip()
        if not field:
            raise ValueError(f'Выберите поле Jira для переменной «{name}»')
        if not FIELD_ID_RE.match(field):
            raise ValueError(f'Переменная «{name}»: некорректное поле Jira {field}')
        fmt = v.get('format') or 'number'
        if fmt not in STRUCTURE_FORMATS:
            raise ValueError(f'Переменная «{name}»: формат — длительность, число, текст или дата')
        return {'field': field, 'format': fmt}
    out = {'mode': raw['mode'], 'sum': bool(raw.get('sum')), 'distinct': bool(raw.get('distinct', True))}
    if raw['mode'] == 'field':  # из редактора — field и format; сохранённые настройки — переменная value
        saved = (raw.get('variables') or {}).get('value') if isinstance(raw.get('variables'), dict) else None
        saved = saved if isinstance(saved, dict) else {}
        out['variables'] = {'value': variable({'field': raw.get('field') or saved.get('field'),
                                               'format': raw.get('format') or saved.get('format')}, 'поле')}
        out['formula'] = 'value'
        return out
    formula = raw.get('formula')
    if not isinstance(formula, str) or not formula.strip():
        raise ValueError('Введите формулу')
    if len(formula) > FORMULA_MAX:
        raise ValueError('Формула слишком большая')
    variables = raw.get('variables')
    variables = {} if variables is None else variables
    if not isinstance(variables, dict) or len(variables) > 50:
        raise ValueError('Переменные формулы — объект {имя: поле}')
    out['formula'] = formula.strip()
    out['variables'] = {}
    for name, v in variables.items():
        if not isinstance(name, str) or not VARIABLE_RE.match(name):
            raise ValueError(f'Некорректное имя переменной: {name}')
        out['variables'][name] = variable(v, name)
    return out


def _expr(formula, variables):
    return {'id': 'expr', 'format': 'number',
            'params': {'formula': formula, 'variables': {n: field_spec(v['field'], v['format']) for n, v in variables.items()}}}


def column_variants(editor):
    """Три варианта колонки для сверки: только сама задача, Σ без дубликатов, Σ с дубликатами."""
    own = _expr(editor['formula'], editor['variables'])
    return {'own': own,
            'distinct': _expr('SUM{\n' + editor['formula'] + '\n}', editor['variables']),
            'all': {'id': 'sum', 'format': 'number', 'params': {'attribute': own}}}


def build_structure_attribute(editor):
    """Спецификация атрибута для Value API по настройкам колонки (как их задают в Structure)."""
    variants = column_variants(editor)
    if not editor['sum']:
        return variants['own']
    return variants['distinct'] if editor['distinct'] else variants['all']


def formula_fields(raw):
    """Поля Jira для переменных формулы: [{id, name, format}] — учёт времени, статус, затем остальные по имени."""
    out = []
    for f in raw or []:
        if not isinstance(f, dict) or not isinstance(f.get('id'), str) or not FIELD_ID_RE.match(f['id']):
            continue
        kind = (f.get('schema') or {}).get('type')
        fmt = ('duration' if f['id'] in DURATION_FIELDS else 'number' if kind == 'number'
               else 'time' if kind in ('date', 'datetime') else 'text')
        out.append({'id': f['id'], 'name': TIME_FIELD_NAMES.get(f['id']) or str(f.get('name') or f['id']), 'format': fmt})
    rank = {'timeoriginalestimate': 0, 'timespent': 1, 'timeestimate': 2, 'status': 3}
    return sorted(out, key=lambda x: (rank.get(x['id'], 9), x['format'] != 'duration', x['name'].lower()))


# ─── Фильтр доски Structure — для колонок «После фильтрации» (After filtering) ──
# Колонка с «After filtering» в Structure суммирует только задачи, видимые с фильтром доски. Burndown повторяет это:
# такие колонки запрашиваются у Structure с тем же фильтром (трансформация в forestSpec), остальные — по всей
# структуре. Фильтр — JQL (модуль filter-jql, как фильтр на доске) или трансформации, скопированные из Structure.

FILTER_JQL_MODULE = 'com.almworks.jira.structure:filter-jql'
TRANSFORM_MODULE_RE = re.compile(r'^[A-Za-z0-9_.\-]{1,100}:[A-Za-z0-9_.\-]{1,100}$')
BOARD_FILTER_MODES = ('none', 'jql', 'json')


def extract_transformations(data, depth=0):
    """Трансформации из вставленного JSON: forestSpec, запрос value/forest из инструментов разработчика или список."""
    if depth > 6:
        return None
    if isinstance(data, list):
        if data and all(isinstance(t, dict) and 'module' in t for t in data):
            return data
        for item in data:
            found = extract_transformations(item, depth + 1)
            if found:
                return found
        return None
    if not isinstance(data, dict):
        return None
    if isinstance(data.get('module'), str):
        return [data]
    if isinstance(data.get('transformations'), list) and data['transformations']:
        return data['transformations']
    for key in ('forestSpec', 'spec', 'requests', 'responses'):
        if key in data:
            found = extract_transformations(data[key], depth + 1)
            if found:
                return found
    return None


def normalize_board_filter(raw):
    """Фильтр доски у кнопки → None (нет) | {'mode': 'jql', 'jql', 'showSubItems'} | {'mode': 'json', 'transformations'}."""
    if not raw:
        return None
    if not isinstance(raw, dict) or (raw.get('mode') or 'none') not in BOARD_FILTER_MODES:
        raise ValueError('Фильтр доски — JQL или трансформации из Structure (JSON)')
    mode = raw.get('mode') or 'none'
    if mode == 'none':
        return None
    if mode == 'jql':
        jql = raw.get('jql')
        if not isinstance(jql, str) or not jql.strip():
            raise ValueError('Введите JQL фильтра доски — тот же, что на доске Structure')
        if len(jql) > 5000:
            raise ValueError('JQL фильтра доски слишком длинный')
        return {'mode': 'jql', 'jql': jql.strip(), 'showSubItems': bool(raw.get('showSubItems'))}
    data = raw.get('transformations')
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except ValueError:
            raise ValueError('Трансформации доски — JSON из Structure (forestSpec или его transformations)')
    found = extract_transformations(data)
    if not found:
        raise ValueError('В JSON не найдены трансформации доски (transformations с module)')
    if len(found) > 10:
        raise ValueError('Слишком много трансформаций доски')
    out = []
    for t in found:
        module, params = t.get('module'), t.get('params', {})
        if not isinstance(module, str) or not TRANSFORM_MODULE_RE.match(module) or not isinstance(params, (dict, type(None))):
            raise ValueError('Некорректная трансформация доски: нужен module вида com.almworks.jira.structure:… и params')
        out.append({'module': module, 'params': params or {}})
    if len(json.dumps(out, ensure_ascii=False)) > ATTRIBUTE_MAX_JSON:
        raise ValueError('Трансформации доски слишком большие')
    return {'mode': 'json', 'transformations': out}


def board_transformations(board_filter):
    """Фильтр доски → трансформации для forestSpec (None — без фильтра)."""
    if not board_filter:
        return None
    if board_filter['mode'] == 'jql':
        params = {'jql': board_filter['jql']}
        if board_filter.get('showSubItems'):
            params['showSubItems'] = True
        return [{'module': FILTER_JQL_MODULE, 'params': params}]
    return board_filter['transformations']


def forest_spec(structure_id, transformations=None):
    spec = {'structureId': structure_id}
    if transformations:
        spec['transformations'] = transformations
    return spec


def normalize_field_map(raw):
    """Проверенное сопоставление метрик: {metric: {'field': id|'', 'unit', 'structure': None | {'attribute',
    'unit', 'label'}}}. field — поле Jira (сумма по задачам; для показателя из колонки Structure — для состава задач
    и восстановления истории); structure — колонка Structure, из которой берётся сам показатель эпика.

    Пустое поле и нет колонки — метрику не заполнять. Некорректные значения → ValueError с понятным текстом.
    """
    raw = raw or {}
    out = {}
    for metric in METRICS:
        item = raw.get(metric, DEFAULT_FIELD_MAP[metric])
        if not isinstance(item, dict):
            raise ValueError('Некорректное сопоставление полей')
        field = (item.get('field') or '').strip()
        unit = item.get('unit') or 'seconds'
        if field and not FIELD_ID_RE.match(field):
            raise ValueError(f'Некорректный идентификатор поля Jira: {field}')
        if unit not in UNITS:
            raise ValueError('Единица измерения — миллисекунды, секунды, минуты, часы или дни')
        if field in DURATION_FIELDS:
            unit = 'seconds'  # учёт времени (и Σ-поля) Jira всегда отдаёт в секундах, что бы ни было выбрано раньше
        structure = item.get('structure')
        if structure:
            if not isinstance(structure, dict):
                raise ValueError('Некорректная колонка Structure')
            s_unit = structure.get('unit') or 'hours'
            if s_unit not in UNITS:
                raise ValueError('Единица колонки Structure — миллисекунды, секунды, минуты, часы или дни')
            label = structure.get('label') if isinstance(structure.get('label'), str) else ''
            editor = normalize_column_editor(structure['editor']) if structure.get('editor') else None
            attribute = build_structure_attribute(editor) if editor else normalize_attribute(structure.get('attribute'))
            structure = {'attribute': attribute, 'unit': s_unit, 'label': label.strip()[:200],
                         'afterFiltering': bool(structure.get('afterFiltering'))}
            if editor:
                structure['editor'] = editor
        out[metric] = {'field': field, 'unit': unit, 'structure': structure or None}
    if not any(v['field'] or v['structure'] for v in out.values()):
        raise ValueError('Сопоставьте с полями Jira или колонками Structure хотя бы одну метрику')
    return out


# ─── HTTP-клиент Jira ────────────────────────────────────────────

class JiraClient:
    def __init__(self, base_url, instance_type, login, token, accept_invalid_certs=False, timeout=(15, 60),
                 detailed=True):
        """detailed=False — для пользователей без доступа к настройкам подключения (руководитель проекта): в ошибках
        нет адреса Jira и технических подробностей, только что случилось и что сообщить администратору."""
        self.detailed = detailed
        self.calls, self.elapsed = 0, 0.0  # число запросов к Jira и их суммарное время — для журнала службы
        if instance_type not in INSTANCE_TYPES:
            raise JiraError('Неизвестный тип подключения к Jira')
        self.base_url = check_base_url(base_url)
        _require_libs()
        self.instance_type = instance_type
        self.timeout = timeout
        self.session = requests.Session()  # прокси из окружения сервера подхватываются автоматически
        self.session.headers.update({'Accept': 'application/json'})
        if instance_type == 'server':
            self.session.headers['Authorization'] = 'Bearer ' + token
        else:
            # Basic: логин и пароль в UTF-8 (requests по умолчанию кодирует latin-1 и падает на кириллице)
            self.session.auth = (login.encode('utf-8'), token.encode('utf-8'))
        self.session.verify = not accept_invalid_certs
        if accept_invalid_certs:
            import urllib3
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    @property
    def cloud(self):
        return self.instance_type == 'cloud'

    @property
    def batch(self):
        """Задач в одном поисковом запросе без журнала изменений."""
        return SEARCH_BATCH if self.cloud else SERVER_BATCH

    def stats(self):
        return f'{self.calls} запрос(ов) к Jira за {self.elapsed:.1f} с'

    def api(self, path):
        return f"{self.base_url}/rest/api/{'3' if self.cloud else '2'}/{path.lstrip('/')}"

    def structure_api(self, path):
        return f"{self.base_url}/rest/structure/2.0/{path.lstrip('/')}"

    def request(self, method, url, **kwargs):
        """Запрос с повтором при 429/503 (с учётом Retry-After) и понятными сообщениями об ошибках."""
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                # без перенаправлений: запросы с токеном идут только на адрес, указанный в подключении
                started = time.monotonic()
                self.calls += 1
                try:
                    resp = self.session.request(method, url, timeout=self.timeout, allow_redirects=False, **kwargs)
                finally:
                    self.elapsed += time.monotonic() - started
            except requests.exceptions.SSLError as e:
                self.fail('Ошибка TLS/сертификата. Если Jira использует самоподписанный или корпоративный '
                          f'сертификат, включите «Не проверять TLS-сертификат». ({e})',
                          'Ошибка TLS-сертификата Jira')
            except requests.exceptions.ConnectTimeout:
                self.fail(f'Превышено время ожидания подключения к {self.base_url}', 'Jira не отвечает')
            except requests.exceptions.ReadTimeout:
                raise JiraError('Jira не ответила вовремя — попробуйте ещё раз')
            except requests.exceptions.ConnectionError as e:
                self.fail(f'Не удалось подключиться к {self.base_url}: проверьте адрес и доступность сети ({e})',
                          'Не удалось подключиться к Jira')
            if resp.is_redirect:
                target = urlsplit(resp.headers.get('Location') or '')
                where = f' на {target.scheme}://{target.netloc}{target.path}' if target.netloc else ''
                self.fail(f'Jira перенаправляет запрос ({resp.status_code}){where} — укажите в подключении '
                          'итоговый адрес Jira (например, с https:// или с путём /jira)',
                          'Jira перенаправляет запросы на другой адрес — подключение нужно обновить')
            if resp.status_code in (429, 503) and attempt < MAX_ATTEMPTS:
                try:
                    delay = min(float(resp.headers.get('Retry-After', attempt)), 10.0)
                except ValueError:
                    delay = attempt
                time.sleep(delay)
                continue
            return resp
        return resp

    def json(self, method, url, not_found=None, kind=dict, **kwargs):
        resp = self.request(method, url, **kwargs)
        self.check(resp, url, not_found)
        return self.parse(resp, url, kind)

    def fail(self, detailed, short):
        """Ошибка подключения: подробно — администратору, кратко и без адреса Jira — остальным."""
        if self.detailed:
            raise JiraError(detailed)
        raise JiraError(short + ' — сообщите администратору (подключение настраивается в разделе «Интеграции»)')

    def where(self, url):
        """Адрес запроса в сообщении об ошибке: полностью — администратору, остальным — только путь API."""
        return url if self.detailed else urlsplit(url).path

    def parse(self, resp, url, kind=dict):
        """Тело ответа Jira: JSON ожидаемого вида (объект или список), иначе понятная ошибка — например,
        когда вместо Jira отвечает страница входа SSO или прокси."""
        try:
            data = resp.json()
        except ValueError:
            data = None
        if not isinstance(data, kind):
            self.fail(f'Jira вернула неожиданный ответ ({url}) — проверьте адрес Jira; '
                      'возможно, отвечает страница входа или прокси',
                      f'Jira вернула неожиданный ответ ({self.where(url)}): возможно, отвечает страница входа или прокси')
        return data

    def check(self, resp, url, not_found=None):
        if resp.status_code == 200:
            return
        denied = resp.headers.get('X-Authentication-Denied-Reason') or ''
        if resp.status_code in (401, 403) and 'CAPTCHA' in denied.upper():
            self.fail('Jira требует ввести CAPTCHA для этой учётной записи (было несколько неудачных входов): '
                      'войдите в Jira через браузер под этой учётной записью, пройдите проверку и повторите',
                      'Jira требует ввести CAPTCHA для учётной записи подключения')
        if resp.status_code == 401:
            self.fail('Jira отклонила логин/токен (401). Проверьте тип подключения, логин и пароль или токен; '
                      'если в Jira отключён вход по паролю (Basic), используйте Personal Access Token',
                      'Jira отклонила логин или пароль подключения (401)')
        if resp.status_code == 403:
            raise JiraError('Недостаточно прав в Jira (403)' + (f': {_error_text(resp)}' if _error_text(resp) else ''))
        if resp.status_code == 404 and not_found:
            raise JiraError(not_found)
        error = JiraQueryError if resp.status_code == 400 else JiraError
        raise error(f'Jira вернула ошибку {resp.status_code}: {_error_text(resp) or self.where(url)}')

    # ── операции ──
    def fields(self):
        """Все поля Jira: [{id, name, custom, schema}]."""
        return self.json('GET', self.api('field'), kind=list)

    def update_issue(self, key, fields):
        """Записать значения полей задачи (REST API v2 — простые значения и в Jira Cloud)."""
        url = f"{self.base_url}/rest/api/2/issue/{quote(key)}"
        resp = self.request('PUT', url, json={'fields': fields})
        if resp.status_code == 204:
            return
        self.check(resp, url, not_found=f'Задача {key} не найдена или нет прав на её просмотр')

    def myself(self):
        data = self.json('GET', self.api('myself'))
        return data.get('displayName') or data.get('name') or data.get('emailAddress') or ''

    def list_structures(self):
        data = self.json('GET', self.structure_api('structure'), params={'limit': 1000},
                         not_found='Плагин Structure недоступен в этой Jira (REST API Structure не найден)')
        return [{'id': s.get('id'), 'name': s.get('name') or f"Структура {s.get('id')}"}
                for s in (data.get('structures') or []) if isinstance(s, dict) and isinstance(s.get('id'), int)]

    def forest(self, structure_id, transformations=None):
        """Строки структуры [(rowId, depth, issueId|None)], с фильтром доски — видимые с ним; за срез — один раз."""
        cache = self.__dict__.setdefault('_forests', {})
        key = (structure_id, json.dumps(transformations, sort_keys=True) if transformations else '')
        if key not in cache:
            data = self.json('POST', self.structure_api('forest/latest'), json=forest_spec(structure_id, transformations),
                             not_found=f'Структура {structure_id} не найдена или плагин Structure недоступен')
            cache[key] = str(data.get('formula') or '')
        return parse_forest_rows(cache[key])

    def forest_items(self, structure_id, transformations=None):
        """Строки структуры с элементами [(rowId, depth, issueId|None, элемент)] — для разделов и мест задач."""
        self.forest(structure_id, transformations)
        key = (structure_id, json.dumps(transformations, sort_keys=True) if transformations else '')
        return parse_forest_items(self.__dict__['_forests'][key])

    def forest_rows(self, structure_id):
        return [(depth, issue_id) for _row, depth, issue_id in self.forest(structure_id)]

    def structure_values(self, structure_id, rows, attributes, transformations=None, keep_format=False):
        """Значения колонок Structure для строк структуры (Value API) → [{rowId: значение}] по порядку attributes.
        Формат значений — число: так Structure отдаёт значение колонки без оформления."""
        out = [dict() for _ in attributes]
        rows = sorted(set(rows))
        specs = [dict(a) if keep_format else dict(a, format='number') for a in attributes]
        for start in range(0, len(rows), 500):
            batch = rows[start:start + 500]
            data = self.json('POST', self.structure_api('value'), json={'requests': [
                {'forestSpec': forest_spec(structure_id, transformations), 'rows': batch, 'attributes': specs}]},
                not_found='API значений Structure недоступно — нужен Structure 4 или новее')
            for response in data.get('responses') or []:
                got_rows = response.get('rows') or batch
                for i, item in enumerate(response.get('data') or []):
                    if i >= len(out) or not isinstance(item, dict):
                        continue
                    for row_id, value in zip(got_rows, item.get('values') or []):
                        out[i][row_id] = value
        return out

    def issue(self, key, fields, changelog=False):
        """Задача по ключу (исходный JSON Jira); None, если её нет или нет прав на просмотр."""
        url = self.api(f'issue/{key}')
        params = {'fields': ','.join(fields)}
        if changelog:
            params['expand'] = 'changelog'
        resp = self.request('GET', url, params=params)
        if resp.status_code == 404:
            return None
        self.check(resp, url)
        return self.parse(resp, url)

    def _search_issues(self, jql, fields, expand_changelog=False, lenient=False):
        """Задачи по JQL в исходном виде Jira, постранично. lenient — в Server/DC ключи, которых нет или которые не видны
        учётной записи, не превращают весь запрос в ошибку (validateQuery: false), а просто не попадают в ответ."""
        found, start_at, token = 0, 0, None
        while True:
            body = {'jql': jql, 'fields': list(fields), 'maxResults': HISTORY_BATCH if expand_changelog else self.batch}
            if expand_changelog:
                body['expand'] = 'changelog' if self.cloud else ['changelog']
            if lenient and not self.cloud:
                body['validateQuery'] = False
            if self.cloud:
                if token:
                    body['nextPageToken'] = token
            else:
                body['startAt'] = start_at
            data = self.json('POST', self.api('search/jql' if self.cloud else 'search'), json=body)
            issues = data.get('issues', [])
            yield from issues
            found += len(issues)
            start_at += len(issues)
            token = data.get('nextPageToken')
            more = bool(token) if self.cloud else start_at < (data.get('total') or 0)
            if not issues or not more or found >= SEARCH_LIMIT:
                return

    def full_changelog(self, issue):
        """Журнал изменений задачи из ответа с expand=changelog; если Jira отдала его не целиком (Cloud), — дочитать."""
        log = issue.get('changelog') or {}
        histories = log.get('histories') or []
        if (log.get('total') or 0) > len(histories):
            histories = self.changelog(issue['key'])
        return histories

    def changelog(self, key):
        """Полный журнал изменений задачи."""
        if not self.cloud:
            data = self.json('GET', self.api(f'issue/{key}'), params={'expand': 'changelog', 'fields': 'created'})
            return (data.get('changelog') or {}).get('histories') or []
        out, start_at = [], 0
        while True:
            data = self.json('GET', self.api(f'issue/{key}/changelog'), params={'startAt': start_at, 'maxResults': 100})
            values = data.get('values') or []
            out.extend(values)
            start_at += len(values)
            if not values or data.get('isLast', True) or start_at >= (data.get('total') or 0):
                return out

    def issues_by_keys(self, keys, fields, changelog=False):
        """{запрошенный ключ: задача} одним поиском на пакет ключей. Ключи, которых нет в ответе (задачу переименовали,
        перенесли в другой проект, её нет или не видно), запрашиваются по одной — так переименованная задача находится
        по старому ключу, а для недоступной будет предупреждение."""
        out = {}
        keys = sorted(set(keys))
        size = HISTORY_BATCH if changelog else SEARCH_BATCH  # ключей в JQL: короткий запрос, понятная ошибка при отказе
        for start in range(0, len(keys), size):
            batch = keys[start:start + size]
            try:
                for issue in self._search_issues('key in (%s)' % ', '.join(batch), fields, changelog, lenient=True):
                    out[issue.get('key')] = issue
            except JiraQueryError:
                pass  # Jira Cloud не пропускает несуществующие ключи в JQL — тогда по одной
        for key in keys:
            if key not in out:
                issue = self.issue(key, fields, changelog)
                if issue is not None:
                    out[key] = issue
        return {k: out[k] for k in keys if k in out}

    def issues_by_ids(self, ids, fields, changelog=False, progress=None):
        """Задачи по ID пакетами (исходный JSON Jira); недоступные учётной записи — пропускаются.
        progress(сделано, всего) — после каждого пакета."""
        out = []
        ids = sorted(set(ids))
        size = HISTORY_BATCH if changelog else self.batch
        for start in range(0, len(ids), size):
            batch = ids[start:start + size]
            out.extend(self._search_issues('id in (%s)' % ','.join(str(i) for i in batch), fields, changelog))
            if progress:
                progress(min(start + size, len(ids)), len(ids))
        return out

    def children(self, parent_keys, fields, changelog=False):
        """Подзадачи (parent in …) для всех задач сразу, пакетами."""
        out = []
        keys = sorted(set(parent_keys))
        for start in range(0, len(keys), SEARCH_BATCH):
            out.extend(self._search_issues('parent in (%s)' % ', '.join(keys[start:start + SEARCH_BATCH]), fields, changelog))
        return out

    def statuses(self):
        """{id статуса: (название, категория)}."""
        data = self.json('GET', self.api('status'), kind=list)
        return {str(st.get('id')): (st.get('name') or '', (st.get('statusCategory') or {}).get('key') or 'new')
                for st in data if isinstance(st, dict)}

    def list_fields(self):
        """Поля Jira, пригодные для среза: учёт времени и числовые (в т. ч. пользовательские)."""
        data = self.json('GET', self.api('field'), kind=list)
        out = []
        for f in data:
            if not isinstance(f, dict):
                continue
            fid = f.get('id') or ''
            ftype = (f.get('schema') or {}).get('type')
            if fid in TIME_FIELD_NAMES or ftype == 'number':
                out.append({'id': fid, 'name': TIME_FIELD_NAMES.get(fid) or f.get('name') or fid,
                            'custom': bool(f.get('custom')), 'time': fid in DURATION_FIELDS})
        out.sort(key=lambda f: (not f['time'], f['id'] in AGGREGATE_FIELDS, f['name'].lower()))
        return out


def _error_text(resp):
    """Сообщения об ошибке из ответа Jira (errorMessages, errors, message). Произвольное тело ответа
    не показывается: адрес мог указывать не на Jira."""
    try:
        data = resp.json()
    except ValueError:
        return ''
    if not isinstance(data, dict):
        return ''
    messages = [str(m) for m in (data.get('errorMessages') or []) if isinstance(data.get('errorMessages'), list)]
    errors = data.get('errors')
    if isinstance(errors, dict):
        messages += [f'{k}: {v}' for k, v in errors.items()]
    if not messages and isinstance(data.get('message'), str):
        messages.append(data['message'])
    return '; '.join(messages)[:300]


# ─── Расчёт среза ────────────────────────────────────────────────

def _issue_fields(issue, key=None):
    return dict(issue.get('fields') or {}, __key=issue.get('key') or key)


def collect_issues(client, structure_id, mappings, fields, changelog=False, progress=None, scope=None):
    """Состав задач кнопки: запросы к Jira идут пакетами на всю кнопку, а не по эпикам —
    задачи-корни одним поиском, дерево структуры один раз, вложенные задачи всех эпиков одним поиском.

    → (группы по ассоциациям [{serviceId, keys, invalid, warnings, issues: {id: поля}, containers}],
       журналы изменений {id: [...]} при changelog=True). Контейнер — задача, вложенные задачи которой вошли в сумму
       этой же ассоциации (у ассоциации без «вложенных» контейнеров нет).
    """
    parsed = [(m, *parse_issue_keys(m.get('issueKeys'))) for m in mappings]
    found_roots = client.issues_by_keys({k for _m, keys, _inv in parsed for k in keys}, fields, changelog)
    histories = {}

    def take(issue, key=None):
        issue_id = int(issue['id'])
        if changelog and issue_id not in histories:
            histories[issue_id] = client.full_changelog(issue)
        return issue_id, _issue_fields(issue, key)

    groups = []
    for m, keys, invalid in parsed:
        group = {'serviceId': m['serviceId'], 'subtree': bool(m.get('includeSubtree')), 'keys': keys, 'invalid': invalid,
                 'warnings': [f'Некорректный ключ задачи: {k}' for k in invalid], 'roots': [], 'issues': {},
                 'containers': set()}
        for key in keys:
            if key not in found_roots:
                group['warnings'].append(f'{key}: задача не найдена или нет прав на просмотр')
                continue
            issue_id, f = take(found_roots[key], key)
            group['roots'].append((key, issue_id, f))
            group['issues'][issue_id] = f  # каждая задача учитывается один раз
        groups.append(group)

    nested = [g for g in groups if g['subtree'] and g['roots']]
    if nested and structure_id:
        rows = client.forest_rows(structure_id)
        items = client.forest_items(structure_id)
        places = issue_occurrences(items)
        wanted = scope_rows(items, scope)
        parents = parent_issue_ids(rows)  # задачи, у которых в структуре есть вложенные
        for g in nested:
            g['need'] = set()
            for key, issue_id, _f in g['roots']:
                occ, in_scope = pick_occurrence(places.get(issue_id), wanted)
                if occ is None:
                    g['warnings'].append(f'{key}: задачи нет в структуре — учтена только она сама')
                    continue
                # поддерево одной строки задачи (как на доске), а не всех её мест в структуре
                g['need'] |= subtree_issues_at(items, occ['row'])
                note = occurrence_warning(key, places[issue_id], occ, in_scope, wanted)
                if note:
                    g['warnings'].append(note)
        missing = set().union(*(g['need'] - set(g['issues']) for g in nested))
        fetched = dict(take(issue) for issue in client.issues_by_ids(missing, fields, changelog, progress)) if missing else {}
        for g in nested:
            want = g['need'] - set(g['issues'])
            g['issues'].update({i: fetched[i] for i in want if i in fetched})
            g['containers'] = parents & set(g['issues'])
            hidden = len(want) - len(want & set(fetched))
            if hidden:
                g['warnings'].append(f'{hidden} задач(и) структуры недоступны для просмотра — не учтены')
    elif nested:
        by_parent = {}
        for issue in client.children({f['__key'] for g in nested for _k, _i, f in g['roots']}, fields, changelog):
            issue_id, f = take(issue)
            parent = f.get('parent') if isinstance(f.get('parent'), dict) else {}
            by_parent.setdefault(parent.get('key'), {})[issue_id] = f
        for g in nested:
            for _key, issue_id, f in g['roots']:
                kids = by_parent.get(f['__key'], {})
                if kids:
                    g['containers'].add(issue_id)
                g['issues'].update({i: c for i, c in kids.items() if i not in g['issues']})
    return groups, histories


def without_subtasks(issues, field_map):
    """Σ-поле Jira у задачи включает её подзадачи: если подзадачи тоже в составе эпика, у родителя остаётся только своя
    часть (Σ минус подзадачи из состава) — иначе в сумме по эпику и в составе задач они учитываются дважды.
    issues — {issueId: поля задачи} → такой же словарь (изменённые задачи — копии)."""
    fields = {spec['field'] for spec in field_map.values() if spec['field'] in AGGREGATE_FIELDS}
    if not fields:
        return issues
    by_key = {f.get('__key'): i for i, f in issues.items()}
    children = {}
    for issue_id, f in issues.items():
        kind = f.get('issuetype') if isinstance(f.get('issuetype'), dict) else {}
        parent = (f.get('parent') if isinstance(f.get('parent'), dict) else {}).get('key')
        if kind.get('subtask') and parent in by_key and by_key[parent] != issue_id:
            children.setdefault(by_key[parent], []).append(f)
    if not children:
        return issues
    out = dict(issues)
    for parent_id, kids in children.items():
        f = dict(out[parent_id])
        for field in fields:
            own, sub = _number(f.get(field)), [_number(k.get(field)) for k in kids]
            if own is not None:
                f[field] = max(own - sum(v for v in sub if v is not None), 0)
        out[parent_id] = f
    return out


def _snapshot_item(group, field_map):
    """Группа задач ассоциации → строка среза: суммы показателей, задачи и предупреждения."""
    warnings = list(group['warnings'])
    values = {}
    issues = without_subtasks(group['issues'], field_map)
    for metric in METRICS:
        spec = field_map[metric]
        if not spec['field']:
            values[metric] = None
            continue
        raw = [f.get(spec['field']) for f in issues.values()]
        values[metric] = sum_hours(raw, spec['unit'])
        if values[metric] is None and any(v not in (None, '') for v in raw):
            warnings.append(f"Поле {spec['field']} не числовое — метрика не заполнена")
    if not group['keys'] and not group['invalid']:
        warnings.append('Не указаны задачи')
    issues = [issue_detail(f, field_map, issue_id in group['containers']) for issue_id, f in issues.items()]
    issues.sort(key=lambda i: issue_sort_key(i['key']))
    return dict(serviceId=group['serviceId'], issueCount=len(group['issues']), warnings=warnings, issues=issues, **values)


def compute_snapshot(client, structure_id, mappings, field_map=None, transformations=None, scope=None):
    """Срез для кнопки.

    mappings: [{'serviceId', 'issueKeys': 'ABC-1, ABC-2', 'includeSubtree': bool}]
    field_map: {metric: {'field', 'unit', 'structure'}} (по умолчанию — учёт времени Jira)
    → [{'serviceId', 'remaining', 'spent', 'estimate', 'issueCount', 'warnings': [...]}]
    """
    field_map = field_map or DEFAULT_FIELD_MAP
    fields = sorted({v['field'] for v in field_map.values() if v['field']} | set(ISSUE_FIELDS))
    groups, _histories = collect_issues(client, structure_id, mappings, fields, scope=scope)
    items = [_snapshot_item(g, field_map) for g in groups]
    columns = {m: field_map[m]['structure'] for m in METRICS if field_map[m].get('structure')}
    if columns:
        apply_structure_columns(client, structure_id, groups, items, columns, transformations, scope)
        apply_structure_issue_values(client, structure_id, groups, items, field_map, scope)
    return items


def own_attribute(column):
    """Та же колонка Structure по самой задаче, без суммы по вложенным → атрибут или None (если не понять, как
    убрать сумму из готовой спецификации)."""
    if column.get('editor'):
        return column_variants(column['editor'])['own']
    attr = column.get('attribute') or {}
    params = attr.get('params') if isinstance(attr.get('params'), dict) else {}
    if attr.get('id') == 'sum' and isinstance(params.get('attribute'), dict):
        return params['attribute']
    formula = params.get('formula')
    if attr.get('id') == 'expr' and isinstance(formula, str):
        text = formula.strip()
        match = re.match(r'^SUM(?:#[A-Za-z]+)?\s*\{(.*)\}$', text, re.S)
        if match:
            return dict(attr, params=dict(params, formula=match.group(1).strip()))
        return None if 'SUM' in text.upper() else attr
    return attr if attr.get('id') and attr.get('id') not in ('sum', 'expr') else None  # колонка без суммы — уже «сама задача»


def apply_structure_issue_values(client, structure_id, groups, items, field_map, scope=None):
    """Показатели задач в составе эпика для метрик из колонок Structure, у которых не задано поле Jira: значение той же
    колонки у строки задачи без суммы по вложенным (для формулы — формула по самой задаче). Строка задачи берётся
    из поддерева выбранной строки эпика (раздел структуры), задача, стоящая в нём несколько раз, — один раз."""
    derive = {}
    for metric in METRICS:
        spec = field_map[metric]
        if spec.get('structure') and not spec['field']:
            attr = own_attribute(spec['structure'])
            if attr:
                derive[metric] = (attr, spec['structure']['unit'])
    if not derive or not structure_id:
        return
    forest = client.forest_items(structure_id)
    places = issue_occurrences(forest)
    wanted = scope_rows(forest, scope)
    row_of = []
    for g in groups:
        rows = {}
        for _key, issue_id, _f in g['roots']:
            occ, _in_scope = pick_occurrence(places.get(issue_id), wanted)
            if occ is None:
                continue
            for rid, iid in (subtree_rows(forest, occ['row']) if g['subtree'] else [(occ['row'], issue_id)]):
                if iid in g['issues']:
                    rows.setdefault(iid, rid)
        row_of.append(rows)
    specs, slot = [], {}
    for metric, (attr, _unit) in derive.items():
        key = json.dumps(attr, sort_keys=True, ensure_ascii=False)
        if key not in slot:
            slot[key] = len(specs)
            specs.append(attr)
        derive[metric] = (slot[key], derive[metric][1])
    all_rows = sorted({r for rows in row_of for r in rows.values()})
    if not all_rows:
        return
    try:
        values = client.structure_values(structure_id, all_rows, specs)
    except JiraError as e:
        for item in items:
            item['warnings'].append(f'Показатели задач из колонок Structure не получены: {e}')
        return
    derived = set(derive)
    for g, item, rows in zip(groups, items, row_of):
        id_of = {f.get('__key'): issue_id for issue_id, f in g['issues'].items()}
        for detail in item.get('issues') or []:
            row = rows.get(id_of.get(detail['key']))
            for metric, (index, unit) in derive.items():
                num = _number(values[index].get(row)) if row is not None else None
                detail[metric] = None if num is None else round(num * UNITS[unit], 2)
            detail['problems'] = issue_problems(detail, field_map, derived)


def _number(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        text = value.strip().replace('\u00a0', '').replace(' ', '').replace(',', '.')
        try:
            return float(text)
        except ValueError:
            return None
    return None


def apply_structure_columns(client, structure_id, groups, items, columns, transformations=None, scope=None):
    """Показатели из колонок Structure: значение колонки у строки задачи эпика (как в структуре); у эпика из
    нескольких задач — сумма их строк. Колонки «После фильтрации» считаются с фильтром доски (transformations),
    остальные — по всей структуре. Задача, которой нет в структуре, в показатель не входит (с предупреждением)."""
    if not structure_id:
        for item in items:
            for metric in columns:
                item[metric] = None
            item['warnings'].append('Колонки Structure работают только у кнопки со структурой — показатели из них не заполнены')
        return
    plain = {m: c for m, c in columns.items() if not (c.get('afterFiltering') and transformations)}
    filtered = {m: c for m, c in columns.items() if c.get('afterFiltering') and transformations}
    if any(c.get('afterFiltering') for c in columns.values()) and not transformations:
        for item in items:
            item['warnings'].append('Колонка отмечена «После фильтрации», но фильтр доски у кнопки не задан — '
                                    'посчитано по всей структуре')
    if plain:
        _apply_columns(client, structure_id, groups, items, plain, None, scope)
    if filtered:
        _apply_columns(client, structure_id, groups, items, filtered, transformations, scope)


def _apply_columns(client, structure_id, groups, items, columns, transformations, scope=None):
    forest = client.forest_items(structure_id, transformations)
    places = issue_occurrences(forest)
    wanted = scope_rows(forest, scope)
    specs, index, slot = [], {}, {}
    for metric, col in columns.items():
        key = json.dumps(col['attribute'], sort_keys=True, ensure_ascii=False)
        if key not in index:
            index[key] = len(specs)
            specs.append(col['attribute'])
        slot[metric] = index[key]
    need, hidden = [], []
    for g, item in zip(groups, items):
        rows, gone = [], False
        for key, issue_id, _f in g['roots']:
            occ, in_scope = pick_occurrence(places.get(issue_id), wanted)
            if occ is None:
                if transformations:
                    item['warnings'].append(f'{key}: задача скрыта фильтром доски — для колонок «После фильтрации» '
                                            'её значение 0')
                    gone = True
                else:
                    item['warnings'].append(f'{key}: задачи нет в структуре — колонки Structure для неё не посчитаны')
                continue
            note = occurrence_warning(key, places[issue_id], occ, in_scope, wanted)
            if note and note not in item['warnings']:
                item['warnings'].append(note)
            rows.append(occ['row'])
        need.append(rows)
        hidden.append(gone)
    all_rows = sorted({r for rows in need for r in rows})
    values = client.structure_values(structure_id, all_rows, specs, transformations) if all_rows else [{} for _ in specs]
    for rows, item, gone in zip(need, items, hidden):
        for metric, col in columns.items():
            raw = [values[slot[metric]].get(row) for row in rows]
            nums = [n for n in (_number(v) for v in raw) if n is not None]
            if not nums and gone and not rows:
                nums = [0]  # на доске с фильтром эпика не видно — в сумме «После фильтрации» его нет
            item[metric] = round(sum(nums) * UNITS[col['unit']], 2) if nums else None
            if not nums and any(v not in (None, '') for v in raw):
                item['warnings'].append(f"Колонка Structure «{col.get('label') or col['attribute']['id']}» вернула не число — "
                                        'проверьте её формат')


def row_titles(client, structure_id, rows, transformations=None):
    """Названия строк структуры (ключ и тема задачи, название папки) → {rowId: текст}."""
    if not rows:
        return {}
    try:
        summary, key = client.structure_values(structure_id, rows, [{'id': 'summary', 'format': 'text'},
                                                                    {'id': 'key', 'format': 'text'}],
                                               transformations, keep_format=True)
    except JiraError:
        return {}
    out = {}
    for r in rows:
        k, t = key.get(r), summary.get(r)
        out[r] = ' '.join(str(x) for x in (k, t) if x not in (None, '')) or f'строка {r}'
    return out


def structure_sections(client, structure_id, limit=300):
    """Разделы структуры для выбора у кнопки: строки верхних двух уровней, под которыми есть вложенные строки.
    У строки второго уровня parentRow — строка над ней: один и тот же эпик может стоять в нескольких разделах."""
    items = client.forest_items(structure_id)
    out, top = [], None
    for i, (row_id, depth, issue_id, item) in enumerate(items):
        if depth == 0:
            top = row_id
        if depth > 1:
            continue
        nxt = items[i + 1] if i + 1 < len(items) else None
        if not nxt or nxt[1] <= depth:
            continue
        size = 0
        for _r, d, _iid, _it in items[i + 1:]:
            if d <= depth:
                break
            size += 1
        out.append({'item': item, 'rowId': row_id, 'depth': depth, 'rows': size, 'issue': issue_id is not None,
                    'parentRow': top if depth else None})
        if len(out) >= limit:
            break
    rows = {x['rowId'] for x in out} | {x['parentRow'] for x in out if x['parentRow'] is not None}
    titles = row_titles(client, structure_id, sorted(rows))
    for x in out:
        x['title'] = titles.get(x['rowId']) or x['item']
        x['parentTitle'] = titles.get(x.pop('parentRow')) if x['depth'] else None
    return out


def structure_preview(client, structure_id, keys, attribute, unit, variants=None, transformations=None,
                      after_filtering=False, scope=None, variables=None):
    """Значения колонки Structure у строк задач (проверка в редакторе кнопки) и данные для сверки с доской:
    [{'key', 'rowId', 'raw', 'hours', 'asUnits', 'variants', 'places', 'vars'}] и предупреждения.

    variants — {имя: атрибут}: те же значения в других вариантах (без суммы, Σ без и с дубликатами — по всей
    структуре; «filtered» — как настроено, с фильтром доски). places — все места задачи в структуре с путём и значением
    колонки в каждом: если задача стоит в нескольких местах, у каждой строки своё поддерево. vars — значение каждой
    переменной формулы у выбранной строки (видно, какое поле Structure не отдаёт)."""
    found = client.issues_by_keys(keys, ['summary'])
    items = client.forest_items(structure_id)
    places = issue_occurrences(items)
    wanted = scope_rows(items, scope)
    rows, warnings, ids = [], [], {}
    for key in keys:
        issue = found.get(key)
        occ, in_scope = pick_occurrence(places.get(int(issue['id'])), wanted) if issue else (None, False)
        if issue is None:
            warnings.append(f'{key}: задача не найдена или нет прав на просмотр')
        elif occ is None:
            warnings.append(f'{key}: задачи нет в структуре')
        else:
            rows.append((key, occ['row']))
            ids[key] = int(issue['id'])
            note = occurrence_warning(key, places[ids[key]], occ, in_scope, wanted)
            if note:
                warnings.append(note)

    def values_for(spec, tf, name=None):
        """{ключ: значение} по выбранным строкам задач — в структуре с фильтром tf (или без него)."""
        if tf:
            forest = client.forest_items(structure_id, tf)
            fplaces, fwanted = issue_occurrences(forest), scope_rows(forest, scope)
            pairs = [(k, (pick_occurrence(fplaces.get(ids[k]), fwanted)[0] or {}).get('row')) for k, _r in rows]
        else:
            pairs = rows
        present = [(k, r) for k, r in pairs if r is not None]
        try:
            got = client.structure_values(structure_id, [r for _k, r in present], [spec], tf)[0] if present else {}
        except JiraError as e:
            if name is None:
                raise
            warnings.append(f'Вариант для сверки «{VARIANT_TITLES.get(name, name)}» не посчитан: {e}')
            return {}
        return {k: got.get(r) for k, r in present}
    main_tf = transformations if after_filtering else None
    values = values_for(attribute, main_tf) if rows else {}
    if after_filtering and not transformations:
        warnings.append('Колонка отмечена «После фильтрации», но фильтр доски у кнопки не задан — посчитано по всей структуре')
    if main_tf:
        for key in [k for k, _r in rows if k not in values]:
            warnings.append(f'{key}: задача скрыта фильтром доски')
    variant_specs = dict(variants or {})
    if transformations and variants:
        variant_specs['filtered'] = attribute
    extra = {name: (values_for(spec, transformations if name == 'filtered' else None, name) if rows else {})
             for name, spec in variant_specs.items()}
    # все места задач в структуре: путь и значение колонки у каждой строки
    all_places = {k: places.get(ids[k], []) for k, _r in rows}
    place_rows = sorted({p['row'] for occ in all_places.values() for p in occ})
    path_rows = sorted({r for occ in all_places.values() for p in occ for r in p['pathRows']})
    show_paths = wanted is not None or any(len(o) > 1 for o in all_places.values())
    titles = row_titles(client, structure_id, path_rows) if show_paths else {}
    try:
        at_place = client.structure_values(structure_id, place_rows, [attribute])[0] if place_rows else {}
    except JiraError:
        at_place = {}
    # значения переменных формулы у выбранных строк — в их собственном формате
    var_values = {}
    for name, spec in (variables or {}).items():
        try:
            var_values[name] = client.structure_values(structure_id, [r for _k, r in rows], [spec],
                                                       keep_format=True)[0] if rows else {}
        except JiraError as e:
            var_values[name] = {}
            warnings.append(f'Переменная «{name}»: Structure не отдал значение поля ({e})')
    out = []
    for key, row in rows:
        raw = values.get(key)
        num = _number(raw)
        item = {'key': key, 'rowId': row, 'raw': raw,
                'hours': None if num is None else round(num * UNITS[unit], 2),
                'asUnits': {u: (None if num is None else round(num * k, 2)) for u, k in UNITS.items()},
                'places': [{'rowId': p['row'], 'depth': p['depth'], 'chosen': p['row'] == row,
                            'path': [titles.get(r, '') for r in p['pathRows']], 'raw': at_place.get(p['row'])}
                           for p in all_places[key]]}
        if variant_specs:
            item['variants'] = {name: _number(extra[name].get(key)) for name in variant_specs}
        if variables:
            item['vars'] = {name: var_values[name].get(row) for name in variables}
        out.append(item)
    return out, warnings


VARIANT_TITLES = {'own': 'только сама задача', 'distinct': 'Σ без дубликатов', 'all': 'Σ с дубликатами',
                  'filtered': 'с фильтром доски'}


# ─── Состав эпика по задачам и качество данных ───────────────────

def parent_issue_ids(rows):
    """Задачи структуры, под которыми есть вложенные строки (эпики, «папки»-задачи)."""
    out = set()
    for (depth, item), nxt in zip(rows, rows[1:]):
        if item is not None and nxt[0] > depth:
            out.add(item)
    return out


def issue_sort_key(key):
    """ABC-2 раньше ABC-10."""
    prefix, _, num = (key or '').rpartition('-')
    return (prefix, int(num) if num.isdigit() else 0, key or '')


def issue_detail(f, field_map, container=False):
    """Поля задачи → строка среза: ключ, тема, статус, категория статуса, показатели (ч) и проблемы данных."""
    status = f.get('status') if isinstance(f.get('status'), dict) else {}
    category = (status.get('statusCategory') or {}).get('key')
    out = {'key': f.get('__key') or '', 'summary': str(f.get('summary') or '')[:500],
           'status': str(status.get('name') or ''),
           'category': category if category in STATUS_CATEGORIES else 'new', 'container': bool(container)}
    for metric in METRICS:
        spec = field_map[metric]
        out[metric] = sum_hours([f.get(spec['field'])], spec['unit']) if spec['field'] else None
    out['problems'] = issue_problems(out, field_map)
    return out


def issue_problems(issue, field_map, derived=()):
    """Признаки, из-за которых сумма по эпику может вводить в заблуждение (только по сопоставленным полям; derived —
    метрики, значения которых взяты из колонок Structure)."""
    mapped = {m: bool(field_map[m]['field']) or m in derived for m in METRICS}
    rem, spent, est = issue['remaining'], issue['spent'], issue['estimate']
    done = issue['category'] == 'done'
    out = []
    if done and mapped['remaining'] and (rem or 0) > 0:
        out.append('done_with_remaining')
    if not done and not issue['container'] and (mapped['remaining'] or mapped['estimate']) \
            and not ((rem or 0) > 0 or (est or 0) > 0):
        out.append('no_estimate')
    if issue['category'] == 'indeterminate' and mapped['remaining'] and not (rem or 0) > 0 and (est or 0) > 0:
        out.append('zero_remaining')
    if mapped['spent'] and mapped['estimate'] and (est or 0) > 0 and (spent or 0) > est:
        out.append('overspent')
    return out


def issue_measured(issue):
    """Есть ли у задачи в срезе хоть один показатель (часы): без них объём задачи — не 0, а «нет данных»."""
    return any(issue.get(m) is not None for m in METRICS)


def issue_volume(issue):
    """Объём задачи для burn-up: оценка, а если её нет — затрачено + остаток."""
    if issue.get('estimate') is not None:
        return issue['estimate']
    return (issue.get('spent') or 0) + (issue.get('remaining') or 0)


# ─── Запись прогноза в задачи Jira ───────────────────────────────

WRITABLE_TYPES = {'date': 'дата', 'datetime': 'дата и время', 'number': 'число', 'string': 'текст', 'option': 'список'}
# какие типы полей подходят для каждого показателя
WRITEBACK_KINDS = {
    'forecast': ('Прогноз завершения', ('date', 'datetime', 'string')),
    'status': ('Статус сроков', ('string', 'option')),
    'chance': ('Шанс успеть, %', ('number', 'string')),
    'headline': ('Главный вывод', ('string',)),
}


def writable_fields(raw):
    """Пользовательские поля Jira, в которые можно записать прогноз: [{id, name, type}] по имени."""
    out = []
    for f in raw or []:
        if not isinstance(f, dict) or not f.get('custom'):
            continue
        schema = f.get('schema') or {}
        kind = schema.get('type')
        if kind in WRITABLE_TYPES and isinstance(f.get('id'), str) and FIELD_ID_RE.match(f['id']):
            out.append({'id': f['id'], 'name': str(f.get('name') or f['id']), 'type': kind,
                        'typeLabel': WRITABLE_TYPES[kind]})
    return sorted(out, key=lambda x: x['name'].lower())


def field_value(kind, value):
    """Значение для поля Jira данного типа (None — очистить поле)."""
    if value is None or value == '':
        return None
    if kind == 'date':
        return str(value)
    if kind == 'datetime':
        return f'{value}T00:00:00.000+0000'
    if kind == 'number':
        return float(value)
    if kind == 'option':
        return {'value': str(value)}
    return str(value)[:250]


def connection_json(row):
    """Строка jira_connections → данные для интерфейса (без токена)."""
    if not row:
        return None
    return {'baseUrl': row['base_url'], 'instanceType': row['instance_type'], 'login': row['login'] or '',
            'acceptInvalidCerts': bool(row['accept_invalid_certs']), 'hasToken': bool(row['token_encrypted'])}



# ─── Восстановление истории по журналу изменений Jira ────────────

def parse_jira_time(value):
    """'2026-07-17T10:15:30.000+0300' → datetime с часовым поясом."""
    for fmt in ('%Y-%m-%dT%H:%M:%S.%f%z', '%Y-%m-%dT%H:%M:%S%z'):
        try:
            return datetime.strptime(value, fmt)
        except (TypeError, ValueError):
            continue
    return None


def _history_value(item, side):
    """Значение из записи журнала: from/to (сырое), иначе fromString/toString; число или None."""
    raw = item.get(side)
    if raw in (None, ''):
        raw = item.get(side + 'String')
    try:
        return float(raw) if raw not in (None, '') else None
    except (TypeError, ValueError):
        return None


def issue_state_at(fields, histories, field_ids, moment):
    """Значения полей и id статуса задачи на момент moment (откат журнала изменений от текущего состояния).

    → (значения, id статуса, поля, по которым в журнале были изменения) или None, если задачи ещё не было.
    """
    created = parse_jira_time(fields.get('created'))
    if created and created > moment:
        return None
    values = {f: fields.get(f) for f in field_ids}
    status_id = str((fields.get('status') or {}).get('id') or '')
    seen = set()
    timed = [(parse_jira_time(h.get('created')), h) for h in histories]
    for when, h in sorted((t for t in timed if t[0]), key=lambda t: t[0], reverse=True):
        if when <= moment:
            break
        for item in h.get('items') or []:
            fid = item.get('fieldId') or item.get('field')
            if fid == 'status':
                status_id = str(item.get('from') or status_id)
            elif fid in values:
                values[fid] = _history_value(item, 'from')
                seen.add(fid)
    for h in histories:  # поля, по которым журнал вообще есть (для предупреждений о пользовательских полях)
        for item in h.get('items') or []:
            seen.add(item.get('fieldId') or item.get('field'))
    return values, status_id, seen


def compute_history(client, structure_id, mappings, field_map, moments, progress=None, scope=None):
    """Срезы на прошлые моменты по текущему составу задач кнопки.

    moments: {дата 'YYYY-MM-DD': datetime конца дня}. → ({дата: items как у compute_snapshot}, предупреждения).
    Состав берётся текущий: задачи, добавленные в структуру позже, учитываются с даты создания.
    """
    field_map = field_map or DEFAULT_FIELD_MAP
    field_ids = sorted({v['field'] for v in field_map.values() if v['field']})
    # текущий состав и журналы изменений — одними и теми же запросами (задачи не загружаются дважды)
    groups, histories = collect_issues(client, structure_id, mappings,
                                       sorted(set(field_ids) | set(ISSUE_FIELDS) | {'created'}), True, progress, scope)
    current = [_snapshot_item(g, field_map) for g in groups]
    keys = {i['key'] for it in current for i in it['issues']}
    data = {f['__key']: (f, histories.get(issue_id, [])) for g in groups for issue_id, f in g['issues'].items()}
    statuses = client.statuses()
    warnings = sorted({w for it in current for w in it['warnings']})
    with_history = set()
    out = {}
    for day, moment in sorted(moments.items()):
        items = []
        for it in current:
            issues = []
            raw = {metric: [] for metric in METRICS}  # значения полей до перевода в часы — округление один раз, как в срезе
            for cur in it['issues']:
                if cur['key'] not in data:
                    continue
                fields, histories = data[cur['key']]
                state = issue_state_at(fields, histories, field_ids, moment)
                if state is None:
                    continue
                values, status_id, seen = state
                with_history |= seen
                name, category = statuses.get(status_id, (cur['status'], cur['category']))
                f = dict(values, __key=cur['key'], summary=cur['summary'],
                         status={'name': name, 'statusCategory': {'key': category}})
                issues.append(issue_detail(f, field_map, cur['container']))
                for metric in METRICS:
                    if field_map[metric]['field']:
                        raw[metric].append(values.get(field_map[metric]['field']))
            totals = {}
            for metric in METRICS:
                spec = field_map[metric]
                totals[metric] = sum_hours(raw[metric], spec['unit']) if spec['field'] else None
            items.append(dict(serviceId=it['serviceId'], issueCount=len(issues), warnings=[], issues=issues, **totals))
        out[day] = items
    if keys and not any(it['issueCount'] for items in out.values() for it in items):
        warnings.insert(0, 'На выбранные даты задач кнопки в Jira ещё не было (созданы позже) — восстанавливать нечего')
    for fid in field_ids:
        if fid not in TIME_FIELDS.values() and fid not in with_history:
            warnings.append(f'По полю {fid} изменений в журнале Jira не найдено — для прошлых дат взято текущее значение')
    return out, warnings


# ─── Эпики проекта из Jira: кандидаты для синхронизации и мастера подключения ───

EPIC_FIELDS = ('summary', 'status', 'duedate', 'fixVersions')
EPIC_TARGETS = ('duedate', 'fixVersion', 'none')
EPIC_LIMIT = 300  # эпиков в одном проекте — с запасом; больше — вероятно, выбран не тот уровень структуры


def epic_target(fields, source):
    """Целевая дата эпика из Jira: срок задачи (duedate) или самая поздняя дата выпуска её версий (fixVersions)."""
    if source == 'duedate':
        due = fields.get('duedate')
        return due[:10] if isinstance(due, str) and len(due) >= 10 else None
    if source == 'fixVersion':
        dates = [v.get('releaseDate') for v in fields.get('fixVersions') or [] if isinstance(v, dict)]
        dates = [d[:10] for d in dates if isinstance(d, str) and len(d) >= 10]
        return max(dates) if dates else None
    return None


def _epic_item(issue, source, stage=None):
    f = issue.get('fields') or {}
    status = f.get('status') if isinstance(f.get('status'), dict) else {}
    category = (status.get('statusCategory') or {}).get('key') or 'new'
    return {'key': issue.get('key'), 'id': int(issue['id']), 'summary': (f.get('summary') or issue.get('key') or '').strip(),
            'status': status.get('name') or '', 'done': category == 'done', 'target': epic_target(f, source),
            'stage': stage}


def epic_candidates(client, structure_id=None, level=0, jql=None, target='duedate'):
    """Задачи-эпики проекта в Jira → [{key, id, summary, status, done, target, stage}] в порядке структуры.

    Из структуры берутся задачи на уровне level (0 — верхний; 1 — вложенные в задачи верхнего уровня, тогда задача
    верхнего уровня — этап эпика). Без структуры — задачи по JQL. Папки и прочие элементы Structure пропускаются.
    """
    if target not in EPIC_TARGETS:
        raise JiraError('Источник целевой даты — срок задачи, версия или «не заполнять»')
    if structure_id:
        rows = client.forest_rows(structure_id)
        picked, parents, parent = [], {}, None
        for depth, issue_id in rows:
            if depth == 0:
                parent = issue_id
            if depth == level and issue_id is not None and issue_id not in parents:
                parents[issue_id] = parent if level else None
                picked.append(issue_id)
        if len(picked) > EPIC_LIMIT:
            raise JiraError(f'На этом уровне структуры {len(picked)} задач — больше {EPIC_LIMIT}. '
                            'Выберите другой уровень эпиков')
        need = set(picked) | {p for p in parents.values() if p is not None}
        issues = {int(i['id']): i for i in client.issues_by_ids(need, EPIC_FIELDS)} if need else {}
        stage_names = {i: (issues[i].get('fields') or {}).get('summary') for i in issues}
        return [_epic_item(issues[i], target, stage_names.get(parents[i]) if parents[i] else None)
                for i in picked if i in issues]
    jql = (jql or '').strip()
    if not jql:
        raise JiraError('Укажите структуру или запрос JQL, по которому найти эпики')
    out = []
    for issue in client._search_issues(jql, EPIC_FIELDS):
        out.append(_epic_item(issue, target))
        if len(out) > EPIC_LIMIT:
            raise JiraError(f'Запрос нашёл больше {EPIC_LIMIT} задач — уточните JQL (например, добавьте issuetype = Epic)')
    return out
