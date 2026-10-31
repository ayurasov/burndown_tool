"""«Выводы» для управленческой работы: что происходит со сроками и почему — на уровне эпика, этапа, проекта и
портфеля проектов.

Для каждого ряда замеров (эпик или сумма по этапу) считаются драйверы прогноза: насколько прогноз расходится
с целью, какой темп нужен, чтобы успеть, и как он соотносится с текущим, ускоряется или замедляется работа,
растёт ли объём, куда сдвигается прогноз, насколько свежи данные. Из драйверов получаются выводы — короткие
фразы с важностью (danger — не успеваем, risk — риск, info — к сведению, ok — хорошо), из выводов эпиков
и этапов — сводка проекта: главное, ключевые цифры, первоочередные риски и состояние данных.

Статусы и прогнозы — те же, что на дашборде (forecast.py повторяет расчёт интерфейса). Пороги выводов —
в настройках проекта.
"""

import math

import forecast as fc

SEVERITY_RANK = {'danger': 3, 'risk': 2, 'info': 1, 'ok': 0}
STATUS_ORDER = {'unknown': -1, 'notarget': -1, 'ok': 0, 'risk': 1, 'danger': 2}
TREND_CHANGE = 0.3  # изменение темпа больше чем на 30 % — вывод «темп снизился/вырос»
PACE_MARGIN = 0.8   # нужный темп не больше 80 % текущего — «запас по темпу»


# ─── Форматирование ──────────────────────────────────────────────

def ru_num(v, digits=2):
    """Число по-русски, как в интерфейсе: 1 188,3 (половина — вверх, как Math.round)."""
    scale = 10 ** digits
    text = f'{fc.js_round(v * scale) / scale:,.{digits}f}'.rstrip('0').rstrip('.') if digits else f'{fc.js_round(v):,}'
    text = text.replace(',', ' ').replace('.', ',')
    return '0' if text in ('-0', '') else text


def fmt_date(d):
    return '.'.join(reversed(d.split('-'))) if d else '—'


def unit_label(settings):
    return 'чел.-дн.' if settings.get('units') == 'days' else 'ч'


def to_units(v, settings):
    return v / (settings.get('hoursPerDay') or 8) if settings.get('units') == 'days' else v


def fmt_work(v, settings):
    """Объём работ в единицах проекта: «12,5 ч» или «1,6 чел.-дн.» (замеры хранятся в часах)."""
    return '—' if v is None else f'{ru_num(to_units(v, settings))} {unit_label(settings)}'


def rate_label(settings):
    return f"{unit_label(settings)}/{'раб. день' if settings.get('workdays') else 'день'}"


def fmt_rate(v, settings):
    return '—' if v is None else f'{ru_num(to_units(v, settings))} {rate_label(settings)}'


def plural(n, one, few, many):
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return one
    return few if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14 else many


def days_text(n):
    return f"{n} {plural(n, 'день', 'дня', 'дней')}"


def finding(severity, kind, text, weight=0):
    return {'severity': severity, 'kind': kind, 'text': text, 'weight': weight}


def _sorted(findings):
    return sorted(findings, key=lambda f: -SEVERITY_RANK[f['severity']])  # sorted устойчив: порядок важности внутри


# ─── Драйверы ряда замеров ───────────────────────────────────────

def _rate(points, cal):
    """Скорость сгорания между первой и последней точкой (в день расчёта)."""
    if len(points) < 2:
        return None
    span = cal.span(points[0][0], points[-1][0])
    return (points[0][1] - points[-1][1]) / span if span > 0 else None


def _value_at_or_before(points, day):
    """Последняя точка не позже day (иначе — первая)."""
    best = points[0]
    for p in points:
        if p[0] <= day:
            best = p
    return best


def series_drivers(dates, remaining, estimate, target, settings, today, scope_history=None, with_mc=True):
    """Драйверы прогноза ряда замеров → словарь (статус, прогноз, темпы, сдвиг, рост объёма, свежесть).
    scope_history — объём по срезам задач Jira [(дата, объём)], если есть (точнее оценки из замеров)."""
    cal = fc.calendar_for(settings)
    st = fc.compute_status(dates, remaining, target, settings, with_mc=with_mc)
    filled = [(d, y) for d, y in zip(dates, remaining) if fc.is_num(y)]
    window = settings.get('insightWindowDays') or 28
    out = {'status': st['status'], 'forecast': st['forecastZero'], 'target': target, 'noBurn': st['noBurn'],
           'tooFar': st['tooFar'], 'finished': st['finished'], 'remaining': st['lastRemaining'], 'lastDate': None,
           'burnRate': st['burnRate'], 'probability': None, 'lo': None, 'hi': None, 'confidence': None,
           'requiredRate': None, 'paceRatio': None, 'recentRate': None, 'priorRate': None, 'trendPct': None,
           'slipDays': None, 'slipFrom': None, 'scopePct': None, 'scopeAbs': None, 'scopeSince': None, 'scopeSource': None,
           'staleDays': None, 'lateDays': None, 'measurements': len(filled), 'lastDecrease': None}
    if st['mc']:
        out.update(probability=st['mc']['probability'], lo=st['mc']['lo'], hi=st['mc']['hi'],
                   confidence=st['mc']['confidence'])
    if not filled:
        return out
    last_date, last_rem = filled[-1]
    out['lastDate'] = last_date
    out['staleDays'] = fc.days_between(last_date, today) if today >= last_date else 0
    if target and st['forecastZero']:
        out['lateDays'] = fc.days_between(target, st['forecastZero'])
    for i in range(len(filled) - 1, 0, -1):
        if filled[i][1] < filled[i - 1][1]:
            out['lastDecrease'] = filled[i][0]
            break
    # нужный темп: сколько сгорать в день, чтобы успеть к цели, — и во сколько раз это больше текущего
    if target and target > last_date and last_rem > 0 and not st['finished']:
        days_left = cal.span(last_date, target)
        if days_left > 0:
            out['requiredRate'] = last_rem / days_left
            if st['burnRate'] and st['burnRate'] > 0:
                out['paceRatio'] = out['requiredRate'] / st['burnRate']
    # тренд темпа: последние window дней против времени до них
    cutoff = fc.add_days(last_date, -window)
    recent = [p for p in filled if p[0] >= cutoff]
    prior = [p for p in filled if p[0] <= (recent[0][0] if recent else last_date)]
    if len(recent) >= 2 and len(prior) >= 2:
        out['recentRate'], out['priorRate'] = _rate(recent, cal), _rate(prior, cal)
        if out['recentRate'] is not None and out['priorRate'] and out['priorRate'] > 0:
            out['trendPct'] = (out['recentRate'] / out['priorRate'] - 1) * 100
    # рост объёма за период: по срезам задач Jira, иначе — по оценке в замерах
    scope = [(d, v) for d, v in (scope_history or []) if fc.is_num(v)]
    source = 'jira'
    if len(scope) < 2:
        scope = [(d, v) for d, v in zip(dates, estimate or []) if fc.is_num(v)]
        source = 'estimate'
    if len(scope) >= 2:
        base = _value_at_or_before(scope, fc.add_days(scope[-1][0], -window))
        if base[0] < scope[-1][0] and base[1] > 0:
            out.update(scopeAbs=scope[-1][1] - base[1], scopePct=(scope[-1][1] - base[1]) / base[1] * 100,
                       scopeSince=base[0], scopeSource=source)
    # сдвиг прогноза: прогноз на замер window дней назад против текущего
    if st['forecastZero'] and not st['finished']:
        idx = [i for i, d in enumerate(dates) if d <= cutoff]
        if idx:
            k = idx[-1] + 1
            prev = fc.compute_status(dates[:k], remaining[:k], target, settings, with_mc=False)
            if prev['forecastZero'] and not prev['finished']:
                out['slipDays'] = fc.days_between(prev['forecastZero'], st['forecastZero'])
                out['slipFrom'] = {'date': prev['lastDate'], 'forecast': prev['forecastZero']}
    return out


def series_findings(drv, settings, today, label=''):
    """Выводы по драйверам ряда. label — «эпика», «этапа» (для текста о цели и прогнозе)."""
    f = []
    rem, target, fz = drv['remaining'], drv['target'], drv['forecast']
    if not drv['measurements']:
        return [finding('info', 'nodata', 'Нет замеров остатка — прогноз не строится.')]
    if drv['finished']:
        late = target and fz > target
        text = f'Работы завершены {fmt_date(fz)}' + (
            f' — позже цели на {days_text(fc.days_between(target, fz))}.' if late else (' — к цели.' if target else '.'))
        return [finding('info' if late else 'ok', 'finished', text)]
    if drv['measurements'] == 1:
        f.append(finding('info', 'fewdata', 'Один замер — для прогноза нужен ещё хотя бы один.'))
    overdue = bool(target and target < today and rem and rem > 0)
    chance = None if drv['probability'] is None else fc.js_round(drv['probability'] * 100)
    chance_text = f'; шанс успеть — {chance} %' if chance is not None else ''
    if overdue:
        f.append(finding('danger', 'overdue', f'Цель {fmt_date(target)} прошла, осталось {fmt_work(rem, settings)}.',
                         weight=fc.days_between(target, today) + 1000))
    if drv['noBurn']:
        if drv['lastDecrease'] == drv['lastDate']:
            since = ', хотя последний замер показал снижение'
        elif drv['lastDecrease']:
            since = f', последнее снижение — {fmt_date(drv["lastDecrease"])}'
        else:
            since = ''
        f.append(finding('danger' if target else 'risk', 'noburn',
                         'Остаток в целом не уменьшается — по замерам срок завершения не прогнозируется '
                         f'(сейчас {fmt_work(rem, settings)}{since}).', weight=900))
    elif drv['tooFar']:
        f.append(finding('danger' if target else 'risk', 'toofar',
                         f'При текущем темпе ({fmt_rate(drv["burnRate"], settings)}) работ больше чем на 3 года.', weight=900))
    elif fz and target and not overdue:
        late = drv['lateDays']
        if drv['status'] in ('risk', 'danger'):
            buffer = ' — в пределах буфера риска' if drv['status'] == 'risk' else ''
            f.append(finding(drv['status'], 'late', f'Прогноз {fmt_date(fz)} — позже цели {fmt_date(target)} '
                             f'на {days_text(late)}{buffer}{chance_text}.', weight=late))
        else:
            ahead = f'раньше цели на {days_text(-late)}' if late < 0 else 'точно к цели'
            rng = ''
            if drv['lo'] and drv['hi'] and drv['lo'] != drv['hi']:
                rng = f'; диапазон {drv["confidence"]} %: {fmt_date(drv["lo"])} — {fmt_date(drv["hi"])}'
            f.append(finding('ok', 'ontrack', f'Прогноз {fmt_date(fz)} — {ahead}{chance_text}{rng}.'))
            threshold = settings.get('chanceThreshold') or 50
            if chance is not None and chance < threshold:
                f.append(finding('risk', 'chance', f'Прогноз в срок, но шанс успеть — только {chance} %: '
                                 'темп между замерами неровный.', weight=threshold - chance))
    elif fz and not target:
        f.append(finding('info', 'notarget', f'Нет целевой даты — прогноз {fmt_date(fz)}, статус не определяется.'))
    elif not target:
        f.append(finding('info', 'notarget', 'Нет целевой даты — статус не определяется.'))
    # нужный темп против текущего
    ratio = drv['paceRatio']
    if ratio is not None and not drv['noBurn'] and not overdue:
        need, now = fmt_rate(drv['requiredRate'], settings), fmt_rate(drv['burnRate'], settings)
        if drv['status'] in ('risk', 'danger') and ratio > 1.05:
            f.append(finding(drv['status'], 'pace', f'Чтобы успеть к цели, нужно {need} — в {ru_num(ratio, 1)} раза '
                             f'быстрее текущего темпа ({now}).', weight=ratio * 10))
        elif drv['status'] == 'ok' and ratio <= PACE_MARGIN:
            f.append(finding('ok', 'pace', f'Запас по темпу: для цели достаточно {need}, текущий темп — {now}.'))
    # тренд темпа
    window = settings.get('insightWindowDays') or 28
    trend = drv['trendPct']
    if trend is not None and not drv['noBurn']:
        detail = (f'за последние {days_text(window)} — {fmt_rate(drv["recentRate"], settings)}, '
                  f'раньше — {fmt_rate(drv["priorRate"], settings)}')
        if trend <= -TREND_CHANGE * 100:
            f.append(finding('risk', 'slowdown', f'Темп снизился на {fc.js_round(-trend)} %: {detail}.', weight=-trend / 10))
        elif trend >= TREND_CHANGE * 100:
            f.append(finding('ok', 'speedup', f'Темп вырос на {fc.js_round(trend)} %: {detail}.'))
    # сдвиг прогноза
    slip, slip_limit = drv['slipDays'], settings.get('slipThreshold') or 7
    if slip is not None and drv['slipFrom']:
        was = f'на замер {fmt_date(drv["slipFrom"]["date"])} был {fmt_date(drv["slipFrom"]["forecast"])}'
        if slip > slip_limit:
            f.append(finding('risk', 'slip', f'Прогноз сдвинулся на {days_text(slip)} позже: {was}.', weight=slip))
        elif slip < -slip_limit:
            f.append(finding('ok', 'slip', f'Прогноз улучшился на {days_text(-slip)}: {was}.'))
    # рост объёма
    pct, scope_limit = drv['scopePct'], settings.get('scopeThreshold') or 10
    if pct is not None:
        src = 'объём задач Jira' if drv['scopeSource'] == 'jira' else 'оценка'
        sign = '+' if drv['scopeAbs'] > 0 else '−'
        detail = f'с {fmt_date(drv["scopeSince"])} ({sign}{fmt_work(abs(drv["scopeAbs"]), settings)})'
        if pct > scope_limit:
            grown = 'вырос' if drv['scopeSource'] == 'jira' else 'выросла'
            how = f'в {ru_num(pct / 100 + 1, 1)} раза' if pct >= 200 else f'на {fc.js_round(pct)} %'
            f.append(finding('risk', 'scope', f'{src[0].upper() + src[1:]} {grown} {how} {detail}: '
                             'сроки сдвигает рост объёма, а не только темп.', weight=min(pct, 200) / 5))
        elif pct < -scope_limit:
            fell = 'уменьшился' if drv['scopeSource'] == 'jira' else 'уменьшилась'
            f.append(finding('info', 'scope', f'{src[0].upper() + src[1:]} {fell} на {fc.js_round(-pct)} % {detail}.'))
    # свежесть данных
    stale_limit = settings.get('staleDays') or 10
    if drv['staleDays'] is not None and drv['staleDays'] > stale_limit:
        f.append(finding('risk', 'stale', f'Последний замер {fmt_date(drv["lastDate"])} — {days_text(drv["staleDays"])} '
                         'назад: прогноз мог устареть.', weight=drv['staleDays'] / 10))
    return _sorted(f)


# ─── Эпик, этап, проект ──────────────────────────────────────────

def epic_insight(epic, rows, settings, today, comp_history=None, comp_latest=None):
    """epic: {'id', 'name', 'etap', 'target'}; rows — замеры по датам; comp_* — состав задач Jira."""
    drv = series_drivers([r['date'] for r in rows], [r['remaining'] for r in rows], [r['estimate'] for r in rows],
                         epic['target'], settings, today, [(p['date'], p['scope']) for p in comp_history or []])
    found = series_findings(drv, settings, today)
    if comp_latest and comp_latest.get('problemIssues'):
        n, total = comp_latest['problemIssues'], comp_latest.get('total') or 0
        severity = 'risk' if total and n / total >= 0.2 else 'info'
        found.append(finding(severity, 'quality', f"{n} {plural(n, 'задача', 'задачи', 'задач')} Jira с неполными данными "
                             '(без оценки, закрыта с остатком и т. п.) — суммы эпика могут вводить в заблуждение.', weight=n))
        found = _sorted(found)
    return dict(drv, id=epic['id'], name=epic['name'], stage=epic.get('stage'), findings=found,
                headline=found[0] if found else None, problems=(comp_latest or {}).get('problemIssues'))


def _names(items, limit=3):
    names = [f'«{e["name"]}»' for e in items[:limit]]
    return ', '.join(names) + (f' и ещё {len(items) - limit}' if len(items) > limit else '')


def _slowest(epics):
    """Эпик, который завершится позже всех: «не сгорает» и «> 3 лет» — раньше любых дат."""
    pending = [e for e in epics if not e['finished'] and e['measurements']]
    blocked = [e for e in pending if e['noBurn'] or e['tooFar']]
    if blocked:
        return blocked[0]
    dated = [e for e in pending if e['forecast']]
    return max(dated, key=lambda e: e['forecast']) if dated else None


def stage_insight(name, target, epics, dates, series, settings, today, scope_history=None):
    """Этап: сумма по эпикам (как в своде) плюс выводы по составу — кто определяет срок и кто под угрозой."""
    drv = series_drivers(dates, series['remaining'], series['estimate'], target, settings, today, scope_history)
    found = series_findings(drv, settings, today)
    slowest = _slowest(epics)
    if slowest and len(epics) > 1:
        when = 'не сгорает' if slowest['noBurn'] else ('> 3 лет' if slowest['tooFar'] else fmt_date(slowest['forecast']))
        late = target and (slowest['noBurn'] or slowest['tooFar'] or slowest['forecast'] > target)
        if late and drv['status'] == 'ok':
            found.append(finding('risk', 'critical', f'Сумма по этапу успевает, но эпик «{slowest["name"]}» — нет '
                                 f'(прогноз: {when}): срок этапа определяет он.', weight=500))
        elif late:
            found.append(finding('info', 'critical', f'Срок этапа определяет эпик «{slowest["name"]}» (прогноз: {when}).'))
        elif target:
            found.append(finding('ok', 'critical', f'Позже всех завершится эпик «{slowest["name"]}» — {when}, к цели этапа.'))
    at_risk = [e for e in epics if e['status'] in ('risk', 'danger') and not e['finished']]
    if at_risk:
        found.append(finding('risk' if any(e['status'] == 'danger' for e in at_risk) else 'info', 'epics',
                             f'Под угрозой {len(at_risk)} из {len(epics)} {plural(len(epics), "эпика", "эпиков", "эпиков")}: '
                             f'{_names(at_risk)}.', weight=len(at_risk) * 50))
    stale = [e for e in epics if 'stale' in {x['kind'] for x in e['findings']}]
    found = [x for x in found if x['kind'] != 'stale' or not stale]  # свежесть этапа — по эпикам, а не по сумме
    if stale:
        found.append(finding('risk', 'stale', f'Замеры устарели у {len(stale)} из {len(epics)} '
                             f'{plural(len(epics), "эпика", "эпиков", "эпиков")}: {_names(stale)}.',
                             weight=len(stale) * 5))
    found = _sorted(found)
    return dict(drv, name=name, epics=[e['id'] for e in epics], atRisk=[e['id'] for e in at_risk],
                critical=slowest['id'] if slowest else None, findings=found, headline=found[0] if found else None)


def _ref(level, item):
    return {'level': level, 'id': item['id']} if level == 'epic' else {'level': 'stage', 'name': item['name']}


def project_summary(project_drv, epics, stages, settings, autofill=()):
    """Сводка проекта: главное одной фразой, ключевые цифры, первоочередные риски, хорошие новости, качество данных."""
    active = [e for e in epics if not e['finished']]
    counts = {k: sum(1 for e in epics if e['status'] == k) for k in ('ok', 'risk', 'danger', 'notarget', 'unknown')}
    stage_counts = {k: sum(1 for s in stages if s['status'] == k) for k in ('ok', 'risk', 'danger')}
    worst = max([STATUS_ORDER[s['status']] for s in stages] + [STATUS_ORDER[e['status']] for e in epics] + [-1])
    status = {2: 'danger', 1: 'risk', 0: 'ok'}.get(worst, 'unknown')
    n_e, n_s = len(epics), len(stages)

    def of(k, n, forms):
        return f'{k} из {n} {plural(n, *forms)}'
    st_forms, ep_forms = ('этапа', 'этапов', 'этапов'), ('эпика', 'эпиков', 'эпиков')
    if not n_e:
        headline = 'В проекте нет несданных эпиков.'
    elif status == 'unknown':
        headline = 'Для выводов не хватает данных: нужны замеры остатка и целевые даты.'
    elif status == 'ok':
        headline = (f'Сроки в порядке: все {n_s} {plural(n_s, "этап", "этапа", "этапов")} и {n_e} '
                    f'{plural(n_e, "эпик", "эпика", "эпиков")} с целями успевают.' if n_s > 1
                    else f'Сроки в порядке: {n_e} {plural(n_e, "эпик", "эпика", "эпиков")} с целями '
                         f'{plural(n_e, "успевает", "успевают", "успевают")}.')
    else:
        def group(stage_n, epic_n):
            items = ([of(stage_n, n_s, st_forms)] if stage_n else []) + ([of(epic_n, n_e, ep_forms)] if epic_n else [])
            return ' и '.join(items)
        parts = []
        if stage_counts['danger'] or counts['danger']:
            parts.append('не успевают ' + group(stage_counts['danger'], counts['danger']))
        if stage_counts['risk'] or counts['risk']:
            parts.append('под риском ' + group(stage_counts['risk'], counts['risk']))
        headline = ('Сроки под угрозой: ' if status == 'danger' else 'Есть риски по срокам: ') + '; '.join(parts) + '.'
    facts = []
    rem = project_drv['remaining']
    if rem is not None:
        facts.append({'label': 'Остаток', 'value': fmt_work(rem, settings)})
    rate = project_drv['burnRate']
    if rate is not None:
        facts.append({'label': 'Темп', 'value': fmt_rate(rate, settings)})
        if rate > 0 and rem:
            days = rem / rate
            week = 5 if settings.get('workdays') else 7
            weeks = max(1, math.ceil(days / week))
            facts.append({'label': 'Работы при текущем темпе', 'value': f'≈ {weeks} {plural(weeks, "неделя", "недели", "недель")}'})
    if project_drv['trendPct'] is not None:
        t = fc.js_round(project_drv['trendPct'])
        facts.append({'label': f'Темп за {days_text(settings.get("insightWindowDays") or 28)}',
                      'value': ('+' if t > 0 else '') + f'{t} %'})
    last_stage = _slowest([dict(s, id=s['name']) for s in stages]) if stages else None
    if last_stage:
        when = 'не сгорает' if last_stage['noBurn'] else ('> 3 лет' if last_stage['tooFar'] else fmt_date(last_stage['forecast']))
        facts.append({'label': 'Прогноз завершения', 'value': when, 'hint': f'последним — {last_stage["name"]}'})
    chances = [(s['probability'], s['name']) for s in stages if s['probability'] is not None and s['target']]
    if chances:
        p, name = min(chances)
        facts.append({'label': 'Наименьший шанс успеть', 'value': f'{fc.js_round(p * 100)} %', 'hint': name})
    # первоочередные риски: этапы и эпики, по важности и весу
    pool = []
    for s in stages:
        for x in s['findings']:
            if x['severity'] in ('danger', 'risk') and x['kind'] != 'stale':
                pool.append(dict(x, subject=s['name'], ref=_ref('stage', s)))
    for e in epics:
        for x in e['findings']:
            if x['severity'] in ('danger', 'risk') and x['kind'] not in ('stale', 'quality'):
                pool.append(dict(x, subject=e['name'], ref=_ref('epic', e)))
    pool.sort(key=lambda x: (-SEVERITY_RANK[x['severity']], -x['weight']))
    top, seen = [], set()
    for x in pool:  # один вывод на эпик/этап: самый важный
        key = (x['ref']['level'], x['ref'].get('id', x['ref'].get('name')))
        if key in seen:
            continue
        seen.add(key)
        top.append(x)
        if len(top) == 6:
            break
    wins = [dict(x, subject=e['name'], ref=_ref('epic', e)) for e in epics for x in e['findings']
            if x['kind'] in ('speedup', 'slip') and x['severity'] == 'ok'][:3]
    health = []
    stale = [e for e in active if any(x['kind'] == 'stale' for x in e['findings'])]
    if stale:
        health.append(finding('risk', 'stale', f'Замеры устарели у {len(stale)} {plural(len(stale), "эпика", "эпиков", "эпиков")}: '
                              f'{_names(stale)}.'))
    no_target = [e for e in active if not e['target']]
    if no_target:
        health.append(finding('info', 'notarget', f'Без целевой даты: {_names(no_target)} — для них нет статуса.'))
    no_data = [e for e in active if not e['measurements']]
    if no_data:
        health.append(finding('info', 'nodata', f'Нет замеров: {_names(no_data)}.'))
    problems = sum(e.get('problems') or 0 for e in epics)
    if problems:
        health.append(finding('info', 'quality', f'Задач Jira с неполными данными: {problems}.'))
    for a in autofill:
        if a.get('lastStatus') == 'error':
            health.append(finding('risk', 'autofill', f'Автозаполнение «{a["button"]}» не выполнилось: {a["lastMessage"]}'))
    return {'status': status, 'headline': headline, 'facts': facts, 'top': top, 'wins': wins, 'health': health,
            'counts': counts, 'stageCounts': stage_counts}


def portfolio_summary(projects):
    """Портфель: projects — [{'id', 'name', 'summary'}] → сводка и проекты по убыванию остроты."""
    order = {'danger': 0, 'risk': 1, 'ok': 2, 'unknown': 3}
    items = sorted(projects, key=lambda p: (order.get(p['summary']['status'], 3), p['name']))
    counts = {k: sum(1 for p in projects if p['summary']['status'] == k) for k in ('danger', 'risk', 'ok', 'unknown')}
    n = len(projects)
    if not n:
        headline = 'Нет доступных проектов.'
    elif counts['danger'] or counts['risk']:
        def names(status):
            items = [p for p in items_all if p['summary']['status'] == status]
            return ', '.join(f'«{p["name"]}»' for p in items[:4]) + ('…' if len(items) > 4 else '')
        items_all = items
        parts = []
        if counts['danger']:
            parts.append(f'сроки под угрозой в {counts["danger"]} из {n} {plural(n, "проекта", "проектов", "проектов")}: '
                         f'{names("danger")}')
        if counts['risk']:
            parts.append(f'есть риски в {counts["risk"]}: {names("risk")}')
        headline = '; '.join(parts) + '.'
        headline = headline[0].upper() + headline[1:]
    elif counts['ok']:
        headline = f'Сроки в порядке во всех проектах ({n}).' if not counts['unknown'] \
            else f'Сроки в порядке в {counts["ok"]} из {n} проектов; по остальным не хватает данных.'
    else:
        headline = 'Для выводов по проектам не хватает данных.'
    return {'headline': headline, 'counts': counts, 'projects': items}
