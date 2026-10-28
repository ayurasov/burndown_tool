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
"""

import base64
import hashlib
import re
import time

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
ISSUE_FIELDS = ('summary', 'status', 'parent')  # для состава эпика по задачам
STATUS_CATEGORIES = ('new', 'indeterminate', 'done')
PROBLEMS = {
    'no_estimate': 'Не закрыта, нет оценки и остатка',
    'done_with_remaining': 'Закрыта, но остаток не обнулён',
    'zero_remaining': 'В работе, остаток 0 при ненулевой оценке',
    'overspent': 'Затрачено больше оценки',
}
TIME_FIELDS = {'remaining': 'timeestimate', 'spent': 'timespent', 'estimate': 'timeoriginalestimate'}
TIME_FIELD_NAMES = {'timeestimate': 'Оставшаяся оценка', 'timespent': 'Затраченное время',
                    'timeoriginalestimate': 'Первоначальная оценка'}
# единица значения поля Jira → множитель для перевода в часы
UNITS = {'seconds': 1 / 3600, 'hours': 1.0}
DEFAULT_FIELD_MAP = {m: {'field': TIME_FIELDS[m], 'unit': 'seconds'} for m in METRICS}
FIELD_ID_RE = re.compile(r'^[A-Za-z][A-Za-z0-9_]*$')
SEARCH_BATCH = 100
SEARCH_LIMIT = 5000
MAX_ATTEMPTS = 3


class JiraError(Exception):
    """Ошибка обращения к Jira с понятным пользователю текстом."""


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


def subtree_issue_ids(rows, root_issue_id):
    """ID задач в поддеревьях всех вхождений root_issue_id (включая сам корень); None — корня нет в структуре."""
    found, ids = False, set()
    i = 0
    while i < len(rows):
        depth, issue_id = rows[i]
        if issue_id == root_issue_id:
            found = True
            ids.add(issue_id)
            j = i + 1
            while j < len(rows) and rows[j][0] > depth:
                if rows[j][1] is not None:
                    ids.add(rows[j][1])
                j += 1
        i += 1
    return ids if found else None


def seconds_to_hours(values):
    """Сумма секунд в часы (2 знака); None, если ни у одной задачи поле не заполнено."""
    return sum_hours(values, 'seconds')


def sum_hours(values, unit):
    """Сумма числовых значений поля в часах (2 знака); None, если ни у одной задачи поле не заполнено."""
    filled = [v for v in values if isinstance(v, (int, float)) and not isinstance(v, bool)]
    return round(sum(filled) * UNITS[unit], 2) if filled else None


def normalize_field_map(raw):
    """Проверенное сопоставление метрик и полей: {metric: {'field': id|'', 'unit': 'seconds'|'hours'}}.

    Пустое поле — метрику не заполнять. Некорректные значения → ValueError с понятным текстом.
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
            raise ValueError('Единица измерения — секунды или часы')
        out[metric] = {'field': field, 'unit': unit}
    if not any(v['field'] for v in out.values()):
        raise ValueError('Сопоставьте с полями Jira хотя бы одну метрику')
    return out


# ─── HTTP-клиент Jira ────────────────────────────────────────────

class JiraClient:
    def __init__(self, base_url, instance_type, login, token, accept_invalid_certs=False, timeout=(15, 60)):
        if instance_type not in INSTANCE_TYPES:
            raise JiraError('Неизвестный тип подключения к Jira')
        self.base_url = (base_url or '').strip().rstrip('/')
        if not re.match(r'^https?://', self.base_url):
            raise JiraError('Адрес Jira должен начинаться с http:// или https://')
        _require_libs()
        self.instance_type = instance_type
        self.timeout = timeout
        self.session = requests.Session()  # прокси из окружения сервера подхватываются автоматически
        self.session.headers.update({'Accept': 'application/json'})
        if instance_type == 'server':
            self.session.headers['Authorization'] = 'Bearer ' + token
        else:
            self.session.auth = (login, token)
        self.session.verify = not accept_invalid_certs
        if accept_invalid_certs:
            import urllib3
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    @property
    def cloud(self):
        return self.instance_type == 'cloud'

    def api(self, path):
        return f"{self.base_url}/rest/api/{'3' if self.cloud else '2'}/{path.lstrip('/')}"

    def structure_api(self, path):
        return f"{self.base_url}/rest/structure/2.0/{path.lstrip('/')}"

    def request(self, method, url, **kwargs):
        """Запрос с повтором при 429/503 (с учётом Retry-After) и понятными сообщениями об ошибках."""
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                resp = self.session.request(method, url, timeout=self.timeout, **kwargs)
            except requests.exceptions.SSLError as e:
                raise JiraError('Ошибка TLS/сертификата. Если Jira использует самоподписанный или корпоративный '
                                f'сертификат, включите «Не проверять TLS-сертификат». ({e})')
            except requests.exceptions.ConnectTimeout:
                raise JiraError(f'Превышено время ожидания подключения к {self.base_url}')
            except requests.exceptions.ReadTimeout:
                raise JiraError('Jira не ответила вовремя — попробуйте ещё раз')
            except requests.exceptions.ConnectionError as e:
                raise JiraError(f'Не удалось подключиться к {self.base_url}: проверьте адрес и доступность сети ({e})')
            if resp.status_code in (429, 503) and attempt < MAX_ATTEMPTS:
                try:
                    delay = min(float(resp.headers.get('Retry-After', attempt)), 10.0)
                except ValueError:
                    delay = attempt
                time.sleep(delay)
                continue
            return resp
        return resp

    def json(self, method, url, not_found=None, **kwargs):
        resp = self.request(method, url, **kwargs)
        self.check(resp, url, not_found)
        try:
            return resp.json()
        except ValueError:
            raise JiraError(f'Jira вернула не JSON ({url}) — проверьте адрес Jira')

    @staticmethod
    def check(resp, url, not_found=None):
        if resp.status_code == 200:
            return
        if resp.status_code == 401:
            raise JiraError('Jira отклонила логин/токен (401). Проверьте тип подключения, логин и токен')
        if resp.status_code == 403:
            raise JiraError('Недостаточно прав в Jira (403)' + (f': {_error_text(resp)}' if _error_text(resp) else ''))
        if resp.status_code == 404 and not_found:
            raise JiraError(not_found)
        raise JiraError(f'Jira вернула ошибку {resp.status_code}: {_error_text(resp) or url}')

    # ── операции ──
    def myself(self):
        data = self.json('GET', self.api('myself'))
        return data.get('displayName') or data.get('name') or data.get('emailAddress') or ''

    def list_structures(self):
        data = self.json('GET', self.structure_api('structure'), params={'limit': 1000},
                         not_found='Плагин Structure недоступен в этой Jira (REST API Structure не найден)')
        return [{'id': s.get('id'), 'name': s.get('name') or f"Структура {s.get('id')}"}
                for s in data.get('structures', []) if isinstance(s.get('id'), int)]

    def forest_rows(self, structure_id):
        data = self.json('POST', self.structure_api('forest/latest'), json={'structureId': structure_id},
                         not_found=f'Структура {structure_id} не найдена или плагин Structure недоступен')
        return parse_forest_formula(data.get('formula', ''))

    def issue(self, key, fields):
        """Задача по ключу; None, если её нет или нет прав на просмотр."""
        url = self.api(f'issue/{key}')
        resp = self.request('GET', url, params={'fields': ','.join(fields)})
        if resp.status_code == 404:
            return None
        self.check(resp, url)
        return resp.json()

    def search(self, jql, fields):
        """Все задачи по JQL (постранично: startAt в Server/DC, nextPageToken в Cloud) → {id: fields}.

        В fields под ключом '__key' — ключ задачи (ABC-1).
        """
        out = {}
        start_at, token = 0, None
        while True:
            body = {'jql': jql, 'fields': list(fields), 'maxResults': SEARCH_BATCH}
            if self.cloud:
                if token:
                    body['nextPageToken'] = token
            else:
                body['startAt'] = start_at
            data = self.json('POST', self.api('search/jql' if self.cloud else 'search'), json=body)
            issues = data.get('issues', [])
            for issue in issues:
                out[int(issue['id'])] = dict(issue.get('fields') or {}, __key=issue.get('key'))
            start_at += len(issues)
            token = data.get('nextPageToken')
            more = bool(token) if self.cloud else start_at < (data.get('total') or 0)
            if not issues or not more or len(out) >= SEARCH_LIMIT:
                return out

    def issues_by_ids(self, ids, fields):
        """{id: fields} для задач по ID (пакетами); недоступные пользователю задачи пропускаются."""
        out = {}
        ids = sorted(set(ids))
        for start in range(0, len(ids), SEARCH_BATCH):
            batch = ids[start:start + SEARCH_BATCH]
            out.update(self.search('id in (%s)' % ','.join(str(i) for i in batch), fields))
        return out

    def list_fields(self):
        """Поля Jira, пригодные для среза: учёт времени и числовые (в т. ч. пользовательские)."""
        data = self.json('GET', self.api('field'))
        out = []
        for f in data if isinstance(data, list) else []:
            fid = f.get('id') or ''
            ftype = (f.get('schema') or {}).get('type')
            if fid in TIME_FIELD_NAMES or ftype == 'number':
                out.append({'id': fid, 'name': TIME_FIELD_NAMES.get(fid) or f.get('name') or fid,
                            'custom': bool(f.get('custom')), 'time': fid in TIME_FIELD_NAMES})
        out.sort(key=lambda f: (not f['time'], f['name'].lower()))
        return out


def _error_text(resp):
    try:
        data = resp.json()
    except ValueError:
        return (resp.text or '')[:200].strip()
    messages = list(data.get('errorMessages') or [])
    messages += [f'{k}: {v}' for k, v in (data.get('errors') or {}).items()]
    if not messages and data.get('message'):
        messages.append(str(data['message']))
    return '; '.join(messages)[:300]


# ─── Расчёт среза ────────────────────────────────────────────────

def compute_snapshot(client, structure_id, mappings, field_map=None):
    """Срез для кнопки.

    mappings: [{'serviceId', 'issueKeys': 'ABC-1, ABC-2', 'includeSubtree': bool}]
    field_map: {metric: {'field', 'unit'}} (по умолчанию — учёт времени Jira)
    → [{'serviceId', 'remaining', 'spent', 'estimate', 'issueCount', 'warnings': [...]}]
    """
    field_map = field_map or DEFAULT_FIELD_MAP
    fields = sorted({v['field'] for v in field_map.values() if v['field']} | set(ISSUE_FIELDS))
    rows = None  # дерево структуры загружается один раз и только если нужно
    containers = set()  # задачи, у которых в структуре есть вложенные строки
    results = []
    for m in mappings:
        keys, invalid = parse_issue_keys(m.get('issueKeys'))
        warnings = [f'Некорректный ключ задачи: {k}' for k in invalid]
        subtree = bool(m.get('includeSubtree'))
        per_issue = {}  # issueId → поля, каждая задача учитывается один раз

        roots = []
        for key in keys:
            issue = client.issue(key, fields)
            if issue is None:
                warnings.append(f'{key}: задача не найдена или нет прав на просмотр')
                continue
            roots.append((key, int(issue['id']), dict(issue.get('fields') or {}, __key=issue.get('key') or key)))
        for _key, issue_id, root_fields in roots:
            per_issue[issue_id] = root_fields

        if subtree and roots and structure_id:
            if rows is None:
                rows = client.forest_rows(structure_id)
                containers = parent_issue_ids(rows)
            need = set()
            for key, issue_id, _f in roots:
                ids = subtree_issue_ids(rows, issue_id)
                if ids is None:
                    warnings.append(f'{key}: задачи нет в структуре — учтена только она сама')
                else:
                    need |= ids
            missing = need - set(per_issue)
            if missing:
                found = client.issues_by_ids(missing, fields)
                per_issue.update(found)
                if len(found) < len(missing):
                    warnings.append(f'{len(missing) - len(found)} задач(и) структуры недоступны для просмотра — не учтены')
        elif subtree and roots:
            children = client.search('parent in (%s)' % ', '.join(k for k, _i, _f in roots), fields)
            per_issue.update({i: f for i, f in children.items() if i not in per_issue})
            parents = {(f.get('parent') or {}).get('key') for f in children.values()
                       if isinstance(f.get('parent'), dict)}
            containers |= {i for _k, i, f in roots if f['__key'] in parents}

        values = {}
        for metric in METRICS:
            spec = field_map[metric]
            if not spec['field']:
                values[metric] = None
                continue
            raw = [f.get(spec['field']) for f in per_issue.values()]
            values[metric] = sum_hours(raw, spec['unit'])
            if values[metric] is None and any(v not in (None, '') for v in raw):
                warnings.append(f"Поле {spec['field']} не числовое — метрика не заполнена")

        if not keys and not invalid:
            warnings.append('Не указаны задачи')
        issues = [issue_detail(f, field_map, issue_id in containers) for issue_id, f in per_issue.items()]
        issues.sort(key=lambda i: issue_sort_key(i['key']))
        results.append(dict(serviceId=m['serviceId'], issueCount=len(per_issue), warnings=warnings, issues=issues,
                            **values))
    return results


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


def issue_problems(issue, field_map):
    """Признаки, из-за которых сумма по эпику может вводить в заблуждение (только по сопоставленным полям)."""
    mapped = {m: bool(field_map[m]['field']) for m in METRICS}
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


def issue_volume(issue):
    """Объём задачи для burn-up: оценка, а если её нет — затрачено + остаток."""
    if issue.get('estimate') is not None:
        return issue['estimate']
    return (issue.get('spent') or 0) + (issue.get('remaining') or 0)


def connection_json(row):
    """Строка jira_connections → данные для интерфейса (без токена)."""
    if not row:
        return None
    return {'baseUrl': row['base_url'], 'instanceType': row['instance_type'], 'login': row['login'] or '',
            'acceptInvalidCerts': bool(row['accept_invalid_certs']), 'hasToken': bool(row['token_encrypted'])}

