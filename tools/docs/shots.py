"""Скриншоты для руководств пользователя и администратора.

    python3 tools/docs/shots.py

Скрипт сам поднимает демо-стенд: временную базу init_db (проекты АИСУЗ и ИСУП), приложение
и учебную Jira со Structure (tests/fake_jira.py с демо-задачами). Рабочая база не затрагивается.
Снимки кладутся в docs/manuals/images/{user,admin}. Нужен Playwright с Chromium
(путь к другому Chromium — в CHROMIUM_PATH).
"""
import os
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / 'docs' / 'manuals' / 'images'
TMP = tempfile.mkdtemp(prefix='burndown-shots-')
os.environ.update(DB_PATH=os.path.join(TMP, 'demo.db'), SECRET_KEY='demo-secret', BURNDOWN_SCHEDULER='0')
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]

import requests  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402
from werkzeug.serving import make_server  # noqa: E402

import app as app_module  # noqa: E402
import fake_jira  # noqa: E402
import init_db  # noqa: E402

H = 3600


def demo_jira():
    """Учебная Jira: структура «АИСУЗ — релиз 4.0», у каждого эпика — задача-эпик с тремя задачами."""
    totals = {  # эпик проекта: (ключ, остаток, затрачено, оценка), ч
        1: ('AISUZ-100', 330, 272, 410), 2: ('AISUZ-200', 198, 266, 330), 3: ('AISUZ-300', 96, 610, 526),
        4: ('AISUZ-400', 186, 2016, 1312), 5: ('AISUZ-500', 262, 2330, 1694), 6: ('AISUZ-600', 78, 377, 420)}
    issues, formula, row = {}, [], 1
    for epic, (key, rem, spent, est) in totals.items():
        root = epic * 100
        issues[root] = {'key': key, 'timeestimate': None, 'timespent': None, 'timeoriginalestimate': None}
        formula.append(f'{row}:0:{root}')
        row += 1
        for i, share in enumerate((0.5, 0.3, 0.2), start=1):
            issues[root + i] = {'key': f'AISUZ-{root + i}', 'parent': key, 'timeestimate': int(rem * share * H),
                                'timespent': int(spent * share * H), 'timeoriginalestimate': int(est * share * H),
                                'customfield_10050': round(est * share / 8, 1)}
            formula.append(f'{row}:1:{root + i}')
            row += 1
    fake_jira.ISSUES = issues
    fake_jira.FORMULA = ','.join(formula)
    fake_jira.STRUCTURES = [{'id': 7, 'name': 'АИСУЗ — релиз 4.0'}, {'id': 8, 'name': 'Портфель проектов 2026'},
                            {'id': 9, 'name': 'Бэклог сопровождения'}]
    fake_jira.FIELDS = fake_jira.FIELDS[:5] + [
        {'id': 'customfield_10051', 'name': 'Трудозатраты по плану, ч', 'custom': True, 'schema': {'type': 'number'}},
        {'id': 'customfield_10016', 'name': 'Story Points', 'custom': True, 'schema': {'type': 'number'}}]
    fake_jira.FIELDS[4] = dict(fake_jira.FIELDS[4], name='Трудозатраты, чел.-дни')


def start_app():
    init_db.DB_PATH = os.environ['DB_PATH']
    app_module.DB_PATH = os.environ['DB_PATH']
    init_db.init_db()
    server = make_server('127.0.0.1', 0, app_module.app, threaded=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return f'http://127.0.0.1:{server.server_port}'


def prepare(base, jira_url):
    s = requests.Session()
    s.post(base + '/api/auth/login', json={'username': 'admin', 'password': 'Test198!'})
    s.put(base + '/api/profile', json={'displayName': 'Администратор', 'email': 'admin@example.ru',
                                       'position': 'Администратор системы'})
    users = {}
    for login, name, pos, role in [('ivanov', 'Иванов Сергей', 'Руководитель проекта', 'manager'),
                                   ('petrova', 'Петрова Анна', 'Аналитик', 'user'),
                                   ('sidorov', 'Сидоров Павел', 'Ведущий разработчик', 'user')]:
        users[login] = s.post(base + '/api/users', json={'username': login, 'password': 'Demo-2026', 'displayName': name,
                                                         'email': login + '@example.ru', 'position': pos,
                                                         'role': 'user'}).json()['id']
        s.post(f'{base}/api/users/{users[login]}/projects', json={'projectId': 1, 'role': role})
    s.put(base + '/api/projects/1/stage_targets', json={'stageName': 'Этап 4', 'targetDate': '2026-08-31'})
    s.put(base + '/api/projects/1/stage_targets', json={'stageName': 'Этап 3', 'targetDate': '2026-08-28'})
    s.put(base + '/api/entries/24', json={'date': '2026-07-29', 'remaining': 86, 'spent': 370, 'estimate': 416})
    return s


def main():
    demo_jira()
    jira = fake_jira.FakeJira(token='demo-pat')
    base = start_app()
    api = prepare(base, jira.url)
    (OUT / 'user').mkdir(parents=True, exist_ok=True)
    (OUT / 'admin').mkdir(parents=True, exist_ok=True)

    def shot(target, name, **kw):
        target.screenshot(path=str(OUT / name), animations='disabled', **kw)

    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=os.environ.get('CHROMIUM_PATH') or None, args=['--lang=ru'],
                                    env={**os.environ, 'LANG': 'ru_RU.UTF-8', 'LANGUAGE': 'ru'})

        def context(**kw):
            opts = dict(viewport={'width': 1440, 'height': 900}, device_scale_factor=2, color_scheme='light',
                        locale='ru-RU', timezone_id='Europe/Moscow')
            opts.update(kw)
            return browser.new_context(**opts)

        def login(pg, username, password):
            pg.goto(base + '/login')
            pg.fill('#username', username)
            pg.fill('#password', password)
            pg.click('button[type=submit]')
            pg.wait_for_selector('#summaryTable tbody tr')
            pg.wait_for_timeout(800)

        ctx = context()
        pg = ctx.new_page()
        pg.on('pageerror', lambda e: print('PAGEERR', e))

        def go(view, wait=None):
            pg.click(f'.nav-item[data-view="{view}"]')
            pg.wait_for_selector(f'#view-{view}.active')
            if wait:
                pg.wait_for_selector(wait)
            pg.wait_for_timeout(500)

        def hide_toast():
            pg.evaluate("document.getElementById('toast').classList.remove('show')")
            pg.wait_for_timeout(250)

        def modal(name):
            pg.click('#confirmTitle')  # снять фокус с поля ввода
            pg.wait_for_timeout(300)
            shot(pg.locator('#confirmModal .modal'), name)

        # ── Вход и дашборд
        pg.goto(base + '/login')
        pg.wait_for_timeout(300)
        shot(pg, 'user/01-login.png')
        login(pg, 'admin', 'Test198!')
        shot(pg, 'user/02-dashboard.png')
        shot(pg.locator('.sidebar'), 'user/03-sidebar.png')
        shot(pg.locator('#view-dashboard .quick-filter'), 'user/04-quick-filter.png')
        shot(pg.locator('#kpiGrid'), 'user/05-kpi.png')
        epic_panel = pg.locator('#view-dashboard > .panel').nth(1)
        shot(epic_panel, 'user/06-epic-chart.png')
        pt = pg.evaluate("(() => { const c = Chart.getChart('chartRemaining'); const p = c.getDatasetMeta(0).data[1];"
                         " const r = c.canvas.getBoundingClientRect(); return {x: r.left + p.x, y: r.top + p.y}; })()")
        pg.mouse.move(pt['x'], pt['y'])
        pg.wait_for_timeout(400)
        shot(epic_panel, 'user/07-epic-tooltip.png')
        pg.mouse.move(5, 5)
        shot(pg.locator('#allEpicsBlock'), 'user/08-all-epics.png')
        shot(pg.locator('#summaryTable').locator('xpath=ancestor::div[contains(@class,"panel")]'), 'user/09-summary.png')
        shot(pg.locator('#burnRateTable').locator('xpath=ancestor::div[contains(@class,"panel")]'), 'user/10-burnrate.png')

        # ── Свод по этапам
        go('stages', '#stageSummaryTable tbody tr')
        shot(pg, 'user/11-stages.png')
        shot(pg.locator('#stageChartsContainer .panel').first, 'user/12-stage-chart.png')
        shot(pg.locator('#stageSummaryTable').locator('xpath=ancestor::div[contains(@class,"panel")]'),
             'user/13-stage-summary.png')

        # ── Данные по датам
        go('data', '#entriesTable tbody tr')
        shot(pg.locator('#view-data .panel'), 'user/14-data.png')
        pg.click('#btnBulkMeasure')
        pg.fill('#bulkDate', '2026-07-30')
        pg.click('#bulkFillPrev')
        pg.fill('#bulkTable tr[data-sid="1"] input[data-field="remaining"]', '330')
        modal('user/15-bulk.png')
        pg.keyboard.press('Escape')
        pg.click('#entriesTable tbody tr:last-child [data-action="delete"]')
        modal('user/19-confirm.png')
        pg.keyboard.press('Escape')

        # ── Эпики, настройки
        go('services', '#servicesTable tbody tr')
        shot(pg.locator('#view-services .panel'), 'user/16-services.png')
        go('settings', '#settingsGrid select')
        shot(pg.locator('#view-settings'), 'user/17-settings.png')

        # ── Профиль
        pg.click('#userInfo')
        modal('user/18-profile.png')
        pg.keyboard.press('Escape')

        # ── Тёмная тема
        go('dashboard', '#summaryTable tbody tr')
        pg.click('#themeToggle')
        pg.wait_for_timeout(600)
        shot(pg, 'user/20-dark.png')
        pg.click('#themeToggle')
        pg.wait_for_timeout(300)

        # ── Связь с Jira: подключение
        go('jira')
        pg.fill('#jiraUrl', 'https://jira.example.ru')
        pg.select_option('#jiraType', 'server')
        pg.fill('#jiraToken', 'demo-pat')
        pg.evaluate('document.activeElement.blur()')
        shot(pg.locator('#view-jira .panel').first, 'user/24-jira-connection.png')
        shot(pg.locator('#view-jira .panel').first, 'admin/06-jira-connection.png')
        # для работы — адрес учебной Jira
        pg.fill('#jiraUrl', jira.url)
        pg.click('#jiraTest')
        pg.wait_for_function("document.getElementById('jiraStatus').classList.contains('ok')")
        pg.fill('#jiraUrl', 'https://jira.example.ru')
        shot(pg.locator('#view-jira .panel').first, 'user/25-jira-test.png')
        pg.fill('#jiraUrl', jira.url)
        pg.click('#jiraSave')
        pg.wait_for_timeout(600)
        hide_toast()

        # ── Редактор кнопки
        pg.click('#jiraAddButton')
        pg.fill('#jbName', 'Срез релиза 4.0')
        pg.click('#jbLoadStructures')
        pg.wait_for_selector('#jbStructure option[value="7"]', state='attached')
        pg.select_option('#jbStructure', '7')
        for sid in range(1, 7):
            pg.fill(f'#jbTable tr[data-sid="{sid}"] input[data-field="keys"]', f'AISUZ-{sid}00')
        pg.click('#jbLoadFields')
        pg.wait_for_timeout(500)
        hide_toast()
        modal('user/26-jira-button-editor.png')
        shot(pg.locator('#confirmModal .jira-fields'), 'user/27-jira-fields.png')
        pg.click('#confirmOk')
        pg.wait_for_selector('#jiraButtonsTable tbody tr[data-bid]')
        pg.wait_for_timeout(400)
        hide_toast()
        shot(pg.locator('#view-jira .panel').nth(1), 'user/28-jira-buttons.png')

        # ── Срез из Jira в «Данных по датам»
        go('data', '#entriesTable tbody tr')
        shot(pg.locator('#view-data .panel-head').first, 'user/29-data-toolbar.png')
        pg.click('#jiraSnapshotButtons button')
        pg.wait_for_selector('#confirmModal.show #bulkTable')
        pg.fill('#bulkDate', '2026-07-30')
        pg.wait_for_timeout(300)
        hide_toast()
        modal('user/30-snapshot-preview.png')
        pg.keyboard.press('Escape')

        # ── Автозаполнение: до первого среза (вторая кнопка) и настроенное расписание
        bid2 = api.post(base + '/api/projects/1/jira/buttons', json={
            'name': 'Срез сопровождения', 'structureId': None,
            'mappings': [{'serviceId': 3, 'issueKeys': 'AISUZ-301, AISUZ-302', 'includeSubtree': True}]}).json()['id']
        bid1 = api.get(base + '/api/projects/1/jira').json()['buttons'][0]['id']
        api.put(f'{base}/api/jira/buttons/{bid1}/schedule', json={
            'enabled': True, 'days': [1, 2, 3, 4, 5], 'time': '09:00', 'timezone': 'Europe/Moscow', 'overwrite': True})
        with app_module.app.app_context():  # сегодняшний автозапуск в 09:00 уже прошёл
            app_module.get_db().execute('UPDATE jira_schedules SET last_run_date = NULL')
            app_module.get_db().commit()
            app_module.run_due_schedules()
        pg.reload()
        pg.wait_for_selector('#summaryTable tbody tr')
        go('autofill', '.autofill-card')
        shot(pg.locator('#view-autofill .panel'), 'user/31-autofill.png')
        shot(pg.locator(f'.autofill-card[data-bid="{bid1}"]'), 'user/32-autofill-card.png')
        shot(pg.locator(f'.autofill-card[data-bid="{bid2}"]'), 'user/33-autofill-locked.png')

        # ── Администрирование
        go('users', '#usersTable tbody tr')
        pg.click('#usersTable tr:has-text("petrova") td:first-child')
        pg.wait_for_selector('#userProjectsMgmt select[data-pid="1"]')
        pg.wait_for_timeout(300)
        shot(pg, 'admin/01-users.png')
        pg.click('#usersTable tr:has-text("ivanov") [data-action="edit"]')
        modal('admin/02-user-edit.png')
        pg.keyboard.press('Escape')
        go('projects', '#projectsMgmtTable tbody tr')
        shot(pg.locator('#view-projects .panel'), 'admin/03-projects.png')
        pg.click('#btnAddProject')
        modal('admin/04-project-add.png')
        pg.keyboard.press('Escape')
        pg.set_viewport_size({'width': 1800, 'height': 900})  # реестр — широкая таблица
        go('audit', '#auditLogTable tbody tr')
        shot(pg.locator('#view-audit .panel'), 'admin/05-audit.png')
        pg.select_option('#auditFilterUser', 'Автозаполнение')
        pg.wait_for_timeout(600)
        shot(pg.locator('#view-audit .panel'), 'admin/07-audit-autofill.png')
        shot(pg.locator('.sidebar-actions'), 'admin/08-sidebar-actions.png')
        pg.set_viewport_size({'width': 1440, 'height': 900})
        go('autofill', '.autofill-card')
        shot(pg.locator('#view-autofill .panel'), 'admin/09-autofill.png')
        ctx.close()

        # ── Режим просмотра (роль «Пользователь»)
        ctx = context()
        pg = ctx.new_page()
        login(pg, 'petrova', 'Demo-2026')
        pg.click('.nav-item[data-view="services"]')
        pg.wait_for_selector('#servicesTable tbody tr')
        pg.wait_for_timeout(500)
        shot(pg, 'user/21-readonly.png')
        ctx.close()

        # ── Телефон
        ctx = context(viewport={'width': 390, 'height': 844}, is_mobile=True, has_touch=True)
        pg = ctx.new_page()
        login(pg, 'admin', 'Test198!')
        shot(pg, 'user/22-mobile.png')
        pg.click('#sidebarToggle')
        pg.wait_for_timeout(400)
        shot(pg, 'user/23-mobile-menu.png')
        ctx.close()
        browser.close()
    jira.stop()
    optimize_png()
    print('Снимки:', sum(1 for _ in OUT.rglob('*.png')))


def optimize_png():
    """256 цветов вместо полноцветных снимков: интерфейс выглядит так же, а файлы и PDF — в 2–3 раза меньше."""
    from PIL import Image
    for path in sorted(OUT.rglob('*.png')):
        image = Image.open(path).convert('RGB')
        image.quantize(colors=256, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE).save(path, optimize=True)


if __name__ == '__main__':
    main()
