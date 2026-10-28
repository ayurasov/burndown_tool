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
        out.append([s['status'], s['forecastZero'], s['noBurn'],
                    [mc['p15'], mc['p50'], mc['p85'], mc['probability']] if mc else None])
    return out


def random_cases(seed, n=60):
    rng = random.Random(seed)
    cases = []
    for _ in range(n):
        day = forecast.add_days('2026-07-01', rng.randint(0, 30))
        dates, rems, rem = [], [], rng.uniform(50, 800)
        for _ in range(rng.randint(1, 9)):
            day = forecast.add_days(day, rng.randint(1, 9))
            rem = max(0.0, rem - rng.uniform(-40, 90))
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
    assert py_results(cases, settings) == js_results(cases, settings)


def test_js_round_and_fixed():
    assert forecast.js_round(2.5) == 3 and forecast.js_round(-2.5) == -2
    assert forecast._fixed6(0.0078125) == '0.007813' and forecast._fixed6(-0.0078125) == '-0.007813'
    assert forecast._fixed6(13.5833333333) == '13.583333' and forecast._fixed6(-0.0000001) == '-0.000000' and forecast._fixed6(-0.0) == '0.000000'


def test_aggregate_series_carries_last_value():
    rows = [[{'date': '2026-07-01', 'remaining': 10, 'spent': 1, 'estimate': 5},
             {'date': '2026-07-08', 'remaining': 6, 'spent': None, 'estimate': 5}],
            [{'date': '2026-07-05', 'remaining': 3, 'spent': 2, 'estimate': 4}]]
    dates, series = forecast.aggregate_series(rows)
    assert dates == ['2026-07-01', '2026-07-05', '2026-07-08']
    assert series['remaining'] == [10, 13, 9] and series['spent'] == [1, 3, 3]
