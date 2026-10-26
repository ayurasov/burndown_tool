"""Скриншоты для руководства пользователя: демо-база init_db + пользователи с разными ролями.

Приложение должно быть запущено на BASE (по умолчанию http://localhost:5055) с демо-базой, см. README.md рядом.
"""
import os
import requests
from playwright.sync_api import sync_playwright

BASE = os.environ.get('BURNDOWN_URL', 'http://localhost:5055')
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'img')
os.makedirs(OUT, exist_ok=True)

# Подготовка данных: пользователи с разными ролями
s = requests.Session()
s.post(BASE + '/api/auth/login', json={'username': 'admin', 'password': 'Test198!'})
s.put(BASE + '/api/profile', json={'displayName': 'Администратор', 'email': 'admin@example.ru', 'position': 'Администратор системы'})
users = {u['username']: u['id'] for u in s.get(BASE + '/api/users').json()}
for login, name, pos in [('ivanov', 'Иванов Сергей', 'Руководитель проекта'), ('petrova', 'Петрова Анна', 'Аналитик')]:
    if login not in users:
        users[login] = s.post(BASE + '/api/users', json={'username': login, 'password': 'Demo-2026', 'displayName': name,
                                                        'email': login + '@example.ru', 'position': pos, 'role': 'user'}).json()['id']
s.post(f"{BASE}/api/users/{users['ivanov']}/projects", json={'projectId': 1, 'role': 'manager'})
s.post(f"{BASE}/api/users/{users['petrova']}/projects", json={'projectId': 1, 'role': 'user'})
# Несколько записей в реестре изменений
s.put(BASE + '/api/projects/1/stage_targets', json={'stageName': 'Этап 4', 'targetDate': '2026-08-31'})
s.put(BASE + '/api/projects/1/stage_targets', json={'stageName': 'Этап 3', 'targetDate': '2026-08-28'})
s.put(BASE + '/api/entries/24', json={'date': '2026-07-29', 'remaining': 86, 'spent': 370, 'estimate': 416})


def shot(target, name, **kw):
    target.screenshot(path=os.path.join(OUT, name + '.png'), **kw)


with sync_playwright() as p:
    b = p.chromium.launch(executable_path=os.environ.get('CHROMIUM_PATH') or None, args=['--lang=ru'], env={**os.environ, 'LANG': 'ru_RU.UTF-8', 'LANGUAGE': 'ru'})
    ctx = b.new_context(viewport={'width': 1440, 'height': 900}, device_scale_factor=2, color_scheme='light', locale='ru-RU')
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

    # Вход
    pg.goto(BASE + '/login')
    pg.wait_for_timeout(300)
    shot(pg, '01-login')
    pg.fill('#username', 'admin')
    pg.fill('#password', 'Test198!')
    pg.click('button[type=submit]')
    pg.wait_for_selector('#summaryTable tbody tr')
    pg.wait_for_timeout(800)

    # Дашборд: общий вид, панели
    shot(pg, '02-dashboard')
    shot(pg.locator('.sidebar'), '03-sidebar')
    shot(pg.locator('#view-dashboard .quick-filter'), '04-quick-filter')
    shot(pg.locator('#kpiGrid'), '05-kpi')
    epic_panel = pg.locator('#view-dashboard > .panel').nth(1)
    shot(epic_panel, '06-epic-chart')
    # тултип на фактическом значении (точка 24.07)
    pt = pg.evaluate("(() => { const c = Chart.getChart('chartRemaining'); const p = c.getDatasetMeta(0).data[1]; const r = c.canvas.getBoundingClientRect(); return {x: r.left + p.x, y: r.top + p.y}; })()")
    pg.mouse.move(pt['x'], pt['y'])
    pg.wait_for_timeout(400)
    shot(epic_panel, '07-epic-tooltip')
    pg.mouse.move(5, 5)
    shot(pg.locator('#allEpicsBlock'), '08-all-epics')
    shot(pg.locator('#summaryTable').locator('xpath=ancestor::div[contains(@class,"panel")]'), '09-summary')
    shot(pg.locator('#burnRateTable').locator('xpath=ancestor::div[contains(@class,"panel")]'), '10-burnrate')

    # Свод по этапам
    go('stages', '#stageSummaryTable tbody tr')
    shot(pg, '11-stages')
    shot(pg.locator('#stageChartsContainer .panel').first, '12-stage-chart')
    shot(pg.locator('#stageSummaryTable').locator('xpath=ancestor::div[contains(@class,"panel")]'), '13-stage-summary')

    # Данные по датам
    go('data', '#entriesTable tbody tr')
    shot(pg.locator('#view-data .panel'), '14-data')
    pg.click('#btnBulkMeasure')
    pg.fill('#bulkDate', '2026-07-30')
    pg.click('#bulkFillPrev')
    pg.fill('#bulkTable tr[data-sid="1"] input[data-field="remaining"]', '330')
    pg.click('#confirmTitle')  # снимаем фокус с поля, чтобы на снимке не было стрелок
    pg.wait_for_timeout(300)
    shot(pg.locator('#confirmModal .modal'), '15-bulk')
    pg.keyboard.press('Escape')

    # Эпики
    go('services', '#servicesTable tbody tr')
    shot(pg.locator('#view-services .panel'), '16-services')

    # Настройки
    go('settings', '#settingsGrid select')
    shot(pg.locator('#view-settings'), '17-settings')

    # Пользователи
    go('users', '#usersTable tbody tr')
    pg.click('#usersTable tr:has-text("petrova") td:first-child')
    pg.wait_for_selector('#userProjectsMgmt select[data-pid="1"]')
    pg.wait_for_timeout(300)
    shot(pg, '18-users')
    pg.click('#usersTable tr:has-text("ivanov") [data-action="edit"]')
    pg.wait_for_timeout(300)
    shot(pg.locator('#confirmModal .modal'), '19-user-edit')
    pg.keyboard.press('Escape')

    # Проекты, реестр
    go('projects', '#projectsMgmtTable tbody tr')
    shot(pg.locator('#view-projects .panel'), '20-projects')
    go('audit', '#auditLogTable tbody tr')
    shot(pg.locator('#view-audit .panel'), '21-audit', )

    # Профиль
    pg.click('#userInfo')
    pg.wait_for_timeout(300)
    shot(pg.locator('#confirmModal .modal'), '22-profile')
    pg.keyboard.press('Escape')

    # Подтверждение удаления
    go('data', '#entriesTable tbody tr')
    pg.click('#entriesTable tbody tr:last-child [data-action="delete"]')
    pg.wait_for_timeout(300)
    shot(pg.locator('#confirmModal .modal'), '23-confirm')
    pg.keyboard.press('Escape')

    # Тёмная тема
    go('dashboard', '#summaryTable tbody tr')
    pg.click('#themeToggle')
    pg.wait_for_timeout(600)
    shot(pg, '24-dark')
    pg.click('#themeToggle')
    pg.wait_for_timeout(300)
    ctx.close()

    # Режим просмотра (роль «Пользователь»)
    ctx = b.new_context(viewport={'width': 1440, 'height': 900}, device_scale_factor=2, locale='ru-RU')
    pg = ctx.new_page()
    pg.goto(BASE + '/login')
    pg.fill('#username', 'petrova')
    pg.fill('#password', 'Demo-2026')
    pg.click('button[type=submit]')
    pg.wait_for_selector('#summaryTable tbody tr')
    pg.click('.nav-item[data-view="services"]')
    pg.wait_for_selector('#servicesTable tbody tr')
    pg.wait_for_timeout(500)
    shot(pg, '25-readonly')
    ctx.close()

    # Мобильная версия
    ctx = b.new_context(viewport={'width': 390, 'height': 844}, device_scale_factor=2, is_mobile=True, has_touch=True, locale='ru-RU')
    pg = ctx.new_page()
    pg.goto(BASE + '/login')
    pg.fill('#username', 'admin')
    pg.fill('#password', 'Test198!')
    pg.click('button[type=submit]')
    pg.wait_for_selector('#summaryTable tbody tr')
    pg.wait_for_timeout(800)
    shot(pg, '26-mobile')
    pg.click('#sidebarToggle')
    pg.wait_for_timeout(400)
    shot(pg, '27-mobile-menu')
    ctx.close()
    b.close()
print('done', sorted(os.listdir(OUT)))
