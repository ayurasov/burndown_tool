"""Браузерные (e2e) тесты интерфейса на Playwright.

Требуют установленный Chromium для Playwright (`playwright install chromium`)
или путь к браузеру в переменной окружения CHROMIUM_PATH; без браузера тесты пропускаются.
Chart.js и шрифты подключаются локально (static/vendor), доступ в интернет не нужен.
"""

import os
import re

import pytest

from conftest import ADMIN

sync_api = pytest.importorskip('playwright.sync_api')

VIEWS = ['dashboard', 'stages', 'data', 'services', 'settings', 'users', 'projects', 'audit']


@pytest.fixture(scope='module')
def browser():
    with sync_api.sync_playwright() as p:
        try:
            b = p.chromium.launch(executable_path=os.environ.get('CHROMIUM_PATH') or None)
        except Exception as exc:  # браузер не установлен
            pytest.skip(f'Chromium недоступен: {exc}')
        yield b
        b.close()


class App:
    """Обёртка над страницей приложения с типовыми действиями."""

    def __init__(self, page, base):
        self.page, self.base = page, base
        self.errors = []
        page.on('pageerror', lambda e: self.errors.append(str(e)))

    def login(self, username, password):
        self.page.goto(self.base + '/login')
        self.page.fill('#username', username)
        self.page.fill('#password', password)
        self.page.click('button[type=submit]')
        self.page.wait_for_url(self.base + '/')
        assert self.page.evaluate('typeof Chart !== "undefined"'), 'Chart.js не загрузился'
        self.page.wait_for_selector('#summaryTable tbody tr, #view-empty.active, #view-projects.active')
        return self

    def go(self, view):
        self.page.click(f'.nav-item[data-view="{view}"]')
        self.page.wait_for_selector(f'#view-{view}.active')
        self.page.wait_for_load_state('networkidle')

    def project(self, pid):
        self.page.click(f'.nav-project[data-pid="{pid}"]')
        self.page.wait_for_load_state('networkidle')

    def text(self, selector):
        return re.sub(r'\s+', ' ', self.page.text_content(selector)).strip()

    def cells(self, selector):
        return [re.sub(r'\s+', ' ', t).strip() for t in self.page.locator(selector).all_text_contents()]

    def expect_toast(self, text):
        """Ждёт уведомление с заданным текстом (предыдущее может ещё висеть на экране)."""
        self.page.wait_for_function(
            'text => { const t = document.getElementById("toast"); return t.classList.contains("show") && t.textContent === text; }',
            arg=text)


@pytest.fixture
def ui(browser, live_server):
    ctx = browser.new_context(viewport={'width': 1366, 'height': 900})
    app = App(ctx.new_page(), live_server)
    yield app
    ctx.close()
    assert app.errors == [], f'Ошибки JS на странице: {app.errors}'


@pytest.fixture
def admin_ui(ui):
    return ui.login(*ADMIN)


# ─── Общее ───────────────────────────────────────────────────────

def test_login_error_message(ui):
    ui.page.goto(ui.base + '/login')
    ui.page.fill('#username', 'admin')
    ui.page.fill('#password', 'wrong')
    ui.page.click('button[type=submit]')
    ui.page.wait_for_selector('#loginError:has-text("Неверный логин или пароль")')
    assert ui.page.is_enabled('.login-btn')


@pytest.mark.parametrize('theme', ['light', 'dark'])
def test_all_views_render_without_errors(admin_ui, theme):
    admin_ui.page.evaluate(f"localStorage.setItem('burndown-theme', '{theme}')")
    admin_ui.page.reload()
    admin_ui.page.wait_for_selector('#summaryTable tbody tr')
    assert admin_ui.page.get_attribute('html', 'data-theme') == theme
    for view in VIEWS:
        admin_ui.go(view)
        assert admin_ui.page.is_visible(f'#view-{view}')


def test_mobile_has_no_horizontal_scroll(browser, live_server):
    ctx = browser.new_context(viewport={'width': 390, 'height': 844})
    app = App(ctx.new_page(), live_server).login(*ADMIN)
    for view in VIEWS:
        app.page.click('#sidebarToggle')
        app.go(view)
        overflow = app.page.evaluate('document.documentElement.scrollWidth - document.documentElement.clientWidth')
        assert overflow <= 0, f'горизонтальная прокрутка в разделе {view}: {overflow}px'
    assert app.errors == []
    ctx.close()


def test_modal_closes_by_escape_and_cancel(admin_ui):
    page = admin_ui.page
    admin_ui.go('projects')
    page.click('#btnAddProject')
    assert page.is_visible('#confirmModal.show')
    page.keyboard.press('Escape')
    page.wait_for_selector('#confirmModal:not(.show)', state='attached')
    page.click('#btnAddProject')
    page.click('#confirmCancel')
    page.wait_for_selector('#confirmModal:not(.show)', state='attached')
    # пустое название — окно остаётся открытым
    page.click('#btnAddProject')
    page.click('#confirmOk')
    admin_ui.expect_toast('Введите название проекта')
    assert page.is_visible('#confirmModal.show')


# ─── Дашборд ─────────────────────────────────────────────────────

def test_dashboard_kpis_and_summary(admin_ui):
    kpis = admin_ui.cells('#kpiGrid .kpi-value')
    assert kpis == ['6', '3', '0', '3', '1 234,34 ч']
    assert admin_ui.cells('#summaryTable tbody tr.total-row td')[:3] == ['Итого', '1 234,34 ч', '57,8 ч']
    assert '31.08.2026' in admin_ui.text('#summaryTable tbody tr:first-child')


def test_summary_numeric_sort(admin_ui):
    th = '#summaryTable th[data-sort="remaining"]'
    admin_ui.page.click(th)
    asc = admin_ui.cells('#summaryTable tbody tr:not(.total-row) td:nth-child(3)')
    assert asc == ['86 ч', '109 ч', '201 ч', '214,34 ч', '278 ч', '346 ч']
    admin_ui.page.click(th)
    assert admin_ui.page.get_attribute(th, 'class').endswith('sorted-desc')
    desc = admin_ui.cells('#summaryTable tbody tr:not(.total-row) td:nth-child(3)')
    assert desc == list(reversed(asc))


def test_epic_filter_and_hide_done(admin_ui):
    page = admin_ui.page
    page.click('#dashEpicNone')
    assert admin_ui.cells('#kpiGrid .kpi-value')[0] == '0'
    page.click('#dashEpicAll')
    page.uncheck('#dashEpicChecks input[data-sid="1"]')
    assert admin_ui.cells('#kpiGrid .kpi-value')[0] == '5'
    assert admin_ui.cells('#kpiGrid .kpi-value')[4] == '888,34 ч'
    page.click('#dashEpicAll')

    # эпик «сдан» → скрывается фильтром «Скрыть сданные»
    admin_ui.go('services')
    page.check('#servicesTable tr[data-id="1"] input[data-field="isDone"]')
    admin_ui.expect_toast('Сохранено')
    admin_ui.go('dashboard')
    page.check('#dashHideDone')
    assert admin_ui.cells('#kpiGrid .kpi-value')[0] == '5'
    assert 'Работа с жалобами' not in admin_ui.text('#summaryTable')


def test_burn_rate_table_and_range(admin_ui):
    totals = admin_ui.cells('#burnRateTable tr.total-row td')
    assert totals == ['Итого по проекту', '—', '117,3', '-88,99', '228,42']
    admin_ui.page.fill('#burnFrom', '2026-07-24')
    admin_ui.page.fill('#burnTo', '2026-07-28')
    assert admin_ui.cells('#burnRateTable thead th') == ['Эпик', '24.07', '28.07']
    admin_ui.page.click('#burnResetRange')
    assert len(admin_ui.cells('#burnRateTable thead th')) == 5


def test_epic_target_date_change_updates_status(admin_ui):
    page = admin_ui.page
    page.select_option('#chartServiceSelect', '1')
    page.fill('#epicTargetDate', '2026-08-01')
    admin_ui.expect_toast('Целевая дата обновлена')
    row = admin_ui.text('#summaryTable tbody tr:first-child')
    assert '01.08.2026' in row and 'Не успеваем' in row


def test_pdf_button_passes_dashboard_filter(admin_ui):
    page = admin_ui.page
    page.uncheck('#dashEpicChecks input[data-sid="2"]')
    with page.context.expect_event('request', lambda r: '/pdf?' in r.url) as req:
        page.click('#btnPdf')
    url = req.value.url
    assert '/api/projects/1/pdf?' in url and 'page=dashboard' in url and 'epics=1%2C3%2C4%2C5%2C6' in url


# ─── Данные по датам ─────────────────────────────────────────────

def test_empty_new_date_keeps_last_remaining(admin_ui):
    admin_ui.go('data')
    admin_ui.page.click('#btnAddRow')
    admin_ui.expect_toast('Добавлена новая дата')
    assert admin_ui.page.locator('#entriesTable tbody tr').count() == 5
    admin_ui.go('dashboard')
    row = admin_ui.cells('#summaryTable tbody tr:first-child td')
    assert row[2] == '346 ч' and row[6] != 'Нет данных'
    assert admin_ui.cells('#kpiGrid .kpi-value')[4] == '1 234,34 ч'


def test_duplicate_entry_date_rejected(admin_ui):
    page = admin_ui.page
    admin_ui.go('data')
    first_date = '#entriesTable tbody tr:first-child input[data-field="date"]'
    page.fill(first_date, '2026-07-29')
    admin_ui.expect_toast('Замер на эту дату уже есть')
    assert page.input_value(first_date) == '2026-07-17'


def test_entry_edit_saves_and_recalculates(admin_ui, client):
    page = admin_ui.page
    admin_ui.go('data')
    cell = '#entriesTable tbody tr:last-child input[data-field="remaining"]'
    page.fill(cell, '300')
    page.dispatch_event(cell, 'change')
    admin_ui.expect_toast('Сохранено')
    admin_ui.go('dashboard')
    assert admin_ui.cells('#summaryTable tbody tr:first-child td')[2] == '300 ч'
    client.post('/api/auth/login', json={'username': 'admin', 'password': 'Test198!'})
    entry = next(e for e in client.get('/api/projects/1/data').get_json()['entries'] if e['id'] == 19)
    assert entry['remaining'] == 300


def test_delete_entry_with_confirmation(admin_ui):
    page = admin_ui.page
    admin_ui.go('data')
    page.click('#entriesTable tbody tr:last-child [data-action="delete"]')
    assert admin_ui.text('#confirmTitle') == 'Удалить запись?'
    assert 'btn-danger' in page.get_attribute('#confirmOk', 'class')
    page.click('#confirmOk')
    admin_ui.expect_toast('Строка удалена')
    assert page.locator('#entriesTable tbody tr').count() == 3


# ─── Эпики и свод по этапам ──────────────────────────────────────

def test_add_epic(admin_ui):
    admin_ui.go('services')
    admin_ui.page.click('#btnAddService')
    admin_ui.expect_toast('Эпик добавлен')
    last = '#servicesTable tbody tr:last-child'
    assert admin_ui.page.input_value(f'{last} input[data-field="name"]') == 'Новый эпик'
    assert admin_ui.page.input_value(f'{last} input[data-field="etap"]') == 'Этап 3.1'
    assert admin_ui.text('#navServiceCount') == '7'


def test_stage_burn_rate_matches_dashboard_total(admin_ui):
    dashboard_total = admin_ui.cells('#burnRateTable tr.total-row td')
    admin_ui.go('stages')
    assert admin_ui.cells('#stageBurnRateTable tr.total-row td') == dashboard_total
    assert admin_ui.cells('#stageSummaryTable tbody td:first-child') == ['Этап 4', 'Этап 3']
    assert admin_ui.page.locator('#stageChartsContainer canvas').count() == 2


def test_stage_show_done_toggle(admin_ui):
    page = admin_ui.page
    admin_ui.go('services')
    page.check('#servicesTable tr[data-id="1"] input[data-field="isDone"]')
    page.check('#servicesTable tr[data-id="2"] input[data-field="isDone"]')
    page.wait_for_load_state('networkidle')
    admin_ui.go('stages')
    assert admin_ui.cells('#stageSummaryTable tbody td:first-child') == ['Этап 3']
    page.check('#stageShowDone')
    assert admin_ui.cells('#stageSummaryTable tbody td:first-child') == ['Этап 4', 'Этап 3']


# ─── Права доступа ───────────────────────────────────────────────

def test_viewer_readonly_mode(ui, make_user):
    make_user('viewer', project_id=1, project_role='user')
    ui.login('viewer', 'pw-12345')
    page = ui.page
    assert page.is_visible('#readonlyBanner')
    assert not page.is_visible('#navSettings') and not page.is_visible('#adminNav')
    assert page.is_disabled('#epicTargetDate')
    assert page.is_enabled('#burnFrom') and page.is_enabled('#dashHideDone')
    ui.go('stages')
    assert page.is_enabled('#stageBurnFrom') and page.is_enabled('#stageShowDone')
    assert page.is_disabled('#stageChartsContainer input[type=date]')
    ui.go('data')
    assert not page.is_visible('#btnAddRow')
    assert page.is_enabled('#dataServiceSelect')
    assert page.get_attribute('#entriesTable input[data-field="remaining"] >> nth=0', 'readonly') is not None
    ui.go('services')
    assert not page.is_visible('#btnAddService')
    assert page.is_disabled('#servicesTable input[type=checkbox] >> nth=0')


def test_readonly_is_lifted_after_switching_project(ui, make_user, admin):
    viewer = make_user('mixed', project_id=1, project_role='user')
    admin.post(f'/api/users/{viewer.user_id}/projects', json={'projectId': 2, 'role': 'manager'})
    admin.post('/api/projects/2/services', json={'etap': 'Этап 1', 'name': 'Эпик ИСУП', 'targetDate': '2026-12-01'})
    ui.login('mixed', 'pw-12345')
    page = ui.page
    ui.go('services')
    assert not page.is_visible('#btnAddService')
    ui.project(2)
    assert page.is_visible('#view-services.active'), 'раздел должен сохраниться при смене проекта'
    assert not page.is_visible('#readonlyBanner')
    assert page.is_visible('#btnAddService')
    assert page.get_attribute('#servicesTable input[data-field="name"]', 'readonly') is None
    ui.go('dashboard')
    assert page.is_enabled('#epicTargetDate')


def test_user_without_projects_sees_empty_state(ui, make_user):
    make_user('lonely')
    ui.login('lonely', 'pw-12345')
    assert ui.page.is_visible('#view-empty.active')
    assert not ui.page.is_visible('#sectionsNav')


# ─── Администрирование и профиль ─────────────────────────────────

def test_add_user_duplicate_shows_server_error(admin_ui):
    page = admin_ui.page
    admin_ui.go('users')
    page.fill('#newUsername', 'admin')
    page.fill('#newPassword', 'x')
    page.click('#addUserForm button[type=submit]')
    admin_ui.expect_toast('Пользователь с таким логином уже существует')
    page.fill('#newUsername', 'ivan')
    page.fill('#newPassword', 'secret')
    page.click('#addUserForm button[type=submit]')
    admin_ui.expect_toast('Пользователь добавлен')
    assert 'ivan' in admin_ui.cells('#usersTable tbody td:first-child')
    # новый пользователь сразу выбран для настройки доступа
    assert page.is_visible('#userProjectsMgmt [data-toggle="1"]')


def test_edit_user_with_quotes_in_name(admin_ui, admin):
    admin.post('/api/users', json={'username': 'ivan', 'password': 'p', 'displayName': 'Иван "Тест"'})
    page = admin_ui.page
    admin_ui.go('users')
    page.click('#usersTable tr:has-text("ivan") [data-action="edit"]')
    assert page.input_value('#editDisplayName') == 'Иван "Тест"'
    page.fill('#editPosition', 'Аналитик')
    page.click('#confirmOk')
    admin_ui.expect_toast('Пользователь обновлён')
    page.wait_for_selector('#usersTable td:has-text("Аналитик")')


def test_user_search(admin_ui, admin):
    admin.post('/api/users', json={'username': 'ivan', 'password': 'p'})
    admin_ui.go('users')
    admin_ui.page.fill('#userSearch', 'iva')
    assert admin_ui.cells('#usersTable tbody td:first-child') == ['ivan']
    admin_ui.page.fill('#userSearch', 'нет-такого')
    assert admin_ui.text('#usersTable tbody') == 'Ничего не найдено'


def test_project_assignment_ui(admin_ui, admin):
    admin.post('/api/users', json={'username': 'ivan', 'password': 'p'})
    page = admin_ui.page
    admin_ui.go('users')
    page.click('#usersTable tr:has-text("ivan") td:first-child')
    page.click('#userProjectsMgmt [data-toggle="1"]')
    admin_ui.expect_toast('Доступ добавлен')
    page.wait_for_selector('#userProjectsMgmt select[data-pid="1"]:not([disabled])')
    page.select_option('#userProjectsMgmt select[data-pid="1"]', 'manager')
    page.wait_for_load_state('networkidle')
    uid = next(u['id'] for u in admin.get('/api/users').get_json() if u['username'] == 'ivan')
    assert admin.get(f'/api/users/{uid}/projects').get_json()[0]['role'] == 'manager'


def test_profile_photo_upload_and_delete(admin_ui, admin, tmp_path):
    png = tmp_path / 'a.png'
    png.write_bytes(bytes.fromhex(
        '89504e470d0a1a0a0000000d4948445200000001000000010806000000'
        '1f15c4890000000d49444154789c6360f8cf000000030101005b2a6b2b0000000049454e44ae426082'))
    page = admin_ui.page
    page.click('#userInfo')
    page.set_input_files('#profilePhotoFile', str(png))
    page.wait_for_selector('#profileAvatarBox img')
    page.click('#confirmOk')
    admin_ui.expect_toast('Профиль обновлён')
    assert admin.get('/api/auth/me').get_json()['user']['photo'].startswith('data:image/png')
    assert page.is_visible('#userInfo img.avatar')

    page.click('#userInfo')
    page.click('#profileDeletePhoto')
    page.click('#confirmOk')
    admin_ui.expect_toast('Профиль обновлён')
    assert admin.get('/api/auth/me').get_json()['user']['photo'] == ''
    assert not page.is_visible('#userInfo img')


def test_create_and_rename_project(admin_ui):
    page = admin_ui.page
    admin_ui.go('projects')
    page.click('#btnAddProject')
    page.fill('#projectName', 'Проект "Альфа"')
    page.click('#confirmOk')
    admin_ui.expect_toast('Проект добавлен')
    page.wait_for_selector('#projectsNav .nav-project:has-text("Альфа")')
    assert page.is_visible('#view-projects.active'), 'после создания проекта остаёмся в разделе'
    page.click('#projectsMgmtTable tr:has-text("Альфа") [data-action="edit"]')
    assert page.input_value('#projectName') == 'Проект "Альфа"'
    page.fill('#projectName', 'Бета')
    page.click('#confirmOk')
    page.wait_for_selector('#projectsNav .nav-project:has-text("Бета")')


def test_audit_log_shows_changes(admin_ui):
    page = admin_ui.page
    admin_ui.go('services')
    name = '#servicesTable tr[data-id="3"] input[data-field="name"]'
    page.fill(name, 'КСБО-2')
    page.dispatch_event(name, 'change')
    admin_ui.expect_toast('Сохранено')
    admin_ui.go('audit')
    row = admin_ui.text('#auditLogTable tbody tr:first-child')
    assert 'Изменение' in row and 'КСБО-2' in row and 'Эпики' in row
    assert re.match(r'\d{2}\.\d{2}\.\d{4} \d{2}:\d{2}', admin_ui.cells('#auditLogTable tbody tr:first-child td')[0])
    page.select_option('#auditFilterPage', 'services')
    page.wait_for_load_state('networkidle')
    assert set(admin_ui.cells('#auditLogTable tbody td:nth-child(4)')) == {'Эпики'}


# ─── Статусы, рост оценки, burn rate ─────────────────────────────

def test_status_without_target_and_non_burning_epic(admin_ui, admin):
    admin.put('/api/services/3', json={'targetDate': ''})
    admin_ui.page.reload()
    admin_ui.page.wait_for_selector('#summaryTable tbody tr')
    ksbo = admin_ui.cells('#summaryTable tbody tr:has-text("КСБО") td')
    assert ksbo[-1] == 'Нет цели'
    # остаток «Планирования закупок» растёт (55 → 86) — прогноз не достигает нуля
    plan = admin_ui.cells('#summaryTable tbody tr:has-text("Планирование закупок") td')
    assert plan[-2] == 'не сгорает' and plan[-1] == '✗ Не успеваем'


def test_scope_change_and_negative_burn_highlight(admin_ui):
    row = '#summaryTable tbody tr:has-text("Работа с жалобами")'
    scope = admin_ui.page.locator(f'{row} td:nth-child(5)')
    assert scope.text_content() == '+251 ч'  # оценка 150 → 401
    assert 'scope-up' in scope.get_attribute('class')
    neg = admin_ui.page.locator('#burnRateTable td.neg')
    assert neg.count() > 0 and neg.first.get_attribute('title') == 'Остаток вырос'
    assert admin_ui.page.locator('#burnRateTable tbody tr:first-child td:nth-child(4)').text_content() == '-56,25'


def test_today_line_plugin_registered(admin_ui):
    assert admin_ui.page.evaluate("!!Chart.registry.plugins.get('todayLine')")


def test_stage_sum_carries_forward_last_values(admin_ui, admin):
    """Замер только по одному эпику этапа не «проваливает» сумму: второй эпик берётся по последнему значению."""
    admin.post('/api/services/1/entries', json={'date': '2026-07-30', 'remaining': 300})
    admin_ui.page.reload()
    admin_ui.page.wait_for_selector('#summaryTable tbody tr')
    admin_ui.go('stages')
    stage4 = admin_ui.cells('#stageSummaryTable tbody tr:has-text("Этап 4") td')
    assert stage4[2] == '514,34 ч'  # 300 + 214,34 (последний остаток КЦД), а не 300


# ─── Данные: новый замер, клавиатура, откат ──────────────────────

def test_new_row_defaults_to_today(admin_ui):
    import datetime
    admin_ui.go('data')
    admin_ui.page.click('#btnAddRow')
    admin_ui.expect_toast('Добавлена новая дата')
    dates = [admin_ui.page.input_value(f'#entriesTable tbody tr:nth-child({i}) input[data-field="date"]') for i in range(1, 6)]
    assert dates[-1] == datetime.date.today().isoformat()


def test_enter_moves_to_next_row(admin_ui):
    page = admin_ui.page
    admin_ui.go('data')
    page.focus('#entriesTable tbody tr:nth-child(1) input[data-field="spent"]')
    page.keyboard.press('Enter')
    assert page.evaluate("document.activeElement.closest('tr').rowIndex") == 2
    assert page.evaluate("document.activeElement.dataset.field") == 'spent'
    page.keyboard.press('Shift+Enter')
    assert page.evaluate("document.activeElement.closest('tr').rowIndex") == 1


def test_failed_save_rolls_back(admin_ui):
    page = admin_ui.page
    admin_ui.go('data')
    page.route('**/api/entries/*', lambda route: route.fulfill(status=500, content_type='application/json',
                                                               body='{"error": "База недоступна"}'))
    cell = '#entriesTable tbody tr:last-child input[data-field="remaining"]'
    page.fill(cell, '1')
    page.dispatch_event(cell, 'change')
    admin_ui.expect_toast('База недоступна')
    page.wait_for_function(f"document.querySelector('{cell}').value === '346'")
    page.unroute('**/api/entries/*')
    admin_ui.go('dashboard')
    assert admin_ui.cells('#summaryTable tbody tr:first-child td')[2] == '346 ч'


def test_negative_value_rejected_in_table(admin_ui):
    page = admin_ui.page
    admin_ui.go('data')
    cell = '#entriesTable tbody tr:last-child input[data-field="remaining"]'
    page.fill(cell, '-5')
    page.dispatch_event(cell, 'change')
    admin_ui.expect_toast('Введите число не меньше 0')
    assert page.input_value(cell) == '346'


# ─── Замер по всем эпикам ────────────────────────────────────────

def test_bulk_measurement_modal(admin_ui, admin):
    page = admin_ui.page
    admin_ui.go('data')
    page.click('#btnBulkMeasure')
    assert admin_ui.text('#confirmTitle') == 'Замер по всем эпикам'
    assert page.locator('#bulkTable tbody tr').count() == 6
    # на существующую дату подставляются сохранённые значения
    page.fill('#bulkDate', '2026-07-29')
    assert page.input_value('#bulkTable tr[data-sid="1"] input[data-field="remaining"]') == '346'
    # на новую дату — пустые поля с подсказкой прошлого замера
    page.fill('#bulkDate', '2026-07-30')
    first = '#bulkTable tr[data-sid="1"] input[data-field="estimate"]'
    assert page.input_value(first) == '' and page.get_attribute(first, 'placeholder') == '401'
    page.click('#bulkFillPrev')
    assert page.input_value(first) == '401'
    page.fill('#bulkTable tr[data-sid="1"] input[data-field="remaining"]', '320')
    page.fill('#bulkTable tr[data-sid="2"] input[data-field="remaining"]', '200')
    page.click('#confirmOk')
    admin_ui.expect_toast('Сохранено замеров: 6')  # остальные строки заполнены прошлыми значениями
    page.wait_for_selector('#confirmModal:not(.show)', state='attached')
    entries = [e for e in admin.get('/api/projects/1/data').get_json()['entries'] if e['date'] == '2026-07-30']
    assert {e['service_id']: e['remaining'] for e in entries}[1] == 320
    admin_ui.go('dashboard')
    assert admin_ui.cells('#kpiGrid .kpi-value')[4] == '1 194 ч'  # 320 + 200 + 109 + 201 + 278 + 86


def test_bulk_measurement_requires_values(admin_ui):
    admin_ui.go('data')
    admin_ui.page.click('#btnBulkMeasure')
    admin_ui.page.click('#confirmOk')
    admin_ui.expect_toast('Заполните хотя бы одно значение')
    assert admin_ui.page.is_visible('#confirmModal.show')


def test_bulk_measurement_hidden_for_viewer(ui, make_user):
    make_user('viewer', project_id=1)
    ui.login('viewer', 'pw-12345')
    ui.go('data')
    assert not ui.page.is_visible('#btnBulkMeasure')


# ─── Дашборд: сворачиваемый блок, печать, офлайн ─────────────────

def test_all_epics_block_collapse_is_remembered(admin_ui):
    page = admin_ui.page
    assert page.evaluate("!!Chart.getChart('chartRemainingAll')")
    page.click('#allEpicsBlock > summary')
    assert not page.is_visible('#chartRemainingAll')
    page.reload()
    page.wait_for_selector('#summaryTable tbody tr')
    assert page.evaluate("document.getElementById('allEpicsBlock').open") is False
    assert page.evaluate("!Chart.getChart('chartRemainingAll')")
    page.click('#allEpicsBlock > summary')
    page.wait_for_function("!!Chart.getChart('chartRemainingAll')")


def test_print_layout_hides_navigation(admin_ui):
    page = admin_ui.page
    assert page.is_visible('#btnPrint')
    page.emulate_media(media='print')
    assert not page.is_visible('.sidebar') and not page.is_visible('#btnPdf')
    assert page.is_visible('#kpiGrid') and page.is_visible('#chartRemaining')
    page.emulate_media(media='screen')
    admin_ui.go('data')
    assert not page.is_visible('#btnPrint')


def test_no_external_requests(browser, live_server):
    ctx = browser.new_context()
    page = ctx.new_page()
    external = []
    page.on('request', lambda r: None if r.url.startswith(live_server) or r.url.startswith('data:') else external.append(r.url))
    app = App(page, live_server).login(*ADMIN)
    app.go('stages')
    assert external == [], f'Внешние запросы: {external}'
    ctx.close()


# ─── Реестр: «Показать ещё» ──────────────────────────────────────

def test_audit_load_more(admin_ui, admin):
    import app as app_module
    for i in range(app_module.AUDIT_PAGE_SIZE + 5):
        admin.put('/api/services/1', json={'name': f'Имя {i}'})
    page = admin_ui.page
    admin_ui.go('audit')
    assert page.locator('#auditLogTable tbody tr').count() == app_module.AUDIT_PAGE_SIZE
    assert admin_ui.text('#auditCount') == f'Показано записей: {app_module.AUDIT_PAGE_SIZE}'
    page.click('#auditLoadMore')
    page.wait_for_function(f"document.querySelectorAll('#auditLogTable tbody tr').length === {app_module.AUDIT_PAGE_SIZE + 5}")
    assert admin_ui.text('#auditCount') == f'Показано записей: {app_module.AUDIT_PAGE_SIZE + 5} (все)'
    assert not page.is_visible('#auditLoadMore')
