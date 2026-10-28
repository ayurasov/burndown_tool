"""Общие фикстуры: временная БД с демо-данными init_db, тестовый клиент Flask, живой сервер для e2e."""

import os
import sys
import threading

import pytest

os.environ.setdefault('BURNDOWN_SCHEDULER', '0')  # фоновый планировщик в тестах не нужен
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as app_module  # noqa: E402
import init_db as init_db_module  # noqa: E402

ADMIN = ('admin', 'Test198!')


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    path = str(tmp_path / 'test.db')
    monkeypatch.setattr(init_db_module, 'DB_PATH', path)
    monkeypatch.setattr(app_module, 'DB_PATH', path)
    init_db_module.init_db()
    return path


@pytest.fixture
def flask_app(db_path):
    app_module.app.config.update(TESTING=True)
    return app_module.app


def login(client, username, password):
    return client.post('/api/auth/login', json={'username': username, 'password': password})


@pytest.fixture
def client(flask_app):
    return flask_app.test_client()


@pytest.fixture
def admin(flask_app):
    c = flask_app.test_client()
    assert login(c, *ADMIN).status_code == 200
    return c


@pytest.fixture
def make_user(admin, flask_app):
    """Создаёт пользователя (опционально с доступом к проекту) и возвращает залогиненный клиент."""
    def factory(username, role='user', project_id=None, project_role='user', password='pw-12345'):
        r = admin.post('/api/users', json={'username': username, 'password': password, 'role': role})
        assert r.status_code == 200, r.get_json()
        uid = r.get_json()['id']
        if project_id:
            assert admin.post(f'/api/users/{uid}/projects',
                              json={'projectId': project_id, 'role': project_role}).status_code == 200
        c = flask_app.test_client()
        assert login(c, username, password).status_code == 200
        c.user_id = uid
        return c
    return factory


@pytest.fixture
def live_server(flask_app):
    """Реальный HTTP-сервер приложения на свободном порту (для браузерных тестов)."""
    from werkzeug.serving import make_server
    server = make_server('127.0.0.1', 0, flask_app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f'http://127.0.0.1:{server.server_port}'
    server.shutdown()
    thread.join()
