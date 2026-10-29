"""О программе: версия и история версий из docs/versions.md, открытие и скачивание руководств."""

import re
from pathlib import Path
from urllib.parse import unquote

import app as app_module

ROOT = Path(__file__).resolve().parents[1]


def test_parse_versions():
    text = '''# Версии

Вступление не попадает в историю.

## Версия 2.0.0 (Ноябрь 2026 г.)

Краткое описание.

### Новое

- первое;
- второе.

Абзац раздела.

## Версия 1.9.9

### Исправлено

- третье.
'''
    assert app_module.parse_versions(text) == [
        {'version': '2.0.0', 'date': 'Ноябрь 2026 г.', 'title': 'Краткое описание.',
         'sections': [{'title': 'Новое', 'paragraphs': ['Абзац раздела.'], 'items': ['первое;', 'второе.']}]},
        {'version': '1.9.9', 'date': '', 'title': '',
         'sections': [{'title': 'Исправлено', 'paragraphs': [], 'items': ['третье.']}]},
    ]


def test_version_is_one_for_app_history_and_manuals():
    """Номер версии один: верх истории версий = версия приложения = версия на обложках руководств."""
    releases = app_module.RELEASES
    assert releases and app_module.APP_VERSION == releases[0]['version']
    versions = [tuple(map(int, r['version'].split('.'))) for r in releases]
    assert versions == sorted(versions, reverse=True) and len(set(versions)) == len(versions)
    assert all(r['date'] and r['title'] and r['sections'] for r in releases)
    for guide in ('user-guide', 'admin-guide'):
        head = (ROOT / 'docs' / 'manuals' / f'{guide}.md').read_text(encoding='utf-8')[:300]
        assert re.search(r'subtitle: "Версия ([\d.]+)"', head).group(1) == app_module.APP_VERSION


def test_about_lists_manuals_by_role(admin, make_user):
    d = admin.get('/api/about').get_json()
    assert d['version'] == app_module.APP_VERSION and d['releases'][0]['version'] == d['version']
    assert [m['id'] for m in d['manuals']] == ['user-guide', 'admin-guide']
    assert d['manuals'][0]['formats'] == ['pdf', 'docx']
    user = make_user('petrova', project_id=1)
    assert [m['id'] for m in user.get('/api/about').get_json()['manuals']] == ['user-guide']


def test_manual_open_and_download(admin, make_user, client):
    user = make_user('petrova', project_id=1)
    r = user.get('/manuals/user-guide.pdf')  # открыть в браузере
    assert r.status_code == 200 and r.mimetype == 'application/pdf' and r.data[:4] == b'%PDF'
    assert 'attachment' not in r.headers.get('Content-Disposition', '')
    r = user.get('/manuals/user-guide.pdf?download=1')
    disposition = unquote(r.headers['Content-Disposition'])
    assert 'attachment' in disposition and f'руководство пользователя {app_module.APP_VERSION}.pdf' in disposition
    r = user.get('/manuals/user-guide.docx')
    assert r.status_code == 200 and 'attachment' in r.headers['Content-Disposition'] and r.data[:2] == b'PK'
    # руководство администратора — только администратору
    assert user.get('/manuals/admin-guide.pdf').status_code == 403
    assert admin.get('/manuals/admin-guide.pdf').status_code == 200
    # чужие файлы и неизвестные форматы не отдаются, без входа — на страницу входа
    assert user.get('/manuals/user-guide.md').status_code == 404
    assert user.get('/manuals/..%2Fapp.pdf').status_code == 404
    r = client.get('/manuals/user-guide.pdf')
    assert r.status_code == 302 and r.headers['Location'].endswith('/login')


def test_sidebar_shows_version(admin):
    html = admin.get('/').get_data(as_text=True)
    assert f'Burndown — версия {app_module.APP_VERSION}' in html
