"""Поддельный Confluence (REST API content/space/user) для тестов отчёта: страницы хранятся в памяти."""

import base64
import threading

from flask import Flask, jsonify, request
from werkzeug.serving import make_server


class FakeConfluence:
    def __init__(self, mode='server', login='', token='conf-pat', spaces=('PROJ',)):
        self.state = {'mode': mode, 'login': login, 'token': token, 'spaces': set(spaces), 'pages': {},
                      'requests': [], 'next_id': 1000, 'readonly': False}
        app = Flask('fake_confluence')
        state = self.state

        @app.before_request
        def auth():
            state['requests'].append((request.method, request.path))
            header = request.headers.get('Authorization', '')
            if state['mode'] == 'server':
                ok = header == 'Bearer ' + state['token']
            else:
                ok = header == 'Basic ' + base64.b64encode(f"{state['login']}:{state['token']}".encode()).decode()
            if not ok:
                return jsonify({'message': 'Unauthorized'}), 401

        def page_json(page):
            return {'id': page['id'], 'type': 'page', 'title': page['title'], 'space': {'key': page['space']},
                    'version': {'number': page['version']}, '_links': {'webui': f"/pages/viewpage.action?pageId={page['id']}"}}

        @app.get('/rest/api/user/current')
        def current():
            return jsonify({'displayName': 'Сервисная учётная запись', 'username': 'burndown'})

        @app.get('/rest/api/space/<key>')
        def space(key):
            if key not in state['spaces']:
                return jsonify({'message': f'No space with key : {key}'}), 404
            return jsonify({'key': key, 'name': f'Пространство {key}'})

        @app.get('/rest/api/content')
        def search():
            found = [page_json(p) for p in state['pages'].values()
                     if p['space'] == request.args.get('spaceKey') and p['title'] == request.args.get('title')]
            return jsonify({'results': found, 'size': len(found)})

        @app.get('/rest/api/content/<pid>')
        def get_page(pid):
            page = state['pages'].get(pid)
            return (jsonify(page_json(page)), 200) if page else (jsonify({'message': 'not found'}), 404)

        @app.post('/rest/api/content')
        def create():
            body = request.get_json()
            space_key = body['space']['key']
            if space_key not in state['spaces']:
                return jsonify({'message': f'No space with key : {space_key}'}), 404
            if state['readonly']:
                return jsonify({'message': 'Not permitted to create page'}), 403
            parent = (body.get('ancestors') or [{}])[0].get('id')
            if parent and parent not in state['pages']:
                return jsonify({'message': 'Parent page not found'}), 404
            if any(p['space'] == space_key and p['title'] == body['title'] for p in state['pages'].values()):
                return jsonify({'message': 'A page with this title already exists'}), 400
            state['next_id'] += 1
            page = {'id': str(state['next_id']), 'title': body['title'], 'space': space_key, 'version': 1,
                    'body': body['body']['storage']['value'], 'parent': parent}
            state['pages'][page['id']] = page
            return jsonify(page_json(page))

        @app.put('/rest/api/content/<pid>')
        def update(pid):
            page = state['pages'].get(pid)
            if not page:
                return jsonify({'message': 'not found'}), 404
            body = request.get_json()
            if body['version']['number'] != page['version'] + 1:
                return jsonify({'message': 'Version must be incremented on update'}), 409
            page.update(title=body['title'], version=page['version'] + 1, body=body['body']['storage']['value'])
            return jsonify(page_json(page))

        self.server = make_server('127.0.0.1', 0, app, threaded=True)
        self.url = f'http://127.0.0.1:{self.server.server_port}'
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def configure(self, **kw):
        self.state.update(kw)

    def stop(self):
        self.server.shutdown()
        self.thread.join()
