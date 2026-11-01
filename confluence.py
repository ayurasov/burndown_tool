"""Отчёт Burndown в Confluence (Server/Data Center и Cloud): клиент REST API и страница отчёта в формате хранения
Confluence (XHTML с макросами статуса и панелей). Без Flask — данные отчёта готовит приложение."""

import html
import os
import re
from urllib.parse import urlsplit

try:
    import requests
except ImportError:  # pragma: no cover — без библиотеки недоступна только публикация
    requests = None

INSTANCE_TYPES = ('cloud', 'server', 'server_basic')
SPACE_KEY_RE = re.compile(r'^[A-Za-z0-9~_]{1,255}$')
STATUS_MACRO = {'ok': ('Green', 'Успевает'), 'risk': ('Yellow', 'Риск'), 'danger': ('Red', 'Не успевает'),
                'notarget': ('Grey', 'Нет цели'), 'unknown': ('Grey', 'Нет прогноза')}
PANEL = {'ok': 'tip', 'risk': 'note', 'danger': 'warning'}
SEVERITY_MARK = {'danger': '⛔', 'risk': '⚠️', 'info': 'ℹ️', 'ok': '✅'}


class ConfluenceError(Exception):
    pass


def check_base_url(base_url):
    """Адрес Confluence: http(s)://хост[:порт][/путь] (для Cloud — с /wiki), без логина, параметров и якоря.
    CONFLUENCE_ALLOWED_HOSTS (через запятую) ограничивает хосты так же, как JIRA_ALLOWED_HOSTS для Jira."""
    url = (base_url or '').strip().rstrip('/')
    if not re.match(r'^https?://', url, re.IGNORECASE):
        raise ConfluenceError('Адрес Confluence должен начинаться с http:// или https://')
    try:
        parts = urlsplit(url)
        parts.port  # noqa: B018 — ValueError, если порт некорректный
    except ValueError:
        raise ConfluenceError('Некорректный адрес Confluence')
    if not parts.hostname or '@' in parts.netloc or parts.query or parts.fragment or '?' in url or '#' in url \
            or re.search(r'\s', url):
        raise ConfluenceError('Адрес Confluence — только https://хост[:порт][/путь], без логина, параметров «?» и «#»')
    allowed = [h.strip().lower() for h in os.environ.get('CONFLUENCE_ALLOWED_HOSTS', '').split(',') if h.strip()]
    host = parts.hostname.lower()
    if allowed and not any(host == h or (h.startswith('.') and (host.endswith(h) or host == h[1:])) for h in allowed):
        raise ConfluenceError(f'Подключение к {host} запрещено настройками сервера (CONFLUENCE_ALLOWED_HOSTS)')
    return url


class ConfluenceClient:
    def __init__(self, base_url, instance_type, login, token, accept_invalid_certs=False, timeout=(15, 60)):
        if instance_type not in INSTANCE_TYPES:
            raise ConfluenceError('Неизвестный тип подключения к Confluence')
        if requests is None:
            raise ConfluenceError('На сервере не установлена библиотека requests')
        self.base_url = check_base_url(base_url)
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({'Accept': 'application/json'})
        if instance_type == 'server':
            if not token.isascii():
                raise ConfluenceError('Токен содержит недопустимые символы — скопируйте Personal Access Token заново')
            self.session.headers['Authorization'] = 'Bearer ' + token
        else:
            self.session.auth = (login.encode('utf-8'), token.encode('utf-8'))
        self.session.verify = not accept_invalid_certs

    def request(self, method, path, not_found=None, **kwargs):
        url = f'{self.base_url}/rest/api/{path.lstrip("/")}'
        try:
            # без перенаправлений: учётные данные уходят только на адрес из подключения
            resp = self.session.request(method, url, timeout=self.timeout, allow_redirects=False, **kwargs)
        except requests.exceptions.SSLError:
            raise ConfluenceError('Ошибка TLS-сертификата Confluence: если сертификат корпоративный, включите '
                                  '«Не проверять TLS-сертификат»')
        except requests.exceptions.Timeout:
            raise ConfluenceError('Confluence не ответил вовремя — попробуйте ещё раз')
        except requests.exceptions.ConnectionError:
            raise ConfluenceError(f'Не удалось подключиться к {self.base_url}: проверьте адрес и доступность сети')
        if resp.is_redirect:
            raise ConfluenceError('Confluence перенаправляет запрос — укажите итоговый адрес (для Cloud — с /wiki)')
        if resp.status_code == 401:
            raise ConfluenceError('Confluence отклонил логин или токен (401)')
        if resp.status_code == 403:
            raise ConfluenceError('Недостаточно прав в Confluence (403)' + _error_text(resp, ': '))
        if resp.status_code == 404 and not_found:
            raise ConfluenceError(not_found)
        if resp.status_code not in (200, 201):
            raise ConfluenceError(f'Confluence вернул ошибку {resp.status_code}' + _error_text(resp, ': '))
        try:
            data = resp.json()
        except ValueError:
            data = None
        if not isinstance(data, dict):
            raise ConfluenceError('Confluence вернул неожиданный ответ — проверьте адрес (для Cloud — с /wiki); '
                                  'возможно, отвечает страница входа или прокси')
        return data

    def current_user(self):
        data = self.request('GET', 'user/current')
        return data.get('displayName') or data.get('username') or data.get('publicName') or ''

    def space(self, key):
        data = self.request('GET', f'space/{key}', not_found=f'Пространство {key} не найдено или нет прав на него')
        return {'key': data.get('key') or key, 'name': data.get('name') or key}

    def get_page(self, page_id):
        try:
            return self.request('GET', f'content/{page_id}', not_found='-', params={'expand': 'version'})
        except ConfluenceError as e:
            if str(e) == '-':
                return None
            raise

    def find_page(self, space, title):
        data = self.request('GET', 'content', params={'spaceKey': space, 'title': title, 'type': 'page',
                                                       'expand': 'version'})
        results = data.get('results') or []
        return results[0] if results and isinstance(results[0], dict) else None

    def page_url(self, page):
        webui = ((page or {}).get('_links') or {}).get('webui') or ''
        return f'{self.base_url}{webui}' if webui.startswith('/') else self.base_url

    def publish(self, space, title, body, parent_id=None, page_id=None):
        """Создать или обновить страницу → {'id', 'url', 'created'}. Страница ищется по id (если известен),
        затем по названию в пространстве."""
        page = self.get_page(page_id) if page_id else None
        if page and (page.get('space') or {}).get('key') not in (None, space):
            page = None  # пространство сменили — новая страница
        page = page or self.find_page(space, title)
        storage = {'storage': {'value': body, 'representation': 'storage'}}
        if page:
            version = int(((page.get('version') or {}).get('number')) or 1) + 1
            data = self.request('PUT', f"content/{page['id']}", json={
                'id': page['id'], 'type': 'page', 'title': title, 'space': {'key': space},
                'version': {'number': version, 'message': 'Burndown'}, 'body': storage})
            return {'id': str(data.get('id') or page['id']), 'url': self.page_url(data), 'created': False}
        payload = {'type': 'page', 'title': title, 'space': {'key': space}, 'body': storage}
        if parent_id:
            payload['ancestors'] = [{'id': str(parent_id)}]
        data = self.request('POST', 'content', json=payload,
                            not_found='Родительская страница или пространство не найдены')
        return {'id': str(data.get('id')), 'url': self.page_url(data), 'created': True}


def _error_text(resp, prefix=''):
    try:
        data = resp.json()
    except ValueError:
        return ''
    if not isinstance(data, dict):
        return ''
    message = data.get('message') or ((data.get('data') or {}).get('errors') or [{}])[0].get('message', {})
    if isinstance(message, dict):
        message = message.get('translation') or message.get('key') or ''
    return f'{prefix}{str(message)[:300]}' if message else ''


# ─── Страница отчёта ─────────────────────────────────────────────

def esc(text):
    return html.escape(str(text if text is not None else ''), quote=True)


def status_macro(status):
    colour, title = STATUS_MACRO.get(status, STATUS_MACRO['unknown'])
    return ('<ac:structured-macro ac:name="status"><ac:parameter ac:name="colour">' + colour +
            '</ac:parameter><ac:parameter ac:name="title">' + esc(title) + '</ac:parameter></ac:structured-macro>')


def _findings(items):
    if not items:
        return ''
    return '<ul>' + ''.join(
        f"<li>{SEVERITY_MARK.get(f['severity'], '')} " + (f"<strong>{esc(f['subject'])}:</strong> " if f.get('subject') else '')
        + f"{esc(f['text'])}</li>" for f in items) + '</ul>'


def _table(head, rows):
    return ('<table><tbody><tr>' + ''.join(f'<th>{esc(h)}</th>' for h in head) + '</tr>' +
            ''.join('<tr>' + ''.join(f'<td>{c}</td>' for c in row) + '</tr>' for row in rows) + '</tbody></table>')


def report_storage(report):
    """Отчёт проекта в формате хранения Confluence. report: project, date, status, headline, facts, top, health,
    week {from, to, items}, stages и epics [{name, status, remaining, forecast, target, chance, headline}], url."""
    panel = PANEL.get(report['status'], 'info')
    out = [f'<p>Отчёт Burndown на {esc(report["date"])}. Страница обновляется автоматически — правки на ней '
           'будут заменены при следующей публикации.</p>',
           f'<ac:structured-macro ac:name="{panel}"><ac:rich-text-body><p><strong>{esc(report["headline"])}</strong></p>'
           '</ac:rich-text-body></ac:structured-macro>']
    if report.get('facts'):
        out.append(_table(['Показатель', 'Значение'], [(esc(f['label']), esc(f['value'])) for f in report['facts']]))
    out.append('<h2>Главные риски</h2>' + (_findings(report.get('top')) or '<p>Рисков по срокам не видно.</p>'))
    week = report.get('week')
    if week:
        out.append(f'<h2>Что изменилось за неделю ({esc(week["from"])} — {esc(week["to"])})</h2>' +
                   (_findings(week['items']) or '<p>Существенных изменений в статусах и прогнозах нет.</p>'))
    head = ['', 'Статус', 'Остаток', 'Прогноз', 'Цель', 'Шанс успеть', 'Главное']

    def rows(items):
        return [(f'<strong>{esc(x["name"])}</strong>', status_macro(x['status']), esc(x['remaining']), esc(x['forecast']),
                 esc(x['target']), esc(x['chance']), esc(x.get('headline') or '')) for x in items]
    if report.get('stages'):
        out.append('<h2>Этапы</h2>' + _table(['Этап'] + head[1:], rows(report['stages'])))
    if report.get('epics'):
        out.append('<h2>Эпики</h2>' + _table(['Эпик'] + head[1:], rows(report['epics'])))
    if report.get('health'):
        out.append('<h2>Данные</h2>' + _findings(report['health']))
    if report.get('url'):
        out.append(f'<p><a href="{esc(report["url"])}">Открыть проект в Burndown</a></p>')
    return ''.join(out)
