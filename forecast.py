"""Расчёт статусов и прогнозов на сервере — точная копия расчётов интерфейса (static/js/app.js).

Нужен для писем и сообщений (дайджест, оповещения): цифры в них должны совпадать с дашбордом. Поэтому здесь
повторены и правила округления JavaScript, и генератор случайных чисел Монте-Карло (mulberry32 с тем же ключом).
Даты — строки 'YYYY-MM-DD'.
"""

import math
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

MC_RUNS = 2000
MC_HORIZON_DAYS = 3 * 365
STATUS_RANK = {'ok': 0, 'risk': 1, 'danger': 2}
_M = 0xFFFFFFFF


def days_between(d1, d2):
    return (date.fromisoformat(d2) - date.fromisoformat(d1)).days


def add_days(d, n):
    return (date.fromisoformat(d) + timedelta(days=n)).isoformat()


def js_round(x):
    """Math.round: половина — вверх."""
    return math.floor(x + 0.5)


def is_num(v):
    return v is not None and not (isinstance(v, float) and math.isnan(v))


def settings_dict(row):
    """Строка settings → настройки как в интерфейсе (DEFAULT_SETTINGS)."""
    if not row:
        return {'forecastMethod': 'linear', 'riskBufferDays': 3, 'burnrateMethod': 'all', 'burnrateWindowDays': 14}
    return {'forecastMethod': row['forecast_method'] or 'linear', 'riskBufferDays': row['risk_buffer_days'],
            'burnrateMethod': row['burnrate_method'] or 'all', 'burnrateWindowDays': row['burnrate_window_days']}


def avg_burn_rate(dates, rems, settings):
    pts = [(d, y) for d, y in zip(dates, rems) if y is not None]
    if len(pts) < 2:
        return None
    if settings.get('burnrateMethod') == 'rolling':
        cutoff = add_days(pts[-1][0], -(settings.get('burnrateWindowDays') or 14))
        windowed = [p for p in pts if p[0] >= cutoff]
        pts = windowed if len(windowed) >= 2 else pts[-2:]
    days = days_between(pts[0][0], pts[-1][0])
    if days <= 0:
        return None
    return (pts[0][1] - pts[-1][1]) / days


def linear_forecast(points):
    pts = [(x, y) for x, y in points if is_num(y)]
    if len(pts) < 2:
        return None
    n = len(pts)
    sum_x = sum(x for x, _ in pts)
    sum_y = sum(y for _, y in pts)
    sum_xy = sum(x * y for x, y in pts)
    sum_xx = sum(x * x for x, _ in pts)
    denom = n * sum_xx - sum_x * sum_x
    if denom == 0:
        return None
    slope = (n * sum_xy - sum_x * sum_y) / denom
    return slope, (sum_y - slope * sum_x) / n


# ─── Монте-Карло (как monteCarloForecast в интерфейсе) ───────────

def _seeded_random(seed):
    state = [seed & _M]

    def rnd():
        s = (state[0] + 0x6D2B79F5) & _M
        state[0] = s
        t = ((s ^ (s >> 15)) * (1 | s)) & _M
        t = ((t + (((t ^ (t >> 7)) * (61 | t)) & _M)) & _M) ^ t
        return ((t ^ (t >> 14)) & _M) / 4294967296
    return rnd


def _hash_string(text):
    h = 2166136261
    for ch in text:
        h ^= ord(ch)
        h = (h * 16777619) & _M
    return h


def _fixed6(x):
    """Number.prototype.toFixed(6): половина — от нуля."""
    q = Decimal(abs(x)).quantize(Decimal('0.000001'), rounding=ROUND_HALF_UP)
    return ('-' if x < 0 else '') + format(q, 'f')  # как в JS: (-1e-7).toFixed(6) === '-0.000000'


def burn_intervals(pts, settings):
    out = []
    for (d0, y0), (d1, y1) in zip(pts, pts[1:]):
        length = days_between(d0, d1)
        if length > 0:
            out.append({'end': d1, 'len': length, 'rate': (y0 - y1) / length})
    if settings.get('burnrateMethod') == 'rolling' and out:
        cutoff = add_days(pts[-1][0], -(settings.get('burnrateWindowDays') or 14))
        windowed = [it for it in out if it['end'] > cutoff]
        out = windowed if len(windowed) >= 2 else out[-2:]
    return out


def mc_key(intervals, last, target):
    return ';'.join(f"{it['end']}:{it['len']}:{_fixed6(it['rate'])}" for it in intervals) + \
        f'|{last[0]}:{_fixed6(last[1])}|{target or ""}'


def monte_carlo(dates, rems, target, settings):
    pts = [(d, y) for d, y in zip(dates, rems) if is_num(y)]
    if len(pts) < 3:
        return None
    intervals = burn_intervals(pts, settings)
    if len(intervals) < 2:
        return None
    last = pts[-1]
    if last[1] <= 0:
        return {'p15': last[0], 'p50': last[0], 'p85': last[0],
                'probability': (1.0 if last[0] <= target else 0.0) if target else None, 'intervals': len(intervals)}
    total = sum(it['len'] for it in intervals)
    rnd = _seeded_random(_hash_string(mc_key(intervals, last, target)))
    days = []
    for _ in range(MC_RUNS):
        rem, day = last[1], 0
        while day < MC_HORIZON_DAYS:
            pick, it = rnd() * total, intervals[0]
            for candidate in intervals:
                pick -= candidate['len']
                if pick <= 0:
                    it = candidate
                    break
            if it['rate'] > 0 and rem <= it['rate'] * it['len']:
                day += rem / it['rate']
                rem = 0
                break
            rem -= it['rate'] * it['len']
            day += it['len']
        days.append(day if rem <= 0 else math.inf)
    days.sort()

    def at(q):
        d = days[math.floor(q * (len(days) - 1))]
        return add_days(last[0], math.ceil(d)) if math.isfinite(d) else None
    probability = None
    if target:
        limit = days_between(last[0], target)
        probability = sum(1 for d in days if math.isfinite(d) and math.ceil(d) <= limit) / len(days)
    return {'p15': at(0.15), 'p50': at(0.5), 'p85': at(0.85), 'probability': probability, 'intervals': len(intervals)}


# ─── Статус (как computeStatus в интерфейсе) ─────────────────────

def compute_status(dates, rems, target, settings):
    empty = {'status': 'unknown', 'forecastZero': None, 'noBurn': False, 'burnRate': None, 'lastRemaining': None,
             'lastDate': None, 'mc': None}
    if not dates:
        return empty
    base = dates[0]
    pts = [(days_between(base, d), y) for d, y in zip(dates, rems) if y is not None]
    burn = avg_burn_rate(dates, rems, settings)
    last_rem = pts[-1][1] if pts else None
    last_x = pts[-1][0] if pts else None
    last_date = add_days(base, last_x) if last_x is not None else dates[-1]
    reg = None
    if settings.get('forecastMethod') == 'average':
        if burn is not None and last_rem is not None and last_x is not None:
            reg = (-burn, last_rem + burn * last_x)
    else:
        reg = linear_forecast(pts)
    target_x = days_between(base, target) if target else None
    status = 'notarget' if (pts and target_x is None) else 'unknown'
    forecast = None
    no_burn = bool(reg and reg[0] >= 0)
    if reg and reg[0] < 0:
        zero_x = -reg[1] / reg[0]
        forecast = add_days(base, js_round(zero_x))
        if target_x is not None:
            buffer = settings.get('riskBufferDays') or 0
            status = 'ok' if zero_x <= target_x else ('risk' if zero_x <= target_x + buffer else 'danger')
    elif no_burn and target_x is not None:
        status = 'danger'
    return {'status': status, 'forecastZero': forecast, 'noBurn': no_burn, 'burnRate': burn, 'lastRemaining': last_rem,
            'lastDate': last_date, 'mc': monte_carlo(dates, rems, target, settings)}


def aggregate_series(rows_by_service):
    """Сумма по этапу на каждую дату замеров с последним известным значением эпика (как aggregateStageSeries)."""
    dates = sorted({r['date'] for rows in rows_by_service for r in rows})
    fields = ('remaining', 'spent', 'estimate')
    series = {f: [] for f in fields}
    last = [{f: None for f in fields} for _ in rows_by_service]
    pos = [0] * len(rows_by_service)
    for day in dates:
        for i, rows in enumerate(rows_by_service):
            while pos[i] < len(rows) and rows[pos[i]]['date'] <= day:
                row = rows[pos[i]]
                pos[i] += 1
                for f in fields:
                    if is_num(row[f]):
                        last[i][f] = row[f]
        for f in fields:
            vals = [lv[f] for lv in last if lv[f] is not None]
            series[f].append(sum(vals) if vals else None)
    return dates, series
