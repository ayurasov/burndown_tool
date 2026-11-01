"""Производительность: кэш расчётов по версии данных, ETag/304, сжатие, общий кэш Монте-Карло, фоновый предрасчёт."""

import gzip
import json
import sqlite3

import pytest

import app as app_module
import forecast as fc


def version(flask_app, pid=1):
    with flask_app.app_context():
        return app_module.project_version(app_module.get_db(), pid)


# ─── Версия данных проекта ───────────────────────────────────────

def test_project_version_tracks_every_input(admin, flask_app):
    v0 = version(flask_app)
    assert version(flask_app) == v0 and version(flask_app, 2) != v0  # стабильна и у каждого проекта своя
    entry = admin.get('/api/projects/1/data').get_json()['entries'][-1]
    steps = [
        lambda: admin.put(f"/api/entries/{entry['id']}", json={'date': entry['date'], 'remaining': 1.5}),
        lambda: admin.put('/api/services/1', json={'name': 'Новое имя'}),
        lambda: admin.put('/api/projects/1/stage_targets', json={'stageName': 'Этап 3', 'targetDate': '2027-03-01'}),
        lambda: admin.put('/api/projects/1/settings', json={'riskBufferDays': 9}),
        lambda: admin.put('/api/admin/calendar', json={'holidays': ['2027-01-11'], 'extraWorkdays': []}),
    ]
    seen = {v0}
    for step in steps:
        assert step().status_code == 200
        v = version(flask_app)
        assert v not in seen
        seen.add(v)
    with flask_app.app_context():  # новый срез Jira — тоже новая версия (и состава задач)
        db = app_module.get_db()
        before = app_module.snapshots_version(db, 1)
        db.execute("INSERT INTO jira_issue_snapshots (service_id, snap_date, issue_key, category) "
                   "VALUES (1, '2026-10-30', 'ABC-1', 'done')")
        db.commit()
        assert app_module.project_version(db, 1) not in seen and app_module.snapshots_version(db, 1) != before


def test_cached_result_computes_once_and_returns_copies(flask_app):
    calls = []

    def compute():
        calls.append(1)
        return {'items': [1, 2]}
    first = app_module.cached_result('test', 1, 'v1', compute)
    first['items'].append(3)  # копия: изменение не портит кэш
    assert app_module.cached_result('test', 1, 'v1', compute) == {'items': [1, 2]} and len(calls) == 1
    assert app_module.cached_result('test', 1, 'v2', compute) == {'items': [1, 2]} and len(calls) == 2
    app_module._RESULT_CACHE.update({('x', i): ('v', b'') for i in range(app_module._RESULT_CACHE_LIMIT)})
    app_module.cached_result('test', 2, 'v1', compute)  # переполнение — кэш сбрасывается, а не растёт
    assert len(app_module._RESULT_CACHE) == 1


def test_insights_cached_until_data_changes(admin, flask_app, monkeypatch):
    calls = []
    original = app_module._project_insights
    monkeypatch.setattr(app_module, '_project_insights', lambda db, pid, today=None: calls.append(pid) or original(db, pid, today))
    first = admin.get('/api/projects/1/insights').get_json()
    assert admin.get('/api/projects/1/insights').get_json() == first and admin.get('/api/portfolio').status_code == 200
    assert calls.count(1) == 1
    admin.put('/api/services/1', json={'isDone': True})
    assert 1 not in {e['id'] for e in admin.get('/api/projects/1/insights').get_json()['epics']} and calls.count(1) == 2


def test_insights_shared_between_workers_through_database(admin, flask_app, monkeypatch):
    data = admin.get('/api/projects/1/insights').get_json()
    app_module._RESULT_CACHE.clear()  # другой воркер: кэш процесса пуст, а расчёт уже лежит в базе
    app_module._PAYLOAD_CACHE.clear()
    monkeypatch.setattr(app_module, '_project_insights', lambda *a, **k: pytest.fail('пересчёт вместо общего кэша'))
    assert admin.get('/api/projects/1/insights').get_json() == data
    with flask_app.app_context():
        rows = app_module.get_db().execute("SELECT kind, key FROM result_cache").fetchall()
    assert ('insights', repr((1, app_module.datetime.now().date().isoformat()))) in {tuple(r) for r in rows}


def test_shared_cache_errors_are_ignored(flask_app, monkeypatch, tmp_path):
    monkeypatch.setattr(app_module, 'DB_PATH', str(tmp_path / 'missing.db'))
    assert app_module._shared_result_get('insights', 1, 'v') is None
    app_module._shared_result_put('insights', 1, 'v', {'a': 1})
    assert app_module._mc_store_get('key') is None
    app_module._mc_store_put('key', {'lo': None})
    assert not (tmp_path / 'missing.db').exists()  # кэш не создаёт пустую базу


# ─── ETag, 304 и сжатие ──────────────────────────────────────────

@pytest.mark.parametrize('url', ['/api/projects/1/data', '/api/projects/1/insights', '/api/projects/1/jira/composition'])
def test_heavy_endpoints_answer_304_when_unchanged(admin, url):
    r = admin.get(url)
    etag = r.headers['ETag']
    assert r.status_code == 200 and etag.startswith('W/"') and r.headers['Cache-Control'] == 'private, no-cache'
    again = admin.get(url, headers={'If-None-Match': etag})
    assert again.status_code == 304 and again.data == b'' and again.headers['ETag'] == etag
    assert admin.get(url, headers={'If-None-Match': 'W/"other"'}).status_code == 200


def test_etag_changes_after_write(admin):
    etag = admin.get('/api/projects/1/data').headers['ETag']
    admin.put('/api/services/2', json={'name': 'Переименован'})
    r = admin.get('/api/projects/1/data', headers={'If-None-Match': etag})
    assert r.status_code == 200 and r.headers['ETag'] != etag
    assert any(s['name'] == 'Переименован' for s in r.get_json()['services'])


def test_gzip_for_large_responses(admin):
    plain = admin.get('/api/projects/1/data')
    packed = admin.get('/api/projects/1/data', headers={'Accept-Encoding': 'gzip, deflate'})
    assert packed.headers['Content-Encoding'] == 'gzip' and 'Accept-Encoding' in packed.headers['Vary']
    assert json.loads(gzip.decompress(packed.data)) == plain.get_json() and len(packed.data) < len(plain.data)
    again = admin.get('/api/projects/1/data', headers={'Accept-Encoding': 'gzip'})  # готовый сжатый ответ из кэша
    assert again.data == packed.data
    page = admin.get('/', headers={'Accept-Encoding': 'gzip'})  # HTML — общим обработчиком ответов
    assert page.headers['Content-Encoding'] == 'gzip' and b'Burndown' in gzip.decompress(page.data)
    small = admin.get('/api/auth/me', headers={'Accept-Encoding': 'gzip'})
    assert 'Content-Encoding' not in small.headers  # маленький ответ не сжимается


def test_generic_etag_for_other_json(admin):
    r = admin.get('/api/projects')
    assert r.headers['ETag'] and admin.get('/api/projects', headers={'If-None-Match': r.headers['ETag']}).status_code == 304
    assert admin.get('/api/projects', headers={'If-None-Match': '*'}).status_code == 304
    post = admin.post('/api/auth/logout')
    assert 'ETag' not in post.headers  # только для GET


def test_static_urls_carry_asset_version(admin, client, monkeypatch, tmp_path):
    html = admin.get('/').get_data(as_text=True)
    assert f'/static/js/app.js?v={app_module.ASSET_VERSION}' in html
    assert f'/static/css/style.css?v={app_module.ASSET_VERSION}' in client.get('/login').get_data(as_text=True)
    monkeypatch.setattr(app_module, 'APP_DIR', str(tmp_path))  # файлов нет — метка всё равно есть
    assert len(app_module._asset_version()) == 10


# ─── Общий кэш Монте-Карло ───────────────────────────────────────

def test_monte_carlo_results_shared_through_database(flask_app, monkeypatch):
    dates, rems = ['2026-09-01', '2026-09-08', '2026-09-15', '2026-09-22'], [100.0, 80.0, 70.0, 50.0]
    with flask_app.app_context():
        app_module.get_db()
        fc._MC_CACHE.clear()
        first = fc.monte_carlo(dates, rems, '2026-12-01', fc.DEFAULT_SETTINGS)
        fc._MC_CACHE.clear()  # другой процесс
        monkeypatch.setattr(fc, 'MC_RUNS', 0)  # пересчёт дал бы другой результат
        assert fc.monte_carlo(dates, rems, '2026-12-01', fc.DEFAULT_SETTINGS) == first and first['p50']
        db = sqlite3.connect(app_module.DB_PATH)
        assert db.execute('SELECT COUNT(*) FROM mc_cache').fetchone()[0] >= 1
        db.close()


def test_old_cache_rows_are_pruned(flask_app):
    with flask_app.app_context():
        db = app_module.get_db()
        db.execute("INSERT INTO mc_cache VALUES ('old', '{}', '2020-01-01 00:00:00')")
        db.execute("INSERT INTO result_cache VALUES ('insights', 'k', 'v', '{}', '2020-01-01 00:00:00')")
        db.commit()
        app_module.deliver_outbox()
        assert db.execute("SELECT COUNT(*) FROM mc_cache WHERE key = 'old'").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM result_cache WHERE key = 'k'").fetchone()[0] == 0


def test_database_uses_wal(flask_app):
    app_module.migrate_db()
    db = sqlite3.connect(app_module.DB_PATH)
    assert db.execute('PRAGMA journal_mode').fetchone()[0] == 'wal'
    db.close()


# ─── Фоновый предрасчёт ──────────────────────────────────────────

def test_writes_schedule_warmup_of_touched_project(admin, monkeypatch):
    scheduled = []
    monkeypatch.setattr(app_module, 'schedule_warmup', lambda pids: scheduled.append(set(pids)))
    admin.get('/api/projects/1/data')
    assert scheduled == []
    admin.put('/api/services/1', json={'name': 'Другое'})
    assert scheduled == [{1}]


def test_warm_projects(flask_app, monkeypatch):
    app_module._RESULT_CACHE.clear()
    assert app_module.warm_projects([1, 2, 999]) == 2
    assert {k for k in app_module._RESULT_CACHE if k[0] == 'insights'} >= {('insights', (1, app_module.datetime.now().date().isoformat()))}
    monkeypatch.setattr(app_module, 'project_insights', lambda db, pid: 1 / 0)
    assert app_module.warm_projects([1]) == 0  # ошибка проекта записывается в журнал, поток не падает


def test_schedule_warmup_starts_worker(flask_app, monkeypatch):
    started = []
    monkeypatch.setattr(app_module, '_warm_loop', lambda: started.append(1))
    monkeypatch.setattr(app_module, '_warm_thread', None)
    app_module.schedule_warmup({1})  # в тестах предрасчёт выключен
    assert not started
    monkeypatch.setitem(flask_app.config, 'TESTING', False)
    monkeypatch.setenv('BURNDOWN_WARMUP', '0')
    app_module.schedule_warmup({1})
    assert not started
    monkeypatch.setenv('BURNDOWN_WARMUP', '1')
    app_module.schedule_warmup({1})
    app_module._warm_thread.join(2)
    assert started == [1] and 1 in app_module._warm_pending
    app_module._warm_pending.clear()


def test_shared_caches_ignore_other_versions(admin, flask_app, monkeypatch):
    admin.get('/api/projects/1/insights')
    app_module._RESULT_CACHE.clear()
    app_module._PAYLOAD_CACHE.clear()
    monkeypatch.setattr(app_module, 'APP_VERSION', '99.0.0')  # обновили приложение — расчёты прежней версии не берём
    calls = []
    original = app_module._project_insights
    monkeypatch.setattr(app_module, '_project_insights', lambda *a, **k: calls.append(1) or original(*a, **k))
    admin.get('/api/projects/1/insights')
    assert calls == [1]
    assert app_module._mc_store_key('k') != app_module.hashlib.sha1(b'k').hexdigest()
    monkeypatch.setattr(app_module.fc, 'MC_ALGO', 99)
    assert app_module._mc_store_key('k') != app_module.hashlib.sha1(b'2|k').hexdigest()
