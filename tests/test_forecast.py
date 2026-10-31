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
                    [mc['p15'], mc['p50'], mc['p85'], mc['probability']] if mc else None])
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


SETTINGS = [
    {'forecastMethod': 'linear', 'riskBufferDays': 3, 'burnrateMethod': 'all', 'burnrateWindowDays': 14},
    {'forecastMethod': 'average', 'riskBufferDays': 0, 'burnrateMethod': 'rolling', 'burnrateWindowDays': 10},
]


@pytest.mark.skipif(not shutil.which('node'), reason='нужен Node.js')
@pytest.mark.parametrize('settings', SETTINGS, ids=['linear-all', 'average-rolling'])
def test_server_forecast_matches_interface(settings):
    cases = random_cases(hash(settings['forecastMethod']) & 0xFFFF)
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
]


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
    assert done['mc']['p50'] == '2026-09-15' and done['mc']['probability'] == 1.0
    late = run([100, 10, 8, 7], '2026-09-20')  # 22.09 ещё 7 ч — цель 20.09 уже не достижима
    assert late['status'] == 'danger' and late['forecastZero'] > '2026-09-22'
    # дата прогноза, показанная пользователю, и статус — по одному и тому же дню
    eq = forecast.compute_status(['2026-10-01', '2026-10-06'], [100, 52], '2026-10-11',
                                 dict(s, riskBufferDays=0))
    assert eq['forecastZero'] == '2026-10-11' and eq['status'] == 'ok'
