"""Лента изменений выводов: снимки на дату, сравнение по неделям, API, дайджест."""

import json
from datetime import date, timedelta

import app as app_module
import insights as ins

SETTINGS = dict(app_module.fc.DEFAULT_SETTINGS)


def item(name='Эпик', status='ok', forecast='2026-12-01', target='2026-12-10', probability=0.8, remaining=100.0,
         no_burn=False, too_far=False, finished=False):
    return {'name': name, 'status': status, 'forecast': forecast, 'target': target, 'probability': probability,
            'remaining': remaining, 'noBurn': no_burn, 'tooFar': too_far, 'finished': finished}


def state(epics=None, stages=None, status='ok', remaining=100.0, day='2026-11-01'):
    return {'date': day, 'status': status, 'remaining': remaining, 'epics': epics or {}, 'stages': stages or {}}


def kinds(found):
    return [(f['kind'], f['severity']) for f in found]


# ─── Сравнение снимков ───────────────────────────────────────────

def test_status_shift_and_chance_changes():
    old = state({'1': item()})
    new = state({'1': item(status='danger', forecast='2026-12-20', probability=0.3)})
    found = ins.state_changes(old, new, SETTINGS)
    assert kinds(found) == [('status', 'danger'), ('chance', 'risk'), ('shift', 'risk')]  # важность, затем вес
    assert found[0]['text'] == 'Статус: успевает → не успевает.' and found[0]['subject'] == 'Эпик'
    assert found[0]['ref'] == {'level': 'epic', 'id': 1}
    assert found[2]['text'] == 'Прогноз позже на 19\xa0дней: 01.12.2026 → 20.12.2026.'
    assert found[1]['text'] == 'Шанс успеть к цели: 80 % → 30 %.'
    better = ins.state_changes(new, old, SETTINGS)
    assert kinds(better) == [('status', 'ok'), ('chance', 'ok'), ('shift', 'ok')]


def test_small_changes_are_not_news():
    old = state({'1': item()})
    new = state({'1': item(forecast='2026-12-03', probability=0.7)})  # 2 дня и 10 п. п. — колебания
    assert ins.state_changes(old, new, SETTINGS) == []
    later_but_on_time = ins.state_changes(old, state({'1': item(forecast='2026-12-06')}), SETTINGS)
    assert kinds(later_but_on_time) == [('shift', 'info')]  # позже, но к цели успевает
    no_target = item(status='notarget', target=None)
    assert ins.state_changes(state({'1': no_target}), state({'1': dict(no_target, status='unknown')}), SETTINGS) == []
    assert ins.state_changes(None, new, SETTINGS) == [] and ins.state_changes(old, None, SETTINGS) == []


def test_finished_stalled_resumed_target():
    old = state({'1': item(), '2': item(name='Второй'), '3': item(name='Третий', no_burn=True, forecast=None)})
    new = state({'1': item(finished=True, remaining=0), '2': item(name='Второй', no_burn=True, forecast=None),
                 '3': item(name='Третий', forecast='2027-01-15', target='2027-02-01')})
    found = {f['subject']: f for f in ins.state_changes(old, new, SETTINGS)}
    assert found['Эпик']['kind'] == 'finished' and found['Эпик']['text'] == 'Эпик завершён — остаток обнулился.'
    assert found['Второй']['text'] == 'Прогноз потерян: остаток не сгорает (был 01.12.2026).'
    too_far = ins.state_changes(old, state({'2': item(name='Второй', too_far=True, forecast=None)}), SETTINGS)
    assert 'срок больше 3 лет' in too_far[0]['text']
    never = ins.state_changes(state({'2': item(forecast=None, status='unknown')}),
                              state({'2': item(no_burn=True, forecast=None)}), SETTINGS)
    assert never[0]['text'] == 'Прогноза нет: остаток не сгорает.'  # прогноза не было — «потерян» не пишем
    third = [f for f in ins.state_changes(old, new, SETTINGS) if f['subject'] == 'Третий']
    assert kinds(third) == [('target', 'info'), ('resumed', 'ok')]
    assert third[0]['text'] == 'Цель изменена: 10.12.2026 → 01.02.2027.'


def test_project_stage_added_and_removed():
    old = state({'1': item(), '2': item(name='Ушедший')}, {'Этап 1': item(name='Этап 1')}, status='ok', remaining=300)
    new = state({'1': item(), '5': item(name='Новый')}, {'Этап 1': item(name='Этап 1', status='risk')}, status='risk',
                remaining=250.5)
    found = ins.state_changes(old, new, SETTINGS)
    texts = [(f['subject'], f['text']) for f in found]
    assert texts[0] == (None, 'Проект: успевает → в зоне риска.')
    assert ('Этап 1', 'Статус: успевает → в зоне риска.') in texts
    assert ('Ушедший', 'Больше не в работе: отмечен сданным или удалён.') in texts
    assert ('Новый', 'Новый эпик в работе.') in texts
    assert (None, 'Остаток проекта уменьшился на 49,5\xa0ч: 300\xa0ч → 250,5\xa0ч.') in texts
    stage_ref = next(f['ref'] for f in found if f['subject'] == 'Этап 1')
    assert stage_ref == {'level': 'stage', 'name': 'Этап 1'}
    assert next(f for f in found if f['subject'] == 'Ушедший')['ref'] is None


def test_insight_state_from_insights(flask_app):
    with flask_app.app_context():
        data = app_module.project_insights(app_module.get_db(), 1)
    st = ins.insight_state(data)
    assert st['date'] == data['date'] and st['status'] == data['summary']['status']
    assert set(st['epics']) == {str(e['id']) for e in data['epics']} and set(st['stages']) == {s['name'] for s in data['stages']}
    assert st['remaining'] == app_module.fc.js_sum(e['remaining'] for e in data['epics'] if e['remaining'] is not None)
    assert json.loads(json.dumps(st)) == st


# ─── Лента по неделям ────────────────────────────────────────────

def recent_measurements(flask_app, values, target_in=60):
    """Замеры эпика 1 раз в неделю до сегодняшнего дня (последний — вчера) и цель через target_in дней."""
    today = date.today()
    with flask_app.app_context():
        db = app_module.get_db()
        db.execute('DELETE FROM entries WHERE service_id = 1')
        days = [today - timedelta(days=7 * (len(values) - 1 - i) + 1) for i in range(len(values))]
        for d, v in zip(days, values):
            db.execute('INSERT INTO entries (service_id, date, remaining, spent, estimate) VALUES (1, ?, ?, 0, 500)',
                       (d.isoformat(), v))
        db.execute('UPDATE services SET target_date = ? WHERE id = 1', ((today + timedelta(days=target_in)).isoformat(),))
        db.commit()
    app_module._RESULT_CACHE.clear()


def test_weekly_feed_api(admin, flask_app, make_user):
    recent_measurements(flask_app, [500, 450, 400, 350, 300, 300, 300])  # шесть недель сгорания, затем стоп
    r = admin.get('/api/projects/1/insights/changes')
    assert r.status_code == 200
    weeks = r.get_json()['weeks']
    today = date.today().isoformat()
    assert weeks[0]['to'] == today and weeks[0]['from'] == (date.today() - timedelta(days=7)).isoformat()
    assert all(w['from'] == n['to'] for w, n in zip(weeks, weeks[1:]))  # недели идут подряд
    epic_name = 'Сервис "Обращения клиентов"'
    latest = [f for f in weeks[0]['items'] if f['subject'] == epic_name]
    assert latest and latest[0]['severity'] != 'ok'  # работа встала — прогноз хуже
    assert len(weeks) <= app_module.CHANGES_WEEKS
    assert admin.get('/api/projects/1/insights/changes', headers={'If-None-Match': r.headers['ETag']}).status_code == 304
    assert len(admin.get('/api/projects/1/insights/changes?weeks=2').get_json()['weeks']) == 2
    assert admin.get('/api/projects/1/insights/changes?weeks=x').status_code == 200
    with flask_app.app_context():
        rows = {tuple(r) for r in app_module.get_db().execute('SELECT snap_date, source FROM insight_snapshots WHERE project_id = 1')}
    assert (today, 'live') in rows and ((date.today() - timedelta(days=7)).isoformat(), 'backfill') in rows
    assert admin.get('/api/projects/999/insights/changes').status_code == 404
    assert make_user('viewer', project_id=2).get('/api/projects/1/insights/changes').status_code == 403


def test_feed_stops_before_first_measurements(admin, flask_app):
    recent_measurements(flask_app, [500, 400])  # данные только за неделю
    with flask_app.app_context():
        db = app_module.get_db()
        db.execute('DELETE FROM entries WHERE service_id != 1')
        db.commit()
    app_module._RESULT_CACHE.clear()
    weeks = admin.get('/api/projects/1/insights/changes').get_json()['weeks']
    assert len(weeks) == 1  # только неделя с замерами в начале; раньше замеров нет — сравнивать не с чем
    empty = admin.get('/api/projects/2/insights/changes').get_json()['weeks']
    assert len(empty) == 1 and empty[0]['items'] == []


def test_stored_snapshot_is_used_and_broken_one_rebuilt(admin, flask_app):
    recent_measurements(flask_app, [500, 450, 400, 350])
    week_ago = (date.today() - timedelta(days=8)).isoformat()  # снимок на день раньше границы — годится
    with flask_app.app_context():
        current = ins.insight_state(app_module.project_insights(app_module.get_db(), 1))
    old = json.loads(json.dumps(current))
    old['epics']['999'] = dict(old['epics']['1'], name='Давний эпик')
    app_module.store_insight_snapshot(1, week_ago, old, 'live')
    items = admin.get('/api/projects/1/insights/changes?weeks=1').get_json()['weeks'][0]['items']
    assert ('Давний эпик', 'removed') in {(f['subject'], f['kind']) for f in items}
    with flask_app.app_context():
        db = app_module.get_db()
        db.execute("UPDATE insight_snapshots SET state = 'не json' WHERE snap_date = ?", (week_ago,))
        db.commit()
        state_on = app_module.insight_state_on(db, 1, (date.today() - timedelta(days=7)).isoformat())
    assert '999' not in state_on['epics']  # испорченный снимок пересчитан по замерам


def test_daily_snapshots_and_digest(admin, flask_app):
    recent_measurements(flask_app, [500, 450, 400, 350, 300, 300, 300])
    with flask_app.app_context():
        db = app_module.get_db()
        assert app_module.record_daily_insights() == 2
        assert app_module.record_daily_insights() == 0  # снимок на сегодня уже есть
        ov = app_module.digest_overview(db, 1)
    assert ov['changes'] and len(ov['changes']) <= 6
    msg = app_module.render_digest(ov, None, 'https://burndown.example')
    subject, text, html_body, tg = msg if isinstance(msg, tuple) else (msg['subject'], msg['text'], msg['html'], msg['telegram'])
    assert 'За неделю:' in text and 'За неделю:' in html_body and 'За неделю:' in tg
    assert ov['changes'][0]['text'] in text


def test_daily_snapshot_errors_do_not_stop_others(flask_app, monkeypatch):
    calls = []

    def boom(db, pid, today=None):
        calls.append(pid)
        raise RuntimeError('сбой')
    monkeypatch.setattr(app_module, 'project_insights', boom)
    with flask_app.app_context():
        assert app_module.record_daily_insights() == 0
    assert calls == [1, 2]


def test_snapshot_store_ignores_locked_database(flask_app, monkeypatch, tmp_path):
    monkeypatch.setattr(app_module, 'DB_PATH', str(tmp_path / 'нет.db'))
    app_module.store_insight_snapshot(1, '2026-11-01', {'x': 1}, 'live')  # без ошибки
    assert not (tmp_path / 'нет.db').exists()
