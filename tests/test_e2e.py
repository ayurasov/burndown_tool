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
# Разделы, которые при открытии загружают данные с сервера: ждём ответа, а не «тишины в сети»
VIEW_REQUESTS = {
    'users': ('/api/users', '#usersTable tbody tr'),
    'projects': ('/api/projects', '#projectsMgmtTable tbody tr'),
    'audit': ('/api/audit', '#auditLogTable tbody tr'),
}

# Счётчик показов уведомлений: позволяет отличить новое уведомление от ещё не скрытого старого
TOAST_COUNTER_SCRIPT = """
document.addEventListener('DOMContentLoaded', function(){
  window.__toastCount = 0;
  var el = document.getElementById('toast');
  if(el) new MutationObserver(function(){ window.__toastCount++; }).observe(el, {childList: true});
});
"""


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
        self.toasts_seen = 0
        page.add_init_script(TOAST_COUNTER_SCRIPT)
        page.on('pageerror', lambda e: self.errors.append(str(e)))
        page.on('load', lambda _: setattr(self, 'toasts_seen', 0))

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
        if view in VIEW_REQUESTS:
            url, rows = VIEW_REQUESTS[view]
            with self.page.expect_response(lambda r: url in r.url):
                self.page.click(f'.nav-item[data-view="{view}"]')
            self.page.wait_for_selector(rows)  # таблица отрисована по ответу сервера
        else:
            self.page.click(f'.nav-item[data-view="{view}"]')
        self.page.wait_for_selector(f'#view-{view}.active')

    def project(self, pid):
        name = self.text(f'.nav-project[data-pid="{pid}"]')
        self.page.click(f'.nav-project[data-pid="{pid}"]')
        # подзаголовок раздела обновляется после загрузки данных проекта
        self.page.wait_for_function('n => document.getElementById("pageSub").textContent.endsWith(" · " + n)', arg=name)

    def text(self, selector):
        return re.sub(r'\s+', ' ', self.page.text_content(selector)).strip()

    def cells(self, selector):
        return [re.sub(r'\s+', ' ', t).strip() for t in self.page.locator(selector).all_text_contents()]

    def expect_toast(self, text):
        """Ждёт новое уведомление с заданным текстом (предыдущее может ещё висеть на экране)."""
        self.page.wait_for_function(
            '([text, seen]) => window.__toastCount > seen && document.getElementById("toast").textContent === text',
            arg=[text, self.toasts_seen])
        self.toasts_seen = self.page.evaluate('window.__toastCount')


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
    for view in VIEWS + ['jira', 'autofill', 'notify', 'integrations']:  # длинные подписи флажков переносятся
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


def test_modal_keyboard_focus_and_enter(admin_ui):
    """Окно: страница под ним недоступна, Tab не уходит из окна, Enter в поле — «Создать», фокус возвращается."""
    page = admin_ui.page
    admin_ui.go('projects')
    assert not page.evaluate("getComputedStyle(document.getElementById('confirmModal')).visibility === 'visible'")
    page.focus('#btnAddProject')
    page.keyboard.press('Enter')
    page.wait_for_selector('#confirmModal.show')
    assert page.evaluate("document.getElementById('appRoot').inert")
    for _ in range(6):
        page.keyboard.press('Tab')
        assert page.evaluate("document.getElementById('confirmModal').contains(document.activeElement)")
    page.fill('#projectName', 'Проект по Enter')
    page.press('#projectName', 'Enter')
    page.wait_for_selector('#confirmModal:not(.show)', state='attached')
    page.wait_for_selector('#projectsMgmtTable:has-text("Проект по Enter")')
    assert not page.evaluate("document.getElementById('appRoot').inert")
    assert page.evaluate('document.activeElement.id') == 'btnAddProject'


def test_delete_confirmation_focuses_cancel(admin_ui):
    page = admin_ui.page
    admin_ui.go('data')
    page.click('#entriesTable [data-action="delete"] >> nth=0')
    page.wait_for_selector('#confirmModal.show')
    assert page.evaluate('document.activeElement.id') == 'confirmCancel'
    page.keyboard.press('Enter')  # Enter на «Отмена» не удаляет
    page.wait_for_selector('#confirmModal:not(.show)', state='attached')
    assert page.locator('#entriesTable tbody tr').count() == 4


def test_mobile_menu_keyboard(browser, live_server):
    ctx = browser.new_context(viewport={'width': 390, 'height': 844})
    app = App(ctx.new_page(), live_server).login(*ADMIN)
    page = app.page
    assert page.evaluate("document.querySelector('.sidebar').inert")  # закрытое меню не в обходе Tab
    page.click('#sidebarToggle')
    assert not page.evaluate("document.querySelector('.sidebar').inert")
    assert page.evaluate("document.activeElement.classList.contains('nav-item')")
    page.keyboard.press('Escape')
    assert page.evaluate("document.querySelector('.sidebar').inert")
    assert page.evaluate('document.activeElement.id') == 'sidebarToggle'
    assert app.errors == []
    ctx.close()


def test_default_password_warning(admin_ui):
    page = admin_ui.page
    assert page.is_visible('#defaultPwWarning')
    page.click('#defaultPwWarning')
    page.wait_for_selector('#confirmModal.show')
    assert page.evaluate('document.activeElement.id') == 'profilePassword'
    page.fill('#profilePassword', 'Strong-pass-2026')
    page.click('#confirmOk')
    admin_ui.expect_toast('Профиль обновлён')
    assert not page.is_visible('#defaultPwWarning')


def test_empty_charts_explain_why(admin_ui):
    page = admin_ui.page
    page.click('text=Снять все')
    page.wait_for_selector('#chartRemaining[hidden]', state='attached')
    assert 'Не выбран ни один эпик' in admin_ui.text('#chartRemaining + .chart-empty')
    page.click('text=Все')
    page.wait_for_selector('#chartRemaining:not([hidden])', state='attached')
    assert page.locator('.chart-wrap .chart-empty').count() == 0


def test_about_version_history_and_manual(admin_ui):
    import app as app_module
    page = admin_ui.page
    version = app_module.APP_VERSION
    assert admin_ui.text('#btnVersions') == f'Burndown — версия {version}'
    page.click('#btnVersions')
    page.wait_for_selector('#confirmModal.show .versions-list')
    assert admin_ui.text('#confirmTitle') == 'История версий'
    latest = page.locator('.versions-list details').first
    assert latest.get_attribute('open') is not None and f'Версия {version}' in latest.text_content()
    assert page.locator('.versions-list details[open]').count() == 1  # остальные свёрнуты
    page.click('.versions-list details >> nth=1 >> summary')
    assert page.locator('.versions-list details[open]').count() == 2
    page.keyboard.press('Escape')
    page.click('#btnAbout')
    page.wait_for_selector('#confirmModal.show .about')
    assert version in admin_ui.text('.about-facts')
    links = page.eval_on_selector_all('.about-manual a', 'els => els.map(e => e.getAttribute("href"))')
    assert '/manuals/user-guide.pdf' in links and '/manuals/user-guide.docx' in links and '/manuals/admin-guide.pdf' in links
    r = page.request.get(admin_ui.base + '/manuals/user-guide.pdf')
    assert r.ok and r.headers['content-type'] == 'application/pdf'
    page.click('#aboutVersions')
    page.wait_for_selector('#confirmModal.show .versions-list')
    assert page.get_attribute('#lnkManual', 'href') == '/manuals/user-guide.pdf'


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


def test_edge_cases_render_with_sensible_statuses(ui, admin):
    """Ровный дробный остаток, сданный по нулям эпик и эпик с замерами в другие дни: дашборд и свод по этапам
    не падают, статусы и прогнозы — по смыслу, в burn rate — только даты показанных эпиков."""
    pid = admin.post('/api/projects', json={'name': 'Края'}).get_json()['id']

    def epic(name, rows, target, done=False):
        sid = admin.post(f'/api/projects/{pid}/services', json={'name': name, 'etap': 'Этап 1', 'targetDate': target,
                                                                'isDone': done}).get_json()['id']
        for date, rem in rows:
            admin.post(f'/api/services/{sid}/entries', json={'date': date, 'remaining': rem})
    epic('Заблокирован', [('2026-09-01', 120.7), ('2026-09-08', 120.7), ('2026-09-15', 120.7)], '2026-10-01')
    epic('Сдан по нулям', [('2026-09-01', 100), ('2026-09-08', 50), ('2026-09-15', 0), ('2026-09-22', 0)], '2026-09-19')
    epic('Скрытый сданный', [('2026-09-03', 30), ('2026-09-10', 10)], '2026-09-30', done=True)
    ui.login(*ADMIN)
    ui.project(pid)
    page = ui.page
    page.check('#dashHideDone')
    rows = {r[1]: r for r in (ui.cells(f'#summaryTable tbody tr:nth-child({i}) td') for i in (1, 2))}
    assert rows['Заблокирован'][6] == 'не сгорает' and 'Не успеваем' in rows['Заблокирован'][8]
    assert rows['Сдан по нулям'][6] == '15.09.2026' and 'Успеваем' in rows['Сдан по нулям'][8]
    assert rows['Сдан по нулям'][7] == '100%'
    assert ui.cells('#burnRateTable thead th')[1:] == ['01.09', '08.09', '15.09', '22.09']  # без 03.09 и 10.09 скрытого эпика
    ui.go('stages')
    assert ui.page.locator('#stageSummaryTable tbody tr').count() == 1


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
    with page.expect_response(lambda r: '/api/services/2' in r.url):
        page.check('#servicesTable tr[data-id="2"] input[data-field="isDone"]')
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
    assert not page.is_visible('#view-data .edit-only')  # «Изменения сохраняются автоматически» — не для просмотра
    ui.go('services')
    assert not page.is_visible('#btnAddService')
    assert page.is_disabled('#servicesTable input[type=checkbox] >> nth=0')
    assert not page.is_visible('#view-services .edit-only')


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
    page.wait_for_selector('#usersTable tbody td:first-child:text-is("ivan")')  # список перезагружается после ответа
    # новый пользователь сразу выбран для настройки доступа (блок дорисовывается после загрузки проектов)
    page.wait_for_selector('#userProjectsMgmt [data-toggle="1"]')


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
    with page.expect_response(lambda r: r.request.method == 'POST' and '/projects' in r.url):
        page.select_option('#userProjectsMgmt select[data-pid="1"]', 'manager')
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
    page.wait_for_selector('#userInfo img', state='detached')


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
    assert 'Эпик «КСБО»' in row and 'Название' in row  # тип записи и поле — по-русски
    assert re.match(r'\d{2}\.\d{2}\.\d{4} \d{2}:\d{2}', admin_ui.cells('#auditLogTable tbody tr:first-child td')[0])
    page.select_option('#auditFilterPage', 'services')
    page.wait_for_function("""() => {
        const cells = [...document.querySelectorAll('#auditLogTable tbody td:nth-child(4)')];
        return cells.length > 0 && cells.every(c => c.textContent === 'Эпики');
    }""")


# ─── Статусы, рост оценки, burn rate ─────────────────────────────

def test_status_without_target_and_non_burning_epic(admin_ui, admin):
    admin.put('/api/services/3', json={'targetDate': ''})
    admin_ui.page.reload()
    admin_ui.page.wait_for_selector('#summaryTable tbody tr')
    ksbo = admin_ui.cells('#summaryTable tbody tr:has-text("КСБО") td')
    assert ksbo[-1] == 'Нет цели' and ksbo[-2] == '—'  # без цели нет и шанса успеть
    # остаток «Планирования закупок» растёт (55 → 86) — прогноз не достигает нуля, шанса успеть нет
    plan = admin_ui.cells('#summaryTable tbody tr:has-text("Планирование закупок") td')
    assert plan[-3] == 'не сгорает' and plan[-2] == '0%' and plan[-1] == '✗ Не успеваем'


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
    # состояние сохраняется в обработчике события toggle, которое приходит асинхронно
    page.wait_for_function("localStorage.getItem('burndown-all-epics-open') === '0'")
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


# ─── Устойчивость интерфейса ─────────────────────────────────────

def test_network_failure_message_and_rollback(admin_ui):
    page = admin_ui.page
    admin_ui.go('data')
    page.route('**/api/entries/*', lambda route: route.abort())
    cell = '#entriesTable tbody tr:last-child input[data-field="spent"]'
    page.fill(cell, '999')
    page.dispatch_event(cell, 'change')
    admin_ui.expect_toast('Нет связи с сервером — изменения не сохранены')
    page.wait_for_function(f"document.querySelector('{cell}').value === '258'")


def test_epic_without_stage_grouped_as_no_stage(admin_ui, admin):
    admin.put('/api/services/6', json={'etap': ''})
    admin_ui.page.reload()
    admin_ui.page.wait_for_selector('#summaryTable tbody tr')
    admin_ui.go('stages')
    assert 'Без этапа' in admin_ui.cells('#stageSummaryTable tbody td:first-child')
    target = '#stageChartsContainer input[data-stage="Без этапа"]'
    admin_ui.page.fill(target, '2026-09-30')
    admin_ui.expect_toast('Целевая дата «Без этапа» обновлена')
    assert admin.get('/api/projects/1/data').get_json()['stageTargets']['Без этапа'] == '2026-09-30'


def test_deleted_user_is_sent_to_login(ui, make_user, admin):
    c = make_user('temp', project_id=1)
    ui.login('temp', 'pw-12345')
    admin.delete(f'/api/users/{c.user_id}')
    ui.page.click('.nav-item[data-view="stages"]')
    ui.page.reload()
    ui.page.wait_for_url(ui.base + '/login')


# ─── Связь с Jira ────────────────────────────────────────────────

@pytest.fixture
def fake_jira():
    from fake_jira import FakeJira
    server = FakeJira()
    yield server
    server.stop()


def setup_jira_connection(ui, fake_jira):
    """Администратор настраивает общее подключение в «Интеграциях» и переходит к кнопкам среза проекта."""
    page = ui.page
    ui.go('integrations')
    page.fill('#jiraUrl', fake_jira.url)
    page.select_option('#jiraType', 'server')
    page.fill('#jiraToken', 'secret-pat')
    page.click('#jiraSave')
    ui.expect_toast('Подключение к Jira сохранено')
    ui.go('jira')
    page.wait_for_selector('#jiraProjectStatus.ok')


def test_jira_connection_form(admin_ui, fake_jira):
    page = admin_ui.page
    admin_ui.go('jira')  # в проекте — только состояние общего подключения
    assert 'не настроено' in admin_ui.text('#jiraProjectStatus') and not page.is_visible('#jiraUrl')
    page.click('#jiraOpenIntegrations')
    page.wait_for_selector('#view-integrations.active')
    page.wait_for_selector('#jiraStatus:has-text("Подключение не настроено")')
    assert page.input_value('#jiraType') == 'server_basic'  # по умолчанию — Server/DC, логин и пароль
    page.fill('#jiraUrl', 'https://corp.atlassian.net')
    page.dispatch_event('#jiraUrl', 'change')
    assert page.input_value('#jiraType') == 'cloud' and admin_ui.text('#jiraLoginLabel') == 'Email'
    page.select_option('#jiraType', 'server_basic')
    assert admin_ui.text('#jiraTokenLabel') == 'Пароль' and 'HTTPS' in admin_ui.text('#jiraHint')
    # неверный токен — понятная ошибка
    page.fill('#jiraUrl', fake_jira.url)
    page.select_option('#jiraType', 'server')
    page.fill('#jiraToken', 'wrong')
    page.click('#jiraTest')
    page.wait_for_selector('#jiraStatus.error')
    assert '401' in admin_ui.text('#jiraStatus')
    page.fill('#jiraToken', 'secret-pat')
    page.click('#jiraTest')
    page.wait_for_selector('#jiraStatus.ok:has-text("Тестовый Пользователь")')
    page.click('#jiraSave')
    admin_ui.expect_toast('Подключение к Jira сохранено')
    # токен не возвращается в форму, вместо него — подсказка
    assert page.input_value('#jiraToken') == ''
    assert 'Сохранён' in page.get_attribute('#jiraToken', 'placeholder')
    assert page.is_visible('#jiraDisconnect')
    admin_ui.go('jira')
    page.wait_for_selector('#jiraProjectStatus.ok')
    assert admin_ui.text('#jiraProjectStatus') == '✓ Подключение к Jira настроено'


def test_manager_sees_connection_but_cannot_change_it(ui, make_user, admin, fake_jira):
    admin.put('/api/admin/jira-connection', json={'baseUrl': fake_jira.url, 'instanceType': 'server_basic',
                                                  'login': 'svc', 'token': 'pw'})
    make_user('mgr', project_id=1, project_role='manager')
    ui.login('mgr', 'pw-12345')
    page = ui.page
    assert not page.is_visible('#navIntegrations')
    ui.go('jira')
    assert ui.text('#jiraProjectStatus') == '✓ Подключение к Jira настроено'  # только факт, без адреса и учётной записи
    assert fake_jira.url not in page.content() and 'svc' not in ui.text('#view-jira')
    assert not page.is_visible('#jiraOpenIntegrations') and not page.is_visible('#jiraUrl')
    assert page.is_enabled('#jiraAddButton')  # кнопки среза руководитель настраивает сам


def test_jira_snapshot_button_flow(admin_ui, fake_jira, admin):
    import datetime
    page = admin_ui.page
    setup_jira_connection(admin_ui, fake_jira)
    page.click('#jiraAddButton')
    page.fill('#jbName', 'Срез релиза')
    page.click('#jbLoadStructures')
    admin_ui.expect_toast('Структур загружено: 2')
    page.select_option('#jbStructure', '7')
    page.fill('#jbTable tr[data-sid="1"] input[data-field="keys"]', 'abc-1')
    page.fill('#jbTable tr[data-sid="2"] input[data-field="keys"]', 'ABC-4')
    page.uncheck('#jbTable tr[data-sid="2"] input[data-field="subtree"]')
    page.click('#confirmOk')
    admin_ui.expect_toast('Кнопка создана')
    row = admin_ui.text('#jiraButtonsTable tbody tr')
    assert 'Срез релиза' in row and 'Релиз 2026' in row and 'ABC-1' in row

    admin_ui.go('data')
    page.click('#jiraSnapshotButtons button:has-text("Срез релиза")')
    page.wait_for_selector('#confirmModal.show #bulkTable')
    assert admin_ui.text('#confirmTitle') == 'Срез из Jira: Срез релиза'
    assert page.locator('#bulkTable tbody tr').count() == 2
    first = '#bulkTable tr[data-sid="1"]'
    assert [page.input_value(f'{first} input[data-field="{f}"]') for f in ('remaining', 'spent', 'estimate')] == ['12.5', '6', '16']
    assert 'Задач Jira учтено: 3' in admin_ui.text(first) and 'недоступны' in admin_ui.text(first)
    assert page.input_value('#bulkDate') == datetime.date.today().isoformat()
    page.click('#confirmOk')
    admin_ui.expect_toast('Сохранено замеров: 2')
    today = datetime.date.today().isoformat()
    saved = {e['service_id']: e for e in admin.get('/api/projects/1/data').get_json()['entries'] if e['date'] == today}
    assert (saved[1]['remaining'], saved[1]['spent'], saved[1]['estimate']) == (12.5, 6, 16)
    assert saved[2]['remaining'] == 10


def test_jira_button_edit_and_delete(admin_ui, fake_jira):
    page = admin_ui.page
    setup_jira_connection(admin_ui, fake_jira)
    page.click('#jiraAddButton')
    page.fill('#jbName', 'Без структуры')
    page.fill('#jbTable tr[data-sid="3"] input[data-field="keys"]', 'ABC-1')
    page.click('#confirmOk')
    admin_ui.expect_toast('Кнопка создана')
    assert 'без структуры' in admin_ui.text('#jiraButtonsTable tbody tr')
    # некорректный ключ — ошибка сервера, окно остаётся открытым
    page.click('#jiraButtonsTable [data-action="edit"]')
    page.select_option('#jbStructure', '__manual')
    page.fill('#jbStructureId', '7')
    page.fill('#jbTable tr[data-sid="3"] input[data-field="keys"]', 'ABC-1, не-ключ')
    page.click('#confirmOk')
    admin_ui.expect_toast('Некорректные ключи задач: не-ключ')
    assert page.is_visible('#confirmModal.show')
    page.fill('#jbTable tr[data-sid="3"] input[data-field="keys"]', 'ABC-1')
    page.click('#confirmOk')
    admin_ui.expect_toast('Кнопка обновлена')
    assert '#7' in admin_ui.text('#jiraButtonsTable tbody tr')
    page.click('#jiraButtonsTable [data-action="delete"]')
    page.click('#confirmOk')
    admin_ui.expect_toast('Кнопка удалена')
    assert 'Кнопок пока нет' in admin_ui.text('#jiraButtonsTable')


def test_jira_snapshot_error_is_shown(admin_ui, fake_jira):
    page = admin_ui.page
    setup_jira_connection(admin_ui, fake_jira)
    page.click('#jiraAddButton')
    page.fill('#jbName', 'Срез')
    page.select_option('#jbStructure', '__manual')
    page.fill('#jbStructureId', '99')
    page.fill('#jbTable tr[data-sid="1"] input[data-field="keys"]', 'ABC-1')
    page.click('#confirmOk')
    admin_ui.expect_toast('Кнопка создана')
    admin_ui.go('data')
    page.click('#jiraSnapshotButtons button')
    admin_ui.expect_toast('Структура 99 не найдена или плагин Structure недоступен')
    assert not page.is_visible('#confirmModal.show')
    assert page.is_enabled('#jiraSnapshotButtons button')


def test_jira_hidden_for_viewer(ui, make_user, admin, fake_jira):
    admin.put('/api/admin/jira-connection', json={'baseUrl': fake_jira.url, 'instanceType': 'server', 'token': 'secret-pat'})
    admin.post('/api/projects/1/jira/buttons', json={'name': 'Срез', 'mappings': [{'serviceId': 1, 'issueKeys': 'ABC-1'}]})
    make_user('viewer', project_id=1)
    ui.login('viewer', 'pw-12345')
    assert not ui.page.is_visible('#navJira') and not ui.page.is_visible('#navAutofill')
    ui.go('data')
    assert ui.page.locator('#jiraSnapshotButtons button').count() == 0


def test_jira_field_mapping_in_editor(admin_ui, fake_jira, admin):
    page = admin_ui.page
    setup_jira_connection(admin_ui, fake_jira)
    page.click('#jiraAddButton')
    page.fill('#jbName', 'Свои поля')
    assert page.input_value('#jbField_remaining') == 'timeestimate' and page.input_value('#jbUnit_remaining') == 'seconds'
    page.click('#jbLoadFields')
    admin_ui.expect_toast('Числовых полей загружено: 4')
    page.select_option('#jbField_remaining', 'customfield_10050')
    assert page.input_value('#jbUnit_remaining') == 'hours'  # своё числовое поле — по умолчанию в часах
    page.select_option('#jbField_spent', '')
    page.fill('#jbTable tr[data-sid="1"] input[data-field="keys"]', 'ABC-1')
    page.click('#confirmOk')
    admin_ui.expect_toast('Кнопка создана')
    row = admin_ui.text('#jiraButtonsTable tbody tr')
    assert 'customfield_10050' in row and 'не заполняется' in row
    fm = admin.get('/api/projects/1/jira').get_json()['buttons'][0]['fieldMap']
    assert fm['remaining'] == {'field': 'customfield_10050', 'unit': 'hours'} and fm['spent']['field'] == ''
    admin_ui.go('data')
    page.click('#jiraSnapshotButtons button:has-text("Свои поля")')
    page.wait_for_selector('#confirmModal.show #bulkTable')
    first = '#bulkTable tr[data-sid="1"]'
    assert [page.input_value(f'{first} input[data-field="{f}"]') for f in ('remaining', 'spent', 'estimate')] == ['25.5', '', '18']


def test_autofill_page_unlocks_after_snapshot(admin_ui, fake_jira, admin):
    import datetime
    page = admin_ui.page
    setup_jira_connection(admin_ui, fake_jira)
    bid = admin.post('/api/projects/1/jira/buttons', json={
        'name': 'Ежедневный срез', 'structureId': 7, 'structureName': 'Релиз 2026',
        'mappings': [{'serviceId': 1, 'issueKeys': 'ABC-1', 'includeSubtree': True}]}).get_json()['id']
    page.reload()
    admin_ui.go('autofill')
    card = f'.autofill-card[data-bid="{bid}"]'
    page.wait_for_selector(card)
    assert 'после первого успешного среза' in admin_ui.text(card)
    assert page.is_disabled(f'{card} [data-f="enabled"]') and page.is_disabled(f'{card} [data-action="run"]')
    # первый срез вручную открывает расписание
    admin_ui.go('data')
    page.click('#jiraSnapshotButtons button:has-text("Ежедневный срез")')
    admin_ui.expect_toast('Первый срез получен — для кнопки доступно автозаполнение по расписанию')
    page.click('#confirmCancel')
    admin_ui.go('autofill')
    assert not page.is_disabled(f'{card} [data-f="enabled"]')
    page.check(f'{card} [data-f="enabled"]')
    page.click(f'{card} .weekday-picker label:has-text("Сб")')
    page.fill(f'{card} [data-f="time"]', '07:30')
    page.select_option(f'{card} [data-f="timezone"]', 'Asia/Novosibirsk')
    page.click(f'{card} button[type="submit"]')
    admin_ui.expect_toast('Расписание сохранено: Пн, Вт, Ср, Чт, Пт, Сб в 07:30')
    s = admin.get('/api/projects/1/jira').get_json()['buttons'][0]['schedule']
    assert s['enabled'] and s['days'] == [1, 2, 3, 4, 5, 6] and s['timezone'] == 'Asia/Novosibirsk'
    assert '07:30' in admin_ui.text(card) and 'Asia/Novosibirsk' in admin_ui.text(card)
    admin_ui.go('jira')
    assert 'автозаполнение: Пн, Вт, Ср, Чт, Пт, Сб в 07:30' in admin_ui.text('#jiraButtonsTable tbody tr')
    # «Запустить сейчас» — замер на сегодня сохранён, статус обновлён
    admin_ui.go('autofill')
    page.click(f'{card} [data-action="run"]')
    page.wait_for_function('s => document.querySelector(s + " .autofill-result.ok")', arg=card)
    assert 'Сохранено замеров' in admin_ui.text(card)
    today = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=7))).date().isoformat()
    entries = admin.get('/api/projects/1/data').get_json()['entries']
    assert any(e['service_id'] == 1 and e['date'] == today and e['remaining'] == 12.5 for e in entries)
    assert not admin_ui.errors


def test_jira_composition_on_dashboard(admin_ui, fake_jira, admin):
    page = admin_ui.page
    assert page.is_hidden('#jiraCompPanel') and page.is_hidden('#jiraBurnupPanel')  # срезов ещё не было
    admin.put('/api/admin/jira-connection', json={'baseUrl': fake_jira.url, 'instanceType': 'server', 'token': 'secret-pat'})
    bid = admin.post('/api/projects/1/jira/buttons', json={'name': 'Срез', 'structureId': 7, 'mappings': [
        {'serviceId': 1, 'issueKeys': 'ABC-1', 'includeSubtree': True}]}).get_json()['id']
    for date in ('2026-11-02', '2026-11-03'):
        admin.post(f'/api/jira/buttons/{bid}/snapshot', json={'date': date})
    page.reload()
    page.wait_for_selector('#jiraCompTable tbody tr[data-sid="1"]')
    row = admin_ui.text('#jiraCompTable tbody tr[data-sid="1"]')
    assert '19%' in row and '1 / 2' in row and '⚠ 1' in row and '03.11.2026' in row
    assert page.locator('#jiraCompTable tbody tr').count() == 1  # только эпики со срезом
    # burn-up выбранного эпика: две точки истории
    page.select_option('#chartServiceSelect', '1')
    page.wait_for_selector('#jiraBurnupPanel:not([hidden])')
    assert page.evaluate("Chart.getChart('chartBurnup').data.datasets.map(d => d.data.length)") == [2, 2]
    page.select_option('#chartServiceSelect', '2')
    assert page.is_hidden('#jiraBurnupPanel')
    # список задач эпика: ссылки в Jira и фильтр проблем
    page.click('#jiraCompTable tbody tr[data-sid="1"]')
    page.wait_for_selector('#jiTable tbody tr')
    assert admin_ui.text('#confirmTitle') == 'Задачи Jira: ' + admin_ui.text('#jiraCompTable tbody tr[data-sid="1"] td')
    assert page.get_attribute('#jiTable a:has-text("ABC-2")', 'href') == fake_jira.url + '/browse/ABC-2'
    assert page.is_hidden('#confirmCancel')
    page.click('#jiFilter [data-filter="problems"]')
    visible = [t.strip() for t in page.locator('#jiTable tbody tr:not([hidden]) td:first-child').all_text_contents()]
    assert visible == ['ABC-2']
    assert 'Закрыта, но остаток не обнулён' in admin_ui.text('#jiTable tbody tr:not([hidden])')
    page.click('#confirmOk')
    page.wait_for_selector('#confirmModal:not(.show)', state='attached')
    # в обычных окнах кнопка «Отмена» снова на месте
    admin_ui.go('data')
    page.click('#btnBulkMeasure')
    assert page.is_visible('#confirmCancel')
    page.click('#confirmCancel')
    admin_ui.go('dashboard')
    # фильтр эпиков дашборда скрывает и строку состава
    page.uncheck('#dashEpicChecks input[data-sid="1"]')
    assert page.is_hidden('#jiraCompPanel')
    assert not admin_ui.errors


def test_jira_composition_visible_for_viewer(ui, make_user, admin, fake_jira):
    admin.put('/api/admin/jira-connection', json={'baseUrl': fake_jira.url, 'instanceType': 'server', 'token': 'secret-pat'})
    bid = admin.post('/api/projects/1/jira/buttons', json={'name': 'Срез', 'mappings': [
        {'serviceId': 1, 'issueKeys': 'ABC-1', 'includeSubtree': True}]}).get_json()['id']
    admin.post(f'/api/jira/buttons/{bid}/snapshot', json={'date': '2026-11-02'})
    make_user('viewer', project_id=1)
    ui.login('viewer', 'pw-12345')
    ui.page.wait_for_selector('#jiraCompTable tbody tr[data-sid="1"]')
    ui.page.click('#jiraCompTable tbody tr[data-sid="1"]')
    ui.page.wait_for_selector('#jiTable tbody tr')
    assert ui.page.locator('#jiTable tbody tr').count() == 2  # ABC-1 и подзадача ABC-6


def test_forecast_probability_and_range(admin_ui):
    page = admin_ui.page
    # демо: у эпиков по 4 замера (3 интервала) и цели — у каждого есть шанс успеть и диапазон
    cells = admin_ui.cells('#summaryTable tbody tr:not(.total-row) td:nth-child(8)')
    assert cells and all(re.fullmatch(r'\d{1,3}%', c) for c in cells), cells
    title = page.get_attribute('#summaryTable tbody tr:first-child .prob', 'title')
    assert 'Диапазон 70%' in title and 'Медиана (P50)' in title and 'По 3 интервалам' in title
    # детерминированно: после перерисовки те же значения
    page.click('#summaryTable th[data-sort="name"]')
    assert sorted(admin_ui.cells('#summaryTable tbody tr:not(.total-row) td:nth-child(8)')) == sorted(cells)
    ranges = page.evaluate("Chart.getChart('chartRemaining').data.datasets.filter(d => d.isRange).length")
    legend = admin_ui.text('#legendRemaining')
    assert ranges in (0, 2) and (ranges == 0 or 'Диапазон прогноза 70%' in legend)
    admin_ui.go('stages')
    page.wait_for_selector('#stageSummaryTable tbody tr')
    assert 'Шанс успеть' in admin_ui.text('#stageSummaryTable thead')
    assert not admin_ui.errors


def test_jira_changes_between_snapshots(admin_ui, fake_jira, admin):
    import fake_jira as fj
    page = admin_ui.page
    admin.put('/api/admin/jira-connection', json={'baseUrl': fake_jira.url, 'instanceType': 'server', 'token': 'secret-pat'})
    bid = admin.post('/api/projects/1/jira/buttons', json={'name': 'Срез', 'structureId': 7, 'mappings': [
        {'serviceId': 1, 'issueKeys': 'ABC-1', 'includeSubtree': True}]}).get_json()['id']
    admin.post(f'/api/jira/buttons/{bid}/snapshot', json={'date': '2026-11-02'})
    old = dict(fj.ISSUES[103])
    fj.ISSUES[103].update(category='done', timeoriginalestimate=3 * 3600)
    try:
        admin.post(f'/api/jira/buttons/{bid}/snapshot', json={'date': '2026-11-09'})
    finally:
        fj.ISSUES[103] = old
    page.reload()
    page.wait_for_selector('#jiraCompTable [data-changes="1"]')
    assert admin_ui.text('#jiraCompTable [data-changes="1"]') == '+2 ч'
    page.click('#jiraCompTable [data-changes="1"]')
    page.wait_for_selector('#chartScopeChanges')
    assert admin_ui.text('#confirmTitle').startswith('Что изменилось: ')
    intro = admin_ui.text('#confirmBody .bulk-intro')
    assert '16 ч' in intro and '18 ч' in intro
    assert page.evaluate("Chart.getChart('chartScopeChanges').data.labels") == ['Объём 02.11', 'Новые', 'Убраны', 'Переоценка', 'Объём 09.11']
    groups = admin_ui.text('#confirmBody .changes-lists')
    assert 'Переоценка · 1 · +2 ч' in groups and 'Закрыты за период · 1' in groups and 'ABC-3' in groups
    page.click('#confirmOk')
    page.wait_for_selector('#confirmModal:not(.show)', state='attached')
    # в списке задач — изменения с прошлого среза
    page.click('#jiraCompTable tbody tr[data-sid="1"]')
    page.wait_for_selector('#jiTable tbody tr')
    assert 'изменение с прошлого среза (02.11.2026)' in admin_ui.text('#confirmBody .bulk-intro')
    assert admin_ui.text('#jiTable tr:has-text("ABC-3") td:nth-child(6) .cell-delta') == '+2'
    assert not admin_ui.errors


def test_jira_history_restore_ui(admin_ui, fake_jira, admin):
    page = admin_ui.page
    admin.put('/api/admin/jira-connection', json={'baseUrl': fake_jira.url, 'instanceType': 'server', 'token': 'secret-pat'})
    bid = admin.post('/api/projects/1/jira/buttons', json={'name': 'Срез', 'structureId': 7, 'mappings': [
        {'serviceId': 3, 'issueKeys': 'ABC-1', 'includeSubtree': True}]}).get_json()['id']
    page.reload()
    admin_ui.go('jira')
    page.wait_for_selector('#jiraButtonsTable [data-action="history"]')
    assert page.is_disabled('#jiraButtonsTable [data-action="history"]')  # до первого среза
    admin.post(f'/api/jira/buttons/{bid}/snapshot', json={'date': '2026-10-28'})
    page.reload()
    admin_ui.go('jira')
    page.wait_for_selector('#jiraButtonsTable [data-action="history"]:not([disabled])')
    page.click('#jiraButtonsTable [data-action="history"]')
    page.fill('#hrFrom', '2026-10-19')
    page.fill('#hrTo', '2026-10-26')
    page.select_option('#hrTz', 'Europe/Moscow')
    page.click('#confirmOk')
    page.wait_for_selector('#hrProgress[data-status="done"]', timeout=20000)
    assert 'Восстановлено замеров: 2 на 2 дат(ы)' in admin_ui.text('#hrProgress')
    assert admin_ui.text('#confirmOk') == 'Закрыть'
    page.click('#confirmOk')
    entries = {e['date']: e for e in admin.get('/api/projects/1/data').get_json()['entries'] if e['service_id'] == 3}
    assert entries['2026-10-19']['remaining'] == 14 and entries['2026-10-26']['remaining'] == 12.5
    page.wait_for_function("document.querySelector('#jiraButtonsTable tbody tr').textContent.includes('история: 19.10.2026 — 26.10.2026, готово')")
    assert not admin_ui.errors


def test_forecast_drift_chart(admin_ui):
    page = admin_ui.page
    page.select_option('#chartServiceSelect', '1')
    page.wait_for_selector('#driftPanel:not([hidden])')
    # демо: 4 замера → прогноз на 3 даты (07-24, 07-28, 07-29); медиана Монте-Карло — с третьего замера
    lens = page.evaluate("Chart.getChart('chartDrift').data.datasets.map(d => d.data.length)")
    assert lens[:4] == [3, 3, 3, 3]
    p50 = page.evaluate("Chart.getChart('chartDrift').data.datasets[1].data.map(p => p.y)")
    assert p50[0] is None and p50[1] and p50[2]
    note = admin_ui.text('#driftNote')
    assert note.startswith('Прогноз завершения на последний замер:') and 'С прошлого замера (28.07.2026)' in note
    assert 'Цель: 31.08.2026' in admin_ui.text('#legendDrift')
    admin_ui.project(2)  # пустой проект — графика нет
    assert page.is_hidden('#driftPanel')
    assert not admin_ui.errors


def test_burnup_forecast_and_stage_level(admin_ui, fake_jira, admin):
    import datetime
    import fake_jira as fj
    page = admin_ui.page
    admin.put('/api/admin/jira-connection', json={'baseUrl': fake_jira.url, 'instanceType': 'server', 'token': 'secret-pat'})
    bid = admin.post('/api/projects/1/jira/buttons', json={'name': 'Срез', 'structureId': 7, 'mappings': [
        {'serviceId': 1, 'issueKeys': 'ABC-1', 'includeSubtree': True},
        {'serviceId': 2, 'issueKeys': 'ABC-4', 'includeSubtree': False}]}).get_json()['id']
    admin.post(f'/api/jira/buttons/{bid}/snapshot', json={'date': '2026-11-02'})
    old = dict(fj.ISSUES[103])
    fj.ISSUES[103].update(category='done')
    try:
        admin.post(f'/api/jira/buttons/{bid}/snapshot', json={'date': '2026-11-09'})
    finally:
        fj.ISSUES[103] = old
    page.reload()
    page.wait_for_selector('#jiraCompTable tbody tr[data-sid="1"]')
    # эпик 1: объём 16 ч, выполнено 3 → 4 ч за неделю, объём не растёт → 12 ч / (1/7 ч в день) = 84 дня
    expected = (datetime.date(2026, 11, 9) + datetime.timedelta(days=84)).strftime('%d.%m.%Y')
    row = admin_ui.cells('#jiraCompTable tbody tr[data-sid="1"] td')
    assert row[6] == expected
    assert page.get_attribute('#jiraCompTable tbody tr[data-sid="1"] td:nth-child(7) span', 'class') == 'prob-low'  # позже цели
    # эпик 2: ничего не закрывается — линии не сходятся
    assert admin_ui.cells('#jiraCompTable tbody tr[data-sid="2"] td')[6] == 'не сходится'
    total = admin_ui.cells('#jiraCompTable tbody tr.total-row td')
    assert total[0] == 'Итого по проекту' and total[3] == '2 / 3' and total[4] == '26 ч'
    page.select_option('#chartServiceSelect', '1')
    page.wait_for_selector('#jiraBurnupPanel:not([hidden])')
    assert f'Прогноз по задачам Jira: {expected}' in admin_ui.text('#burnupNote')
    labels = page.evaluate("Chart.getChart('chartBurnup').data.datasets.map(d => d.label)")
    assert f'Прогноз: {expected}' in labels
    page.select_option('#chartServiceSelect', '2')
    assert 'не завершится' in admin_ui.text('#burnupNote')
    # свод по этапам: этап 4 (эпики 1 и 2) и итог по проекту, burn-up этапа
    admin_ui.go('stages')
    page.wait_for_selector('#stageJiraTable tbody tr[data-stage]')
    stages = admin_ui.cells('#stageJiraTable tbody tr td:first-child')
    assert stages == ['Этап 4', 'Итого по проекту']
    assert page.evaluate("[...document.querySelectorAll('#stageBurnupSelect option')].map(o => o.value)") == ['Этап 4', '__project']
    page.select_option('#stageBurnupSelect', '__project')
    page.wait_for_function("Chart.getChart('chartStageBurnup') && Chart.getChart('chartStageBurnup').data.datasets[0].data.length === 2")
    assert 'Прогноз по задачам Jira' in admin_ui.text('#stageBurnupNote')
    page.click('#stageJiraTable tbody tr[data-stage="Этап 4"]')
    assert page.input_value('#stageBurnupSelect') == 'Этап 4'
    assert not admin_ui.errors


@pytest.fixture
def fake_mail(monkeypatch):
    import notify
    from fake_notify import FakeSMTP
    FakeSMTP.reset()
    monkeypatch.setattr(notify.smtplib, 'SMTP', FakeSMTP)
    monkeypatch.setattr(notify.smtplib, 'SMTP_SSL', FakeSMTP)
    return FakeSMTP


@pytest.fixture
def fake_tg(flask_app, monkeypatch):
    from fake_notify import FakeTelegram
    server = FakeTelegram()
    monkeypatch.setitem(flask_app.config, 'TELEGRAM_API', server.url)
    yield server
    server.stop()


def test_notify_admin_settings_ui(admin_ui, fake_mail, fake_tg):
    page = admin_ui.page
    admin_ui.go('integrations')
    page.wait_for_selector('#notifyLogTable thead')
    page.fill('#naHost', 'smtp.example.ru')
    page.fill('#naPort', '465')
    page.select_option('#naSecurity', 'ssl')
    page.fill('#naSender', 'burndown@example.ru')
    page.fill('#naLogin', 'burndown')
    page.fill('#naPassword', 'secret')
    page.fill('#naToken', '123:ABC')
    page.fill('#naUrl', 'https://burndown.example.ru')
    page.click('#naSave')
    admin_ui.expect_toast('Настройки рассылок сохранены')
    assert admin_ui.text('#naBotStatus') == '✓ Бот @burndown_test_bot'
    assert page.input_value('#naPassword') == '' and 'Сохранён' in page.get_attribute('#naPassword', 'placeholder')
    page.fill('#naTestTo', 'ops@example.ru')
    page.click('#naTestEmail')
    admin_ui.expect_toast('Тестовое письмо отправлено на ops@example.ru')
    conn = fake_mail.connections[-1]
    assert (conn.port, conn.user) == (465, 'burndown') and fake_mail.sent[-1]['To'] == 'ops@example.ru'
    page.wait_for_selector('#notifyLogTable tbody tr')
    assert 'Проверка' in admin_ui.text('#notifyLogTable tbody tr') and '✓' in admin_ui.text('#notifyLogTable tbody tr')
    assert not admin_ui.errors


def test_notify_project_view_ui(admin_ui, admin, fake_mail, fake_tg):
    page = admin_ui.page
    admin.put('/api/admin/notify-settings', json={'smtp': {'host': 'smtp.example.ru', 'port': 587, 'security': 'starttls',
                                                           'sender': 'burndown@example.ru'},
                                                  'telegram': {'token': '123:ABC'}, 'publicUrl': 'https://burndown.example.ru'})
    admin.put('/api/profile', json={'displayName': 'Администратор', 'email': 'admin@example.ru', 'position': 'Админ'})
    page.reload()
    page.wait_for_selector('#summaryTable tbody tr')
    ui_chance = admin_ui.cells('#summaryTable tbody tr:has-text("Работа с жалобами") td')[7]
    admin_ui.go('notify')
    page.wait_for_selector('#nfDays [data-day]')
    assert admin_ui.text('#nfEmailState') == 'admin@example.ru' and admin_ui.text('#nfTelegramState') == 'Не привязан'
    # привязка Telegram
    page.click('#nfTelegramLink')
    page.wait_for_selector('.telegram-steps a')
    code = page.get_attribute('.telegram-steps a', 'href').split('start=')[1]
    page.click('#confirmOk')
    admin_ui.expect_toast('Бот ещё не получил команду Start — откройте ссылку и нажмите «Запустить»')
    fake_tg.user_starts(code, chat_id=99)
    page.click('#confirmOk')
    admin_ui.expect_toast('Telegram привязан')
    page.wait_for_function("document.getElementById('nfTelegramState').textContent.includes('Привязан')")
    # дайджест по понедельникам и средам + оповещения, оба канала
    page.check('#nfViaTelegram')
    page.check('#nfDigestOn')
    page.click('#nfDays label:has-text("Ср")')
    page.fill('#nfTime', '08:30')
    page.select_option('#nfTz', 'Europe/Moscow')
    page.check('#nfAlertsOn')
    page.fill('#nfChance', '60')
    page.click('#nfSave')
    admin_ui.expect_toast('Настройки уведомлений сохранены')
    sub = admin.get('/api/projects/1/notifications').get_json()
    assert sub['viaTelegram'] and sub['digest']['days'] == [1, 3] and sub['digest']['time'] == '08:30'
    assert sub['alerts']['enabled'] and sub['alerts']['chanceThreshold'] == 60
    assert 'Europe/Moscow' in admin_ui.text('#nfDigestStatus')
    # предпросмотр: цифры совпадают с дашбордом
    page.click('#nfPreview')
    page.wait_for_selector('#confirmModal.show iframe.digest-preview')
    frame = page.frame_locator('iframe.digest-preview')
    row = frame.locator('tr:has-text("Работа с жалобами")').inner_text()
    assert ui_chance in row
    page.click('#confirmOk')
    page.wait_for_selector('#confirmModal:not(.show)', state='attached')
    page.click('#nfSendNow')
    admin_ui.expect_toast('Дайджест отправлен: почта, Telegram')
    assert fake_mail.sent[-1]['To'] == 'admin@example.ru' and fake_tg.state['messages'][-1]['chat_id'] == '99'
    page.wait_for_function("document.getElementById('nfDigestStatus').textContent.includes('почта: отправлено')")
    assert not admin_ui.errors


def test_notify_view_for_read_only_user(ui, make_user):
    make_user('viewer', project_id=1)
    ui.login('viewer', 'pw-12345')
    assert ui.page.is_visible('#navNotify') and not ui.page.is_visible('#navIntegrations')
    ui.go('notify')
    ui.page.wait_for_selector('#nfDays [data-day]')
    assert 'не настроил' in ui.text('#nfEmailState')
    assert not ui.errors
