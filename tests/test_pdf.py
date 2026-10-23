"""Тесты PDF-выгрузки: корректные итоги, burn rate, фильтры интерфейса и вспомогательные функции."""

import io

import pytest
from pypdf import PdfReader

import app as app_module


def pdf_text(client, **params):
    r = client.get('/api/projects/1/pdf', query_string=params)
    assert r.status_code == 200, r.get_data(as_text=True)
    assert r.mimetype == 'application/pdf'
    assert 'attachment' in r.headers['Content-Disposition']
    reader = PdfReader(io.BytesIO(r.data))
    return '\n'.join(page.extract_text() for page in reader.pages)


def test_dashboard_totals_use_last_measurement(admin):
    text = pdf_text(admin, page='dashboard')
    # остаток = сумма последних замеров эпиков (346 + 214.34 + 109 + 201 + 278 + 86),
    # а не сумма всех замеров за всю историю
    assert '1234.3' in text
    assert '5790.8' in text  # затрачено: 258 + 259 + 594 + 2000.75 + 2311 + 368
    assert 'Burn rate по эпикам, ч/день' in text


def test_dashboard_burn_rate_values(admin):
    text = pdf_text(admin, page='dashboard')
    # итог дневного burn rate за 29.07 (как в таблице на дашборде) — 228.42
    assert '228.4' in text
    # дата 17.07 — первая, burn rate для неё не определён
    assert '17.07' in text


def test_dashboard_respects_selected_epics(admin):
    text = pdf_text(admin, page='dashboard', epics='3,6')
    assert 'КСБО' in text and 'Планирование закупок' in text
    assert 'ЦЗК' not in text
    assert '195.0' in text  # 109 + 86


def test_stages_exclude_done_epics_by_default(admin):
    admin.put('/api/services/1', json={'isDone': True})
    text = pdf_text(admin, page='stages')
    assert 'Работа с жалобами' not in text
    text_all = pdf_text(admin, page='stages', showDone='1')
    assert 'Работа с жалобами' in text_all


def test_stages_use_stage_group(admin):
    admin.put('/api/services/3', json={'stageGroup': 'Особая группа'})
    text = pdf_text(admin, page='stages')
    assert 'Особая группа — детали по эпикам' in text


def test_data_page_lists_dates(admin):
    text = pdf_text(admin, page='data')
    assert '17.07.2026' in text and '29.07.2026' in text


def test_unknown_page_rejected(admin):
    assert admin.get('/api/projects/1/pdf?page=zzz').status_code == 400


def test_pdf_requires_project_access(make_user):
    c = make_user('outsider')
    assert c.get('/api/projects/1/pdf?page=dashboard').status_code == 403


def test_pdf_for_viewer(make_user):
    c = make_user('viewer', project_id=1)
    assert c.get('/api/projects/1/pdf?page=data').status_code == 200


def test_pdf_empty_project(admin):
    r = admin.get('/api/projects/2/pdf?page=dashboard')
    assert r.status_code == 200 and r.data.startswith(b'%PDF')


# ─── Вспомогательные функции ─────────────────────────────────────

@pytest.mark.parametrize('value, expected', [(None, '—'), (3, '3.0'), (214.345, '214.3'), ('abc', 'abc')])
def test_fmt_num(value, expected):
    assert app_module._fmt_num(value) == expected


@pytest.mark.parametrize('value, expected', [(None, '—'), ('', '—'), ('2026-08-31', '31.08.2026'), ('bad', 'bad')])
def test_fmt_date(value, expected):
    assert app_module._fmt_date(value) == expected


def test_daily_rates_skip_empty_measurements():
    entries = [
        {'date': '2026-07-01', 'remaining': 100},
        {'date': '2026-07-03', 'remaining': None},   # пустой замер не разрывает расчёт
        {'date': '2026-07-05', 'remaining': 60},
        {'date': '2026-07-06', 'remaining': 70},
    ]
    rates = app_module._daily_rates(entries, ['2026-07-01', '2026-07-03', '2026-07-05', '2026-07-06', '2026-07-09'])
    assert rates == [None, None, 10.0, -10.0, None]


@pytest.mark.parametrize('stage_group, etap, expected', [
    ('', 'Этап 3.2', 'Этап 3'),
    (None, 'Этап 4', 'Этап 4'),
    ('  Группа  ', 'Этап 3.2', 'Группа'),
    ('', 'Пилот', 'Пилот'),
])
def test_stage_key(stage_group, etap, expected):
    assert app_module.stage_key({'stage_group': stage_group, 'etap': etap}) == expected


def test_stage_key_empty_etap():
    assert app_module.stage_key({'stage_group': '', 'etap': ''}) == 'Без этапа'
    assert app_module.stage_key({'stage_group': None, 'etap': '  Этап 2.1 '}) == 'Этап 2'
