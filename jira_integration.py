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
import os
from datetime import datetime
import re
import time
from urllib.parse import urlsplit

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
    def myself(self):
        data = self.json('GET', self.api('myself'))
        return data.get('displayName') or data.get('name') or data.get('emailAddress') or ''

    def list_structures(self):
        data = self.json('GET', self.structure_api('structure'), params={'limit': 1000},
                         not_found='Плагин Structure недоступен в этой Jira (REST API Structure не найден)')
        return [{'id': s.get('id'), 'name': s.get('name') or f"Структура {s.get('id')}"}
                for s in (data.get('structures') or []) if isinstance(s, dict) and isinstance(s.get('id'), int)]

    def forest_rows(self, structure_id):
        data = self.json('POST', self.structure_api('forest/latest'), json={'structureId': structure_id},
                         not_found=f'Структура {structure_id} не найдена или плагин Structure недоступен')
        return parse_forest_formula(str(data.get('formula') or ''))

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
                            'custom': bool(f.get('custom')), 'time': fid in TIME_FIELD_NAMES})
        out.sort(key=lambda f: (not f['time'], f['name'].lower()))
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


def collect_issues(client, structure_id, mappings, fields, changelog=False, progress=None):
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
        parents = parent_issue_ids(rows)  # задачи, у которых в структуре есть вложенные
        for g in nested:
            g['need'] = set()
            for key, issue_id, _f in g['roots']:
                ids = subtree_issue_ids(rows, issue_id)
                if ids is None:
                    g['warnings'].append(f'{key}: задачи нет в структуре — учтена только она сама')
                else:
                    g['need'] |= ids
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


def _snapshot_item(group, field_map):
    """Группа задач ассоциации → строка среза: суммы показателей, задачи и предупреждения."""
    warnings = list(group['warnings'])
    values = {}
    for metric in METRICS:
        spec = field_map[metric]
        if not spec['field']:
            values[metric] = None
            continue
        raw = [f.get(spec['field']) for f in group['issues'].values()]
        values[metric] = sum_hours(raw, spec['unit'])
        if values[metric] is None and any(v not in (None, '') for v in raw):
            warnings.append(f"Поле {spec['field']} не числовое — метрика не заполнена")
    if not group['keys'] and not group['invalid']:
        warnings.append('Не указаны задачи')
    issues = [issue_detail(f, field_map, issue_id in group['containers']) for issue_id, f in group['issues'].items()]
    issues.sort(key=lambda i: issue_sort_key(i['key']))
    return dict(serviceId=group['serviceId'], issueCount=len(group['issues']), warnings=warnings, issues=issues, **values)


def compute_snapshot(client, structure_id, mappings, field_map=None):
    """Срез для кнопки.

    mappings: [{'serviceId', 'issueKeys': 'ABC-1, ABC-2', 'includeSubtree': bool}]
    field_map: {metric: {'field', 'unit'}} (по умолчанию — учёт времени Jira)
    → [{'serviceId', 'remaining', 'spent', 'estimate', 'issueCount', 'warnings': [...]}]
    """
    field_map = field_map or DEFAULT_FIELD_MAP
    fields = sorted({v['field'] for v in field_map.values() if v['field']} | set(ISSUE_FIELDS))
    groups, _histories = collect_issues(client, structure_id, mappings, fields)
    return [_snapshot_item(g, field_map) for g in groups]


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


def compute_history(client, structure_id, mappings, field_map, moments, progress=None):
    """Срезы на прошлые моменты по текущему составу задач кнопки.

    moments: {дата 'YYYY-MM-DD': datetime конца дня}. → ({дата: items как у compute_snapshot}, предупреждения).
    Состав берётся текущий: задачи, добавленные в структуру позже, учитываются с даты создания.
    """
    field_map = field_map or DEFAULT_FIELD_MAP
    field_ids = sorted({v['field'] for v in field_map.values() if v['field']})
    # текущий состав и журналы изменений — одними и теми же запросами (задачи не загружаются дважды)
    groups, histories = collect_issues(client, structure_id, mappings,
                                       sorted(set(field_ids) | set(ISSUE_FIELDS) | {'created'}), True, progress)
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
