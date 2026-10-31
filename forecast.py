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
SLOPE_EPS = 1e-9  # наклон меньше — «не сгорает»: шум округления на ровном остатке, а не скорость
STATUS_RANK = {'ok': 0, 'risk': 1, 'danger': 2}
_M = 0xFFFFFFFF


def days_between(d1, d2):
    return (date.fromisoformat(d2) - date.fromisoformat(d1)).days


def add_days(d, n):
    return (date.fromisoformat(d) + timedelta(days=n)).isoformat()


def js_round(x):
    """Math.round: половина — вверх."""
    return math.floor(x + 0.5)


def js_sum(values):
    """Сумма слева направо, как в JavaScript (sum() в Python 3.12+ складывает float с компенсацией погрешности
    и в последнем знаке может разойтись с интерфейсом)."""
    total = 0
    for v in values:
        total += v
    return total


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
    sum_x = js_sum(x for x, _ in pts)
    sum_y = js_sum(y for _, y in pts)
    sum_xy = js_sum(x * y for x, y in pts)
    sum_xx = js_sum(x * x for x, _ in pts)
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


def zero_reached_date(pts):
    """Дата, когда остаток обнулился: начало последней серии замеров с остатком ≤ 0 (None, если остаток не нулевой)."""
    if not pts or pts[-1][1] > 0:
        return None
    i = len(pts) - 1
    while i > 0 and pts[i - 1][1] <= 0:
        i -= 1
    return pts[i][0]


def monte_carlo(dates, rems, target, settings):
    pts = [(d, y) for d, y in zip(dates, rems) if is_num(y)]
    if len(pts) < 3:
        return None
    intervals = burn_intervals(pts, settings)
    if len(intervals) < 2:
        return None
    last = pts[-1]
    done = zero_reached_date(pts)
    if done:  # остаток уже обнулён — дата завершения известна
        return {'p15': done, 'p50': done, 'p85': done,
                'probability': (1.0 if done <= target else 0.0) if target else None, 'intervals': len(intervals)}
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
    empty = {'status': 'unknown', 'forecastZero': None, 'noBurn': False, 'tooFar': False, 'finished': False,
             'burnRate': None, 'lastRemaining': None, 'lastDate': None, 'mc': None}
    if not dates:
        return empty
    base = dates[0]
    pts = [(days_between(base, d), y) for d, y in zip(dates, rems) if y is not None]
    burn = avg_burn_rate(dates, rems, settings)
    last_rem = pts[-1][1] if pts else None
    last_x = pts[-1][0] if pts else None
    last_date = add_days(base, last_x) if last_x is not None else dates[-1]
    target_x = days_between(base, target) if target else None
    status = 'notarget' if (pts and target_x is None) else 'unknown'
    buffer = settings.get('riskBufferDays') or 0

    def by_day(day):
        return 'ok' if day <= target_x else ('risk' if day <= target_x + buffer else 'danger')
    mc = monte_carlo(dates, rems, target, settings)
    done = zero_reached_date([(add_days(base, x), y) for x, y in pts])
    if done:  # работы завершены: прогноз — фактическая дата обнуления
        return dict(empty, status='notarget' if target_x is None else by_day(days_between(base, done)), forecastZero=done,
                    finished=True, burnRate=burn, lastRemaining=last_rem, lastDate=last_date, mc=mc)
    reg = None
    if settings.get('forecastMethod') == 'average':
        if burn is not None and last_rem is not None and last_x is not None:
            reg = (-burn, last_rem + burn * last_x)
    else:
        reg = linear_forecast(pts)
    forecast, too_far = None, False
    no_burn = bool(reg and reg[0] >= -SLOPE_EPS)
    if reg and reg[0] < -SLOPE_EPS:
        zero_x = -reg[1] / reg[0]
        if last_rem > 0 and zero_x < last_x:  # регрессия пересекла ноль раньше последнего замера — от последнего замера
            zero_x = last_x + last_rem / -reg[0]
        if zero_x - last_x > MC_HORIZON_DAYS:
            too_far = True
            if target_x is not None:
                status = 'danger'
        else:
            zero_day = js_round(zero_x)  # статус — по той же дате, что показана
            forecast = add_days(base, zero_day)
            if target_x is not None:
                status = by_day(zero_day)
    elif no_burn and target_x is not None:
        status = 'danger'
    return {'status': status, 'forecastZero': forecast, 'noBurn': no_burn, 'tooFar': too_far, 'finished': False,
            'burnRate': burn, 'lastRemaining': last_rem, 'lastDate': last_date, 'mc': mc}


def aggregate_series(rows_by_service):
    """Сумма по этапу на каждую дату замеров (как aggregateSeries в интерфейсе): для эпика без замера в эту дату —
    последнее известное значение, до первого замера — первое; дата, на которую ни у одного эпика нет значения
    показателя, для этого показателя пропускается (None)."""
    fields = ('remaining', 'spent', 'estimate')
    dates = sorted({r['date'] for rows in rows_by_service for r in rows if any(is_num(r[f]) for f in fields)})
    series = {f: [] for f in fields}
    first = [{f: next((r[f] for r in rows if is_num(r[f])), None) for f in fields} for rows in rows_by_service]
    last = [{f: None for f in fields} for _ in rows_by_service]
    pos = [0] * len(rows_by_service)
    for day in dates:
        measured = set()
        for i, rows in enumerate(rows_by_service):
            while pos[i] < len(rows) and rows[pos[i]]['date'] <= day:
                row = rows[pos[i]]
                pos[i] += 1
                for f in fields:
                    if is_num(row[f]):
                        last[i][f] = row[f]
                        if row['date'] == day:
                            measured.add(f)
        for f in fields:
            if f not in measured:
                series[f].append(None)
                continue
            vals = [lv[f] if lv[f] is not None else fv[f] for lv, fv in zip(last, first)]
            series[f].append(js_sum(v for v in vals if v is not None))
    return dates, series
