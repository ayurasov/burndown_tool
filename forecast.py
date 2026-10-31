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
MC_HORIZON_WORKDAYS = 3 * 261  # три года в рабочих днях
MC_QUANTILES = {50: (0.25, 0.75), 70: (0.15, 0.85), 80: (0.1, 0.9), 90: (0.05, 0.95)}  # уровень доверия диапазона
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


DEFAULT_SETTINGS = {
    'forecastMethod': 'linear', 'riskBufferDays': 3, 'burnrateMethod': 'all', 'burnrateWindowDays': 14,
    'rangeConfidence': 70, 'workdays': False, 'holidays': [], 'extraWorkdays': [],
    'units': 'hours', 'hoursPerDay': 8,
    'staleDays': 10, 'scopeThreshold': 10, 'slipThreshold': 7, 'chanceThreshold': 50, 'insightWindowDays': 28,
}
# ключ настроек в интерфейсе → столбец таблицы settings
SETTINGS_COLUMNS = [
    ('forecastMethod', 'forecast_method'), ('riskBufferDays', 'risk_buffer_days'),
    ('burnrateMethod', 'burnrate_method'), ('burnrateWindowDays', 'burnrate_window_days'),
    ('rangeConfidence', 'range_confidence'), ('workdays', 'workdays'), ('holidays', 'holidays'),
    ('extraWorkdays', 'extra_workdays'), ('units', 'units'), ('hoursPerDay', 'hours_per_day'),
    ('staleDays', 'stale_days'), ('scopeThreshold', 'scope_threshold'), ('slipThreshold', 'slip_threshold'),
    ('chanceThreshold', 'chance_threshold'), ('insightWindowDays', 'insight_window_days'),
]
DATE_LIST_KEYS = ('holidays', 'extraWorkdays')


def parse_date_list(text):
    """'2026-01-01,2026-01-02' → ['2026-01-01', '2026-01-02'] (хранение списков дат календаря в БД)."""
    items = (part.strip() for part in (text or '').replace('\n', ',').split(','))
    return sorted({d for d in items if len(d) == 10 and d[4] == '-' and d[7] == '-'})


def settings_dict(row):
    """Строка settings → настройки как в интерфейсе (DEFAULT_SETTINGS; пустые значения — по умолчанию)."""
    out = dict(DEFAULT_SETTINGS, holidays=[], extraWorkdays=[])
    if not row:
        return out
    keys = row.keys()
    for key, col in SETTINGS_COLUMNS:
        if col not in keys or row[col] is None:
            continue
        value = row[col]
        if key in DATE_LIST_KEYS:
            value = parse_date_list(value)
        elif key == 'workdays':
            value = bool(value)
        out[key] = value
    return out


# ─── Календарь расчёта сроков (как workCalendar в интерфейсе) ────

_EPOCH = date(1970, 1, 1).toordinal()


def _ordinal(d):
    return date.fromisoformat(d).toordinal() - _EPOCH


def _iso(o):
    return date.fromordinal(o + _EPOCH).isoformat()


class _CalendarDays:
    """Календарные дни: span(a, b) — дней между датами, shift(a, n) — дата через n дней."""
    workdays = False
    horizon = MC_HORIZON_DAYS
    signature = ''

    @staticmethod
    def span(a, b):
        return days_between(a, b)

    @staticmethod
    def shift(a, n):
        return add_days(a, n)


CALENDAR_DAYS = _CalendarDays()


class WorkCalendar:
    """Рабочие дни: пн–пт, кроме праздников, плюс перенесённые рабочие дни.
    span(a, b) — число рабочих дней в (a, b], shift(a, n) — n-й рабочий день после a."""
    workdays = True
    horizon = MC_HORIZON_WORKDAYS

    def __init__(self, holidays, extra):
        self.on = {_ordinal(d) for d in extra}
        self.off = {_ordinal(d) for d in holidays} - self.on
        self._off = sorted(self.off)
        self._on = sorted(self.on)
        self.signature = 'w:' + ','.join(holidays) + '|' + ','.join(extra)

    def is_work(self, o):
        if o in self.on:
            return True
        if o in self.off:
            return False
        return (o + 3) % 7 < 5  # 01.01.1970 — четверг; пн = 0

    def _count(self, oa, ob):
        n = ob - oa
        full = n // 7
        count = full * 5
        for o in range(oa + full * 7 + 1, ob + 1):
            if (o + 3) % 7 < 5:
                count += 1
        for o in self._off:
            if oa < o <= ob and (o + 3) % 7 < 5:
                count -= 1
        for o in self._on:
            if oa < o <= ob and (o + 3) % 7 >= 5:
                count += 1
        return count

    def span(self, a, b):
        oa, ob = _ordinal(a), _ordinal(b)
        return self._count(oa, ob) if ob >= oa else -self._count(ob, oa)

    def shift(self, a, n):
        if n <= 0:
            return add_days(a, n)
        o, count = _ordinal(a), 0
        while count < n:
            o += 1
            if self.is_work(o):
                count += 1
        return _iso(o)


def calendar_for(settings):
    if not settings.get('workdays'):
        return CALENDAR_DAYS
    return WorkCalendar(list(settings.get('holidays') or []), list(settings.get('extraWorkdays') or []))


def avg_burn_rate(dates, rems, settings, cal=None):
    cal = cal or calendar_for(settings)
    pts = [(d, y) for d, y in zip(dates, rems) if y is not None]
    if len(pts) < 2:
        return None
    if settings.get('burnrateMethod') == 'rolling':
        cutoff = add_days(pts[-1][0], -(settings.get('burnrateWindowDays') or 14))
        windowed = [p for p in pts if p[0] >= cutoff]
        pts = windowed if len(windowed) >= 2 else pts[-2:]
    days = cal.span(pts[0][0], pts[-1][0])
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


def burn_intervals(pts, settings, cal=None):
    cal = cal or calendar_for(settings)
    out = []
    for (d0, y0), (d1, y1) in zip(pts, pts[1:]):
        length = cal.span(d0, d1)
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


_MC_CACHE = {}
_MC_CACHE_LIMIT = 4096


def monte_carlo(dates, rems, target, settings, cal=None):
    cal = cal or calendar_for(settings)
    pts = [(d, y) for d, y in zip(dates, rems) if is_num(y)]
    if len(pts) < 3:
        return None
    intervals = burn_intervals(pts, settings, cal)
    if len(intervals) < 2:
        return None
    last = pts[-1]
    confidence = settings.get('rangeConfidence') if settings.get('rangeConfidence') in MC_QUANTILES else 70
    q_lo, q_hi = MC_QUANTILES[confidence]
    seed_key = mc_key(intervals, last, target)
    cache_key = seed_key + '|' + cal.signature + '|' + str(confidence)
    if cache_key in _MC_CACHE:
        return dict(_MC_CACHE[cache_key])
    done = zero_reached_date(pts)
    if done:  # остаток уже обнулён — дата завершения известна
        res = {'lo': done, 'p50': done, 'hi': done, 'confidence': confidence,
               'probability': (1.0 if done <= target else 0.0) if target else None, 'intervals': len(intervals)}
    else:
        total = sum(it['len'] for it in intervals)
        lens = [it['len'] for it in intervals]
        rates = [it['rate'] for it in intervals]
        horizon = cal.horizon
        days = []
        if max(rates) <= 0:  # остаток ни на одном интервале не уменьшался — ни один сценарий не завершится
            days = [math.inf] * MC_RUNS
        state = _hash_string(seed_key) & _M
        # Сценарий, который уже точно не успеет до горизонта (даже на самой высокой скорости), можно не досчитывать:
        # при одинаковой длине интервалов число оставшихся шагов известно, а генератор mulberry32 — счётчик с шагом
        # 0x6D2B79F5, поэтому пропуск шагов не меняет последовательность — результат тот же, что в интерфейсе.
        uniform = lens[0] if len(set(lens)) == 1 else None
        bound = max(rates) * 1.000000001
        for _ in range(MC_RUNS if not days else 0):
            rem, day = last[1], 0
            while day < horizon:
                if uniform and rem > bound * (horizon - day + uniform):
                    state = (state + -(-(horizon - day) // uniform) * 0x6D2B79F5) & _M
                    break
                # mulberry32 (как seededRandom в интерфейсе) — встроен в цикл ради скорости
                state = (state + 0x6D2B79F5) & _M
                t = ((state ^ (state >> 15)) * (1 | state)) & _M
                t = ((t + (((t ^ (t >> 7)) * (61 | t)) & _M)) & _M) ^ t
                pick = (((t ^ (t >> 14)) & _M) / 4294967296) * total
                k = 0
                for k, length in enumerate(lens):
                    pick -= length
                    if pick <= 0:
                        break
                else:
                    k = 0  # как в интерфейсе: без попадания — первый интервал
                rate, length = rates[k], lens[k]
                if rate > 0 and rem <= rate * length:
                    day += rem / rate
                    rem = 0
                    break
                rem -= rate * length
                day += length
            days.append(day if rem <= 0 else math.inf)
        days.sort()

        def at(q):
            d = days[math.floor(q * (len(days) - 1))]
            return cal.shift(last[0], math.ceil(d)) if math.isfinite(d) else None
        probability = None
        if target:
            limit = cal.span(last[0], target)
            probability = sum(1 for d in days if math.isfinite(d) and math.ceil(d) <= limit) / len(days)
        res = {'lo': at(q_lo), 'p50': at(0.5), 'hi': at(q_hi), 'confidence': confidence, 'probability': probability,
               'intervals': len(intervals)}
    if len(_MC_CACHE) >= _MC_CACHE_LIMIT:
        _MC_CACHE.clear()
    _MC_CACHE[cache_key] = res
    return dict(res)


# ─── Статус (как computeStatus в интерфейсе) ─────────────────────

def compute_status(dates, rems, target, settings, with_mc=True):
    cal = calendar_for(settings)
    empty = {'status': 'unknown', 'forecastZero': None, 'noBurn': False, 'tooFar': False, 'finished': False,
             'burnRate': None, 'lastRemaining': None, 'lastDate': None, 'mc': None, 'slope': None}
    if not dates:
        return empty
    base = dates[0]
    filled = [(d, y) for d, y in zip(dates, rems) if y is not None]
    pts = [(cal.span(base, d), y) for d, y in filled]
    burn = avg_burn_rate(dates, rems, settings, cal)
    last_rem = pts[-1][1] if pts else None
    last_x = pts[-1][0] if pts else None
    last_date = filled[-1][0] if filled else dates[-1]
    target_x = cal.span(base, target) if target else None
    status = 'notarget' if (pts and target_x is None) else 'unknown'
    buffer = settings.get('riskBufferDays') or 0

    def by_day(day):
        return 'ok' if day <= target_x else ('risk' if day <= target_x + buffer else 'danger')
    mc = monte_carlo(dates, rems, target, settings, cal) if with_mc else None
    done = zero_reached_date(filled)
    if done:  # работы завершены: прогноз — фактическая дата обнуления
        return dict(empty, status='notarget' if target_x is None else by_day(cal.span(base, done)), forecastZero=done,
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
        if zero_x - last_x > cal.horizon:
            too_far = True
            if target_x is not None:
                status = 'danger'
        else:
            zero_day = js_round(zero_x)  # статус — по той же дате, что показана
            forecast = cal.shift(base, zero_day)
            if forecast < last_date:  # замер в выходной: не раньше последнего замера
                forecast = last_date
            if target_x is not None:
                status = by_day(zero_day)
    elif no_burn and target_x is not None:
        status = 'danger'
    return {'status': status, 'forecastZero': forecast, 'noBurn': no_burn, 'tooFar': too_far, 'finished': False,
            'burnRate': burn, 'lastRemaining': last_rem, 'lastDate': last_date, 'mc': mc,
            'slope': reg[0] if reg else None}


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
