"""Поддельные почтовый сервер (подмена smtplib) и Telegram Bot API для тестов уведомлений."""

import smtplib
import threading

from flask import Flask, jsonify, request
from werkzeug.serving import make_server


class FakeSMTP:
    """Подмена smtplib.SMTP / SMTP_SSL: письма складываются в FakeSMTP.sent."""
    sent = []
    connections = []
    fail = None  # 'auth' | 'connect' — смоделировать ошибку

    def __init__(self, host, port, timeout=None, context=None):
        if FakeSMTP.fail == 'connect':
            raise OSError('Connection refused')
        self.host, self.port, self.tls, self.user = host, port, False, None
        FakeSMTP.connections.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self, context=None):
        self.tls = True

    def login(self, user, password):
        if FakeSMTP.fail == 'auth':
            raise smtplib.SMTPAuthenticationError(535, b'Authentication failed')
        self.user = user

    def send_message(self, msg):
        FakeSMTP.sent.append(msg)

    @classmethod
    def reset(cls):
        cls.sent, cls.connections, cls.fail = [], [], None


class FakeTelegram:
    """Bot API: getMe, getUpdates, sendMessage, sendDocument. updates — что «написали» боту пользователи."""

    def __init__(self, token='123:ABC', username='burndown_test_bot'):
        self.state = {'token': token, 'username': username, 'updates': [], 'messages': [], 'documents': []}
        app = Flask('fake_telegram')

        @app.post('/bot<token>/<method>')
        def call(token, method):
            if token != self.state['token']:
                return jsonify({'ok': False, 'error_code': 401, 'description': 'Unauthorized'}), 401
            if method == 'getMe':
                return jsonify({'ok': True, 'result': {'id': 1, 'is_bot': True, 'username': self.state['username']}})
            if method == 'getUpdates':
                return jsonify({'ok': True, 'result': self.state['updates']})
            if method == 'sendMessage':
                if request.form.get('chat_id') == 'blocked':
                    return jsonify({'ok': False, 'error_code': 403, 'description': 'Forbidden: bot was blocked by the user'}), 403
                self.state['messages'].append(dict(request.form))
                return jsonify({'ok': True, 'result': {'message_id': len(self.state['messages'])}})
            if method == 'sendDocument':
                f = request.files['document']
                self.state['documents'].append({'chat_id': request.form.get('chat_id'), 'name': f.filename,
                                                'size': len(f.read()), 'caption': request.form.get('caption')})
                return jsonify({'ok': True, 'result': {}})
            return jsonify({'ok': False, 'description': 'Not Found'}), 404

        self.server = make_server('127.0.0.1', 0, app, threaded=True)
        self.url = f'http://127.0.0.1:{self.server.server_port}'
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def user_starts(self, code, chat_id=555):
        """Пользователь открыл ссылку привязки и нажал «Start»."""
        self.state['updates'].append({'update_id': len(self.state['updates']) + 1,
                                      'message': {'text': f'/start {code}', 'chat': {'id': chat_id}}})

    def stop(self):
        self.server.shutdown()
        self.thread.join()
