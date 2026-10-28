"""Доставка уведомлений: электронная почта (SMTP) и Telegram-бот. Без Flask — только транспорт."""

import html
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr

try:
    import requests
except ImportError:  # pragma: no cover — без библиотеки недоступен только Telegram
    requests = None

SMTP_SECURITY = ('none', 'starttls', 'ssl')
TELEGRAM_LIMIT = 4000  # сообщение Telegram — до 4096 символов


class NotifyError(Exception):
    """Ошибка отправки с понятным пользователю текстом."""


# ─── Почта ───────────────────────────────────────────────────────

def send_email(cfg, to, subject, text, html_body=None, attachments=(), timeout=20):
    """cfg: host, port, security (none|starttls|ssl), login, password, sender. attachments: [(имя, байты, mime)]."""
    if not cfg.get('host') or not cfg.get('sender'):
        raise NotifyError('Почта не настроена: укажите SMTP-сервер и адрес отправителя')
    if not to:
        raise NotifyError('У пользователя не указан email')
    msg = EmailMessage()
    msg['Subject'] = subject
    msg['From'] = formataddr(('Burndown', cfg['sender']))
    msg['To'] = to
    msg.set_content(text)
    if html_body:
        msg.add_alternative(html_body, subtype='html')
    for name, data, mime in attachments:
        maintype, subtype = mime.split('/', 1)
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=name)
    security = cfg.get('security') or 'starttls'
    port = int(cfg.get('port') or (465 if security == 'ssl' else 587))
    try:
        if security == 'ssl':
            server = smtplib.SMTP_SSL(cfg['host'], port, timeout=timeout, context=ssl.create_default_context())
        else:
            server = smtplib.SMTP(cfg['host'], port, timeout=timeout)
        with server:
            if security == 'starttls':
                server.starttls(context=ssl.create_default_context())
            if cfg.get('login'):
                server.login(cfg['login'], cfg.get('password') or '')
            server.send_message(msg)
    except smtplib.SMTPAuthenticationError:
        raise NotifyError('SMTP-сервер отклонил логин или пароль')
    except smtplib.SMTPRecipientsRefused:
        raise NotifyError(f'SMTP-сервер отклонил адрес получателя {to}')
    except (smtplib.SMTPException, OSError) as e:
        raise NotifyError(f'Не удалось отправить письмо через {cfg["host"]}:{port}: {e}')


# ─── Telegram ────────────────────────────────────────────────────

class Telegram:
    def __init__(self, token, api_base='https://api.telegram.org', timeout=20):
        if not token:
            raise NotifyError('Telegram-бот не настроен')
        if requests is None:
            raise NotifyError('На сервере не установлена библиотека requests')
        self.url = f'{api_base.rstrip("/")}/bot{token}/'
        self.timeout = timeout

    def call(self, method, data=None, files=None):
        try:
            resp = requests.post(self.url + method, data=data, files=files, timeout=self.timeout)
        except requests.RequestException as e:
            raise NotifyError(f'Telegram недоступен: {e.__class__.__name__}')
        try:
            body = resp.json()
        except ValueError:
            raise NotifyError(f'Telegram вернул не JSON (HTTP {resp.status_code})')
        if not body.get('ok'):
            if resp.status_code == 401:
                raise NotifyError('Telegram отклонил токен бота')
            raise NotifyError('Telegram: ' + str(body.get('description') or f'ошибка {resp.status_code}'))
        return body.get('result')

    def bot_name(self):
        return (self.call('getMe') or {}).get('username') or ''

    def send_message(self, chat_id, text_html):
        if len(text_html) > TELEGRAM_LIMIT:
            text_html = text_html[:TELEGRAM_LIMIT].rsplit('\n', 1)[0] + '\n…'
        self.call('sendMessage', {'chat_id': chat_id, 'text': text_html, 'parse_mode': 'HTML',
                                  'disable_web_page_preview': 'true'})

    def send_document(self, chat_id, filename, data, caption=''):
        self.call('sendDocument', {'chat_id': chat_id, 'caption': caption[:1000]},
                  files={'document': (filename, data, 'application/pdf')})

    def find_start(self, code):
        """chat_id из сообщения «/start <code>», которое пользователь отправил боту по ссылке привязки."""
        for update in reversed(self.call('getUpdates', {'allowed_updates': '["message"]'}) or []):
            message = update.get('message') or {}
            if (message.get('text') or '').strip() == f'/start {code}':
                return str((message.get('chat') or {}).get('id'))
        return None


def esc(text):
    return html.escape(str(text if text is not None else ''), quote=False)
