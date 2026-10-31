"""Серверный расчёт прогнозов (forecast.py) совпадает с расчётом интерфейса (static/js/app.js)."""

import json
import os
import random
import shutil
import subprocess

import pytest

import forecast

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def js_results(cases, settings):
    out = subprocess.run(['node', os.path.join(ROOT, 'tests', 'js_forecast.js'), os.path.join(ROOT, 'static', 'js', 'app.js'),
                          json.dumps(settings)], input=json.dumps(cases), capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


def py_results(cases, settings):
    out = []
    for dates, rems, target in cases:
        s = forecast.compute_status(dates, rems, target, settings)
        mc = s['mc']
        out.append([s['status'], s['forecastZero'], s['noBurn'], s['tooFar'], s['finished'],
                    [mc['lo'], mc['p50'], mc['hi'], mc['probability'], mc['confidence']] if mc else None, s['lastDate']])
    return out


def random_cases(seed, n=60):
    rng = random.Random(seed)
    cases = []
    for _ in range(n):
        day = forecast.add_days('2026-07-01', rng.randint(0, 30))
        dates, rems, rem = [], [], rng.uniform(50, 800)
        flat = rng.random() < 0.15  # ровный дробный остаток (заблокированный эпик)
        for _ in range(rng.randint(1, 9)):
            day = forecast.add_days(day, rng.randint(1, 9))
            rem = rem if flat else max(0.0, rem - rng.uniform(-40, 90))
            dates.append(day)
            rems.append(None if rng.random() < 0.08 else round(rem, rng.choice([0, 2, 5])))
        target = None if rng.random() < 0.15 else forecast.add_days(day, rng.randint(-20, 90))
        cases.append([dates, rems, target])
    return cases


HOLIDAYS = ['2026-07-03', '2026-07-06', '2026-08-14', '2026-09-01', '2026-11-04', '2026-12-31', '2027-01-01']
SETTINGS = [
    {'forecastMethod': 'linear', 'riskBufferDays': 3, 'burnrateMethod': 'all', 'burnrateWindowDays': 14},
    {'forecastMethod': 'average', 'riskBufferDays': 0, 'burnrateMethod': 'rolling', 'burnrateWindowDays': 10},
    {'forecastMethod': 'linear', 'riskBufferDays': 2, 'burnrateMethod': 'all', 'burnrateWindowDays': 14,
     'rangeConfidence': 90, 'workdays': True, 'holidays': HOLIDAYS, 'extraWorkdays': ['2026-07-11', '2026-11-07']},
    {'forecastMethod': 'average', 'riskBufferDays': 1, 'burnrateMethod': 'rolling', 'burnrateWindowDays': 21,
     'rangeConfidence': 50, 'workdays': True, 'holidays': [], 'extraWorkdays': []},
]


@pytest.mark.skipif(not shutil.which('node'), reason='нужен Node.js')
@pytest.mark.parametrize('settings', SETTINGS, ids=['linear-all', 'average-rolling', 'workdays-90', 'workdays-average-50'])
def test_server_forecast_matches_interface(settings):
    cases = random_cases(SETTINGS.index(settings) * 7919 + 13)
    cases.append([['2026-07-17', '2026-07-24', '2026-07-28', '2026-07-29'], [509, 124, 349, 346], '2026-08-31'])
    cases.append([['2026-07-01', '2026-07-08', '2026-07-15'], [10, 5, 0], '2026-07-10'])
    cases += [[EDGE_DATES[:len(r)], r, t] for r, t in EDGE_CASES]
    assert py_results(cases, settings) == js_results(cases, settings)


EDGE_DATES = ['2026-09-01', '2026-09-08', '2026-09-15', '2026-09-22', '2026-09-29']
EDGE_CASES = [
    ([120.7, 120.7, 120.7], '2026-10-01'),        # ровный дробный остаток: наклон — шум округления
    ([10000, 10000, 9999.99], '2026-12-31'),      # почти ровный: ноль дальше трёх лет
    ([100, 50, 0, 0, 0], '2026-09-19'),           # завершён раньше цели, дальше нулевые замеры
    ([100, 10, 8, 7], '2026-09-20'),              # регрессия пересекает ноль раньше последнего замера
    ([0, 0, 0], '2026-09-05'),
    ([100, 90, 120, 110, 140], '2026-12-01'),     # равные интервалы, остаток чаще растёт: досчёт сценариев пропускается
    ([300, 280, 310, 250, 290], '2027-03-01'),
]


def _js_query(payload, settings):
    out = subprocess.run(['node', os.path.join(ROOT, 'tests', 'js_forecast.js'), os.path.join(ROOT, 'static', 'js', 'app.js'),
                          json.dumps(settings)], input=json.dumps(payload), capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


@pytest.mark.skipif(not shutil.which('node'), reason='нужен Node.js')
def test_work_calendar_matches_interface():
    settings = SETTINGS[2]
    cal = forecast.calendar_for(settings)
    rng = random.Random(11)
    queries = []
    for _ in range(300):
        a = forecast.add_days('2026-06-20', rng.randint(0, 220))
        if rng.random() < 0.5:
            queries.append(['span', a, forecast.add_days(a, rng.randint(-40, 400))])
        else:
            queries.append(['shift', a, rng.randint(-3, 800)])
    expected = [cal.span(q[1], q[2]) if q[0] == 'span' else cal.shift(q[1], q[2]) for q in queries]
    assert _js_query({'calendar': queries}, settings) == expected


def test_work_calendar_rules():
    cal = forecast.calendar_for({'workdays': True, 'holidays': ['2026-11-04'], 'extraWorkdays': ['2026-11-07']})
    assert cal.span('2026-11-02', '2026-11-09') == 5  # вт, чт, пт, рабочая суббота, пн; среда — праздник
    assert cal.shift('2026-11-02', 4) == '2026-11-07' and cal.shift('2026-11-06', 0) == '2026-11-06'
    assert cal.span('2026-11-09', '2026-11-02') == -5 and cal.span('2026-11-07', '2026-11-08') == 0
    assert forecast.calendar_for({}).span('2026-11-02', '2026-11-09') == 7


def test_workdays_forecast_skips_weekends_and_holidays():
    s = {'forecastMethod': 'average', 'riskBufferDays': 0, 'burnrateMethod': 'all', 'workdays': True,
         'holidays': ['2026-11-04']}
    # пн 02.11 — 50 ч, пт 06.11 — 30 ч: 20 ч за 3 рабочих дня (среда — праздник); 30 ч → ещё 4,5 → 5 рабочих дней
    st = forecast.compute_status(['2026-11-02', '2026-11-06'], [50, 30], '2026-11-13', s)
    assert st['forecastZero'] == '2026-11-13' and st['status'] == 'ok'
    assert forecast.compute_status(['2026-11-02', '2026-11-06'], [50, 30], '2026-11-13', dict(s, workdays=False))['forecastZero'] \
        == '2026-11-12'


def test_confidence_level_widens_range():
    dates = ['2026-09-01', '2026-09-08', '2026-09-15', '2026-09-22', '2026-09-29']
    rems = [200, 170, 160, 120, 110]
    base = {'forecastMethod': 'linear', 'riskBufferDays': 0, 'burnrateMethod': 'all'}
    r50 = forecast.monte_carlo(dates, rems, '2026-12-01', dict(base, rangeConfidence=50))
    r90 = forecast.monte_carlo(dates, rems, '2026-12-01', dict(base, rangeConfidence=90))
    assert r50['confidence'] == 50 and r90['confidence'] == 90 and r50['p50'] == r90['p50']
    assert r90['lo'] <= r50['lo'] <= r50['hi'] <= r90['hi'] and r90['lo'] < r90['hi']
    assert forecast.monte_carlo(dates, rems, None, dict(base, rangeConfidence=33))['confidence'] == 70


def test_js_round_and_fixed():
    assert forecast.js_round(2.5) == 3 and forecast.js_round(-2.5) == -2
    assert forecast._fixed6(0.0078125) == '0.007813' and forecast._fixed6(-0.0078125) == '-0.007813'
    assert forecast._fixed6(13.5833333333) == '13.583333' and forecast._fixed6(-0.0000001) == '-0.000000' and forecast._fixed6(-0.0) == '0.000000'


def test_aggregate_series_carries_last_value_and_backfills_first():
    """Эпик без замера на дату — последнее значение; до первого замера — первое (без ложного «роста объёма»);
    дата, на которую ни у одного эпика нет значения показателя, этот показатель пропускает."""
    rows = [[{'date': '2026-07-01', 'remaining': 10, 'spent': 1, 'estimate': 5},
             {'date': '2026-07-08', 'remaining': 6, 'spent': None, 'estimate': 5}],
            [{'date': '2026-07-05', 'remaining': 3, 'spent': 2, 'estimate': 4},
             {'date': '2026-07-10', 'remaining': None, 'spent': None, 'estimate': None}]]  # пустая «+ Новая дата»
    dates, series = forecast.aggregate_series(rows)
    assert dates == ['2026-07-01', '2026-07-05', '2026-07-08']
    assert series['remaining'] == [13, 13, 9] and series['spent'] == [3, 3, None] and series['estimate'] == [9, 9, 9]


def _js_aggregate(rows):
    out = subprocess.run(['node', os.path.join(ROOT, 'tests', 'js_forecast.js'), os.path.join(ROOT, 'static', 'js', 'app.js'),
                          '{}'], input=json.dumps({'aggregate': rows}), capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


@pytest.mark.skipif(not shutil.which('node'), reason='нужен Node.js')
def test_aggregate_series_matches_interface():
    rng = random.Random(7)
    for _ in range(30):
        rows = []
        for _e in range(rng.randint(1, 4)):
            day, epic = forecast.add_days('2026-07-01', rng.randint(0, 10)), []
            for _r in range(rng.randint(0, 6)):
                day = forecast.add_days(day, rng.randint(1, 8))
                epic.append({'date': day, **{f: (None if rng.random() < 0.2 else round(rng.uniform(0, 300), 1))
                                             for f in ('remaining', 'spent', 'estimate')}})
            rows.append(epic)
        dates, series = forecast.aggregate_series(rows)
        assert _js_aggregate(rows) == [dates, series['remaining'], series['spent'], series['estimate']]


def test_edge_cases_are_sensible():
    s = {'forecastMethod': 'linear', 'riskBufferDays': 3, 'burnrateMethod': 'all', 'burnrateWindowDays': 14}
    run = lambda rems, target: forecast.compute_status(EDGE_DATES[:len(rems)], rems, target, s)
    flat = run([120.7, 120.7, 120.7], '2026-10-01')
    assert flat['noBurn'] and flat['status'] == 'danger' and flat['forecastZero'] is None
    far = run([10000, 10000, 9999.99], '2026-12-31')
    assert far['tooFar'] and far['status'] == 'danger' and far['forecastZero'] is None
    done = run([100, 50, 0, 0, 0], '2026-09-19')
    assert done['finished'] and done['forecastZero'] == '2026-09-15' and done['status'] == 'ok'
    assert done['mc']['p50'] == '2026-09-15' and done['mc']['probability'] == 1.0 and done['mc']['lo'] == '2026-09-15'
    late = run([100, 10, 8, 7], '2026-09-20')  # 22.09 ещё 7 ч — цель 20.09 уже не достижима
    assert late['status'] == 'danger' and late['forecastZero'] > '2026-09-22'
    # дата прогноза, показанная пользователю, и статус — по одному и тому же дню
    eq = forecast.compute_status(['2026-10-01', '2026-10-06'], [100, 52], '2026-10-11',
                                 dict(s, riskBufferDays=0))
    assert eq['forecastZero'] == '2026-10-11' and eq['status'] == 'ok'


@pytest.mark.skipif(not shutil.which('node'), reason='нужен Node.js')
@pytest.mark.parametrize('settings', SETTINGS, ids=['linear-all', 'average-rolling', 'workdays-90', 'workdays-average-50'])
def test_burnup_forecast_matches_interface(settings):
    import app as app_module
    rng = random.Random(SETTINGS.index(settings) + 101)
    histories = []
    for _ in range(40):
        day, scope, done, hist = forecast.add_days('2026-07-01', rng.randint(0, 20)), rng.uniform(50, 400), 0.0, []
        for _r in range(rng.randint(1, 7)):
            day = forecast.add_days(day, rng.randint(0, 9))
            scope += rng.uniform(-5, 30)
            done = min(scope, done + rng.uniform(0, 60))
            hist.append({'date': day, 'scope': round(scope, 1), 'done': round(done, 1)})
        histories.append(hist)
    expected = []
    for h in histories:
        f = app_module.burnup_forecast(h, dict(forecast.DEFAULT_SETTINGS, **settings))
        expected.append([f['converges'], f['date'], bool(f.get('tooFar')), round(f['doneRate'] * 1e6) / 1e6,
                         round(f['scopeRate'] * 1e6) / 1e6] if f else None)
    got = _js_query({'burnup': histories}, settings)
    assert got == expected
