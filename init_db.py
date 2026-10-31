#!/usr/bin/env python3
"""Инициализация базы данных SQLite для Burndown Tracker.

Создаёт все таблицы и заполняет начальными данными:
- Пользователь admin / Test198! (роль: администратор)
- Проект «АИСУЗ» с предзаполненными эпиками и замерами
- Пустой проект «ИСУП»
"""

import sqlite3
import os
from datetime import datetime
from werkzeug.security import generate_password_hash

DB_PATH = os.environ.get('DB_PATH', os.path.join(os.path.dirname(os.path.abspath(__file__)), 'burndown.db'))


def create_jira_tables(conn):
    """Таблицы связки с Jira; создаются идемпотентно (новая база и миграция старой)."""
    conn.executescript('''
        -- Подключение проекта к Jira (токен хранится зашифрованным)
        CREATE TABLE IF NOT EXISTS jira_connections (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id INTEGER NOT NULL UNIQUE,
            base_url TEXT NOT NULL,
            instance_type TEXT NOT NULL,
            login TEXT,
            token_encrypted TEXT,
            accept_invalid_certs INTEGER DEFAULT 0,
            FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
        );
        -- Кнопки «Срез из Jira»: структура (необязательно) и ассоциации эпиков с задачами
        CREATE TABLE IF NOT EXISTS jira_snapshot_buttons (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            structure_id INTEGER,
            structure_name TEXT,
            sort_order INTEGER DEFAULT 0,
            FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS jira_snapshot_mappings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            button_id INTEGER NOT NULL,
            service_id INTEGER NOT NULL,
            issue_keys TEXT NOT NULL,
            include_subtree INTEGER DEFAULT 1,
            FOREIGN KEY (button_id) REFERENCES jira_snapshot_buttons(id) ON DELETE CASCADE,
            FOREIGN KEY (service_id) REFERENCES services(id) ON DELETE CASCADE,
            UNIQUE(button_id, service_id)
        );
        -- Автозаполнение по расписанию: одно расписание на кнопку
        CREATE TABLE IF NOT EXISTS jira_schedules (
            button_id INTEGER PRIMARY KEY,
            enabled INTEGER DEFAULT 0,
            days TEXT DEFAULT '1,2,3,4,5',
            run_time TEXT DEFAULT '09:00',
            timezone TEXT DEFAULT 'Europe/Moscow',
            overwrite INTEGER DEFAULT 1,
            last_run_date TEXT,
            last_run_at TEXT,
            last_status TEXT,
            last_message TEXT,
            FOREIGN KEY (button_id) REFERENCES jira_snapshot_buttons(id) ON DELETE CASCADE
        );
        -- Состав эпика по задачам Jira на день среза (последний срез за день заменяет предыдущий)
        CREATE TABLE IF NOT EXISTS jira_issue_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            service_id INTEGER NOT NULL,
            snap_date TEXT NOT NULL,
            issue_key TEXT NOT NULL,
            summary TEXT,
            status TEXT,
            category TEXT,
            remaining REAL,
            spent REAL,
            estimate REAL,
            container INTEGER DEFAULT 0,
            problems TEXT,
            captured_at TEXT,
            FOREIGN KEY (service_id) REFERENCES services(id) ON DELETE CASCADE,
            UNIQUE(service_id, snap_date, issue_key)
        );
        CREATE INDEX IF NOT EXISTS idx_jira_issue_snapshots ON jira_issue_snapshots(service_id, snap_date);
        -- Восстановление истории по журналу изменений Jira (фоновая задача с прогрессом)
        CREATE TABLE IF NOT EXISTS jira_history_jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            button_id INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'running',
            progress INTEGER DEFAULT 0,
            total INTEGER DEFAULT 0,
            message TEXT,
            params TEXT,
            created_by TEXT,
            created_at TEXT,
            updated_at TEXT,
            finished_at TEXT,
            FOREIGN KEY (button_id) REFERENCES jira_snapshot_buttons(id) ON DELETE CASCADE
        );
    ''')
    # колонки, добавленные после первого выпуска таблицы кнопок
    cols = {r[1] for r in conn.execute('PRAGMA table_info(jira_snapshot_buttons)')}
    if 'field_map' not in cols:
        conn.execute('ALTER TABLE jira_snapshot_buttons ADD COLUMN field_map TEXT')
    if 'last_success_at' not in cols:
        conn.execute('ALTER TABLE jira_snapshot_buttons ADD COLUMN last_success_at TEXT')
    # 1.8.0: синхронизация эпиков с Jira (настройки и итог последнего запуска)
    for col in ('epic_sync', 'epic_sync_at', 'epic_sync_message'):
        if col not in cols:
            conn.execute(f'ALTER TABLE jira_snapshot_buttons ADD COLUMN {col} TEXT')
    # срез восстановлен по журналу изменений (1), а не получен из Jira на этот день (0) — восстановленный можно заменить
    snap_cols = {r[1] for r in conn.execute('PRAGMA table_info(jira_issue_snapshots)')}
    if 'restored' not in snap_cols:
        conn.execute('ALTER TABLE jira_issue_snapshots ADD COLUMN restored INTEGER DEFAULT 0')


def create_notify_tables(conn):
    """Уведомления: настройки отправки, подписки пользователей на проекты, состояние для оповещений, очередь, журнал."""
    conn.executescript('''
        -- Настройки сервиса (SMTP, Telegram, адрес Burndown); секреты — в зашифрованном виде
        CREATE TABLE IF NOT EXISTS app_settings (
            key TEXT PRIMARY KEY,
            value TEXT
        );
        -- Подписка пользователя на проект: каналы, дайджест по расписанию, оповещения
        CREATE TABLE IF NOT EXISTS notification_subscriptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            project_id INTEGER NOT NULL,
            via_email INTEGER DEFAULT 1,
            via_telegram INTEGER DEFAULT 0,
            digest_enabled INTEGER DEFAULT 0,
            digest_days TEXT DEFAULT '1',
            digest_time TEXT DEFAULT '09:00',
            timezone TEXT DEFAULT 'Europe/Moscow',
            digest_pdf INTEGER DEFAULT 0,
            last_digest_date TEXT,
            last_digest_at TEXT,
            last_digest_status TEXT,
            last_digest_message TEXT,
            digest_snapshot TEXT,
            alerts_enabled INTEGER DEFAULT 0,
            alert_types TEXT DEFAULT 'autofill,status,chance,scope,problems',
            chance_threshold INTEGER DEFAULT 50,
            scope_threshold INTEGER DEFAULT 10,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
            FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE,
            UNIQUE(user_id, project_id)
        );
        -- Последнее известное состояние эпиков и этапов — для оповещений об ухудшениях
        CREATE TABLE IF NOT EXISTS notification_state (
            project_id INTEGER NOT NULL,
            entity TEXT NOT NULL,
            name TEXT,
            status TEXT,
            probability REAL,
            scope REAL,
            problems INTEGER,
            PRIMARY KEY (project_id, entity),
            FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS notification_marks (
            project_id INTEGER PRIMARY KEY,
            fingerprint TEXT,
            checked_at TEXT,
            FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
        );
        -- Очередь оповещений: доставляет фоновый планировщик
        CREATE TABLE IF NOT EXISTS notification_outbox (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            project_id INTEGER NOT NULL,
            kind TEXT NOT NULL,
            subject TEXT,
            body_text TEXT,
            body_html TEXT,
            status TEXT DEFAULT 'pending',
            created_at TEXT,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
            FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
        );
        -- Журнал отправок (для администратора)
        CREATE TABLE IF NOT EXISTS notification_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            username TEXT,
            project_id INTEGER,
            project_name TEXT,
            kind TEXT,
            channel TEXT,
            status TEXT,
            message TEXT,
            created_at TEXT
        );
    ''')
    cols = {r[1] for r in conn.execute('PRAGMA table_info(users)')}
    if 'telegram_chat_id' not in cols:
        conn.execute('ALTER TABLE users ADD COLUMN telegram_chat_id TEXT')
    if 'telegram_link_code' not in cols:
        conn.execute('ALTER TABLE users ADD COLUMN telegram_link_code TEXT')


def create_auth_tables(conn):
    """Неудачные попытки входа — для ограничения подбора пароля."""
    conn.execute('CREATE TABLE IF NOT EXISTS login_failures (username TEXT, ip TEXT, at TEXT)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_login_failures ON login_failures(username, ip, at)')


# Настройки проекта, добавленные в 1.7.0: календарь и единицы, диапазон прогноза, пороги выводов.
# Пустое значение (NULL) — значение по умолчанию (forecast.DEFAULT_SETTINGS).
SETTINGS_COLUMNS_V17 = [
    ('range_confidence', 'INTEGER'), ('workdays', 'INTEGER'), ('holidays', 'TEXT'), ('extra_workdays', 'TEXT'),
    ('units', 'TEXT'), ('hours_per_day', 'REAL'), ('stale_days', 'INTEGER'), ('scope_threshold', 'INTEGER'),
    ('slip_threshold', 'INTEGER'), ('chance_threshold', 'INTEGER'), ('insight_window_days', 'INTEGER'),
]


def ensure_org_calendar(conn):
    """Общий производственный календарь: при первом запуске — праздники РФ из встроенного набора (holidays_ru.py).
    Администратор может изменить или очистить его в разделе «Календарь»; повторно набор не подставляется."""
    if conn.execute("SELECT 1 FROM app_settings WHERE key = 'org_calendar'").fetchone():
        return
    import json
    import holidays_ru
    holidays, extra = holidays_ru.all_dates()
    conn.execute("INSERT INTO app_settings (key, value) VALUES ('org_calendar', ?)",
                 (json.dumps({'holidays': holidays, 'extraWorkdays': extra}),))


def upgrade_settings(conn):
    """Новые столбцы настроек проекта и личные настройки пользователя (вид дашборда). Существующие значения
    не меняются: новые столбцы пусты, а пустое значение означает «по умолчанию»."""
    cols = {r[1] for r in conn.execute('PRAGMA table_info(settings)')}
    for name, kind in SETTINGS_COLUMNS_V17:
        if name not in cols:
            conn.execute(f'ALTER TABLE settings ADD COLUMN {name} {kind}')
    user_cols = {r[1] for r in conn.execute('PRAGMA table_info(users)')}
    if 'prefs' not in user_cols:
        conn.execute('ALTER TABLE users ADD COLUMN prefs TEXT')


def _iso_date(value):
    """Дата из старых записей (например, «2026-7-9») → «2026-07-09»; нераспознанное — как есть."""
    text = str(value).strip()
    try:
        return datetime.strptime(text, '%Y-%m-%d').date().isoformat()
    except ValueError:
        return value


def normalize_dates(conn):
    """Даты замеров и целей — строго ГГГГ-ММ-ДД: прежние версии принимали «2026-7-9», из-за чего ломались
    сортировка и поиск замера на дату."""
    canon = "'[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'"
    for table, col in (('entries', 'date'), ('services', 'target_date'), ('stage_targets', 'target_date')):
        rows = conn.execute(f"SELECT id, {col} FROM {table} WHERE {col} IS NOT NULL AND {col} != '' "
                            f"AND {col} NOT GLOB {canon}").fetchall()
        for row_id, value in rows:
            fixed = _iso_date(value)
            if fixed != value:
                conn.execute(f'UPDATE {table} SET {col} = ? WHERE id = ?', (fixed, row_id))


def dedupe_entries(conn):
    """Один замер эпика на дату. Дубликаты (одновременное сохранение в прежних версиях) объединяются в самую
    позднюю запись: её пустые показатели дополняются значениями из более ранних."""
    dups = conn.execute('SELECT service_id, date FROM entries GROUP BY service_id, date HAVING COUNT(*) > 1').fetchall()
    for service_id, day in dups:
        rows = conn.execute('SELECT id, remaining, spent, estimate FROM entries WHERE service_id = ? AND date = ? '
                            'ORDER BY id DESC', (service_id, day)).fetchall()
        keep = list(rows[0])
        for row in rows[1:]:
            for i in (1, 2, 3):
                if keep[i] is None:
                    keep[i] = row[i]
        conn.execute('UPDATE entries SET remaining = ?, spent = ?, estimate = ? WHERE id = ?', (*keep[1:], keep[0]))
        conn.executemany('DELETE FROM entries WHERE id = ?', [(row[0],) for row in rows[1:]])


def create_indexes(conn):
    """Индексы для частых выборок; создаются идемпотентно. Замеры: даты приводятся к ГГГГ-ММ-ДД, дубликаты на дату
    объединяются, затем уникальный индекс не даёт создать второй замер эпика на ту же дату."""
    normalize_dates(conn)
    dedupe_entries(conn)
    conn.execute('CREATE UNIQUE INDEX IF NOT EXISTS ux_entries_service_date ON entries(service_id, date)')
    conn.execute('DROP INDEX IF EXISTS idx_entries_service_date')  # заменён уникальным
    conn.execute('CREATE INDEX IF NOT EXISTS idx_services_project ON services(project_id)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_audit_project ON audit_log(project_id)')


def init_db():
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)

    conn = sqlite3.connect(DB_PATH)
    conn.execute('PRAGMA foreign_keys = ON')
    c = conn.cursor()

    # ── Пользователи ──────────────────────────────────────────────
    c.execute('''
        CREATE TABLE users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            display_name TEXT,
            email TEXT,
            position TEXT,
            photo TEXT,
            role TEXT NOT NULL DEFAULT 'user',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # ── Проекты ──────────────────────────────────────────────────
    c.execute('''
        CREATE TABLE projects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            description TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # ── Связь пользователей и проектов (видимость + роль) ────────
    c.execute('''
        CREATE TABLE user_projects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            project_id INTEGER NOT NULL,
            role TEXT NOT NULL DEFAULT 'user',
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
            FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE,
            UNIQUE(user_id, project_id)
        )
    ''')

    # ── Эпики / бизнес-процессы ──────────────────────────────────
    c.execute('''
        CREATE TABLE services (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id INTEGER NOT NULL,
            etap TEXT,
            name TEXT,
            target_date TEXT,
            is_done INTEGER DEFAULT 0,
            stage_group TEXT,
            sort_order INTEGER DEFAULT 0,
            FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
        )
    ''')

    # ── Замеры по датам ──────────────────────────────────────────
    c.execute('''
        CREATE TABLE entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            service_id INTEGER NOT NULL,
            date TEXT NOT NULL,
            remaining REAL,
            spent REAL,
            estimate REAL,
            FOREIGN KEY (service_id) REFERENCES services(id) ON DELETE CASCADE
        )
    ''')

    # ── Целевые даты этапов ─────────────────────────────────────
    c.execute('''
        CREATE TABLE stage_targets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id INTEGER NOT NULL,
            stage_name TEXT NOT NULL,
            target_date TEXT,
            FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE,
            UNIQUE(project_id, stage_name)
        )
    ''')

    # ── Настройки проекта ───────────────────────────────────────
    c.execute('''
        CREATE TABLE settings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id INTEGER NOT NULL,
            forecast_method TEXT DEFAULT 'linear',
            risk_buffer_days INTEGER DEFAULT 3,
            burnrate_method TEXT DEFAULT 'all',
            burnrate_window_days INTEGER DEFAULT 14,
            FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE,
            UNIQUE(project_id)
        )
    ''')

    # ── Реестр изменений (audit log) ─────────────────────────────
    c.execute('''
        CREATE TABLE audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            username TEXT,
            project_id INTEGER,
            project_name TEXT,
            page TEXT,
            action TEXT,
            entity_type TEXT,
            entity_id INTEGER,
            entity_name TEXT,
            field_name TEXT,
            old_value TEXT,
            new_value TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # ── Начальный пользователь admin ────────────────────────────
    c.execute('''
        INSERT INTO users (username, password_hash, display_name, role)
        VALUES (?, ?, 'Администратор', 'admin')
    ''', ('admin', generate_password_hash('Test198!')))

    # ── Проект 1: АИСУЗ ─────────────────────────────────────────
    c.execute('''
        INSERT INTO projects (id, name, description)
        VALUES (1, 'АИСУЗ', 'Автоматизированная информационная система управления закупками')
    ''')

    # Настройки АИСУЗ
    c.execute('''
        INSERT INTO settings (project_id, forecast_method, risk_buffer_days, burnrate_method, burnrate_window_days)
        VALUES (1, 'linear', 3, 'all', 14)
    ''')

    # Целевые даты этапов АИСУЗ
    stage_targets = [
        (1, 'Этап 3', '2026-08-24'),
        (1, 'Этап 4', '2026-08-31'),
    ]
    c.executemany('''
        INSERT INTO stage_targets (project_id, stage_name, target_date)
        VALUES (?, ?, ?)
    ''', stage_targets)

    # Эпики АИСУЗ
    services = [
        (1, 1, 'Этап 4', 'Сервис "Работа с жалобами"', '2026-08-31', 1),
        (2, 1, 'Этап 4', 'Сервис "Комплект ценообразующей документации (КЦД)"', '2026-08-12', 2),
        (3, 1, 'Этап 3.2', 'КСБО', '2026-08-24', 3),
        (4, 1, 'Этап 3.2', 'ЦЗК', '2026-08-25', 4),
        (5, 1, 'Этап 3.2', 'ЕИО', '2026-08-26', 5),
        (6, 1, 'Этап 3.1', 'Планирование закупок', '2026-08-25', 6),
    ]
    c.executemany('''
        INSERT INTO services (id, project_id, etap, name, target_date, sort_order)
        VALUES (?, ?, ?, ?, ?, ?)
    ''', services)

    # Замеры АИСУЗ
    entries = [
        (1, 1, '2026-07-17', 509, 186, 150),
        (2, 1, '2026-07-24', 124, 227, 152),
        (3, 1, '2026-07-28', 349, 255, 401),
        (4, 2, '2026-07-17', 179, 46, 5),
        (5, 2, '2026-07-24', 85.17, 73, 340.1),
        (6, 2, '2026-07-28', 216.12, 192.5, 325.12),
        (7, 3, '2026-07-17', 494.65, 868.5, 1106.15),
        (8, 3, '2026-07-24', 131.2, 482.5, 480.2),
        (9, 3, '2026-07-28', 113.17, 559, 523.18),
        (10, 4, '2026-07-17', 219.75, 619.75, 643),
        (11, 4, '2026-07-24', 399.58, 3421.25, 1902.25),
        (12, 4, '2026-07-28', 424.58, 3491.25, 1954.25),
        (13, 5, '2026-07-17', 470.5, 1048.25, 1359.5),
        (14, 5, '2026-07-24', 281.7, 2146.5, 1559.22),
        (15, 5, '2026-07-28', 275.72, 2248.5, 1660.23),
        (16, 6, '2026-07-17', 55, 300.25, 309),
        (17, 6, '2026-07-24', 85.15, 328, 377),
        (18, 6, '2026-07-28', 84.17, 364, 410.02),
        (19, 1, '2026-07-29', 346, 258, 401),
        (20, 2, '2026-07-29', 214.34, 259, 327),
        (21, 3, '2026-07-29', 109, 594, 521),
        (22, 4, '2026-07-29', 201, 2000.75, 1308.7),
        (23, 5, '2026-07-29', 278, 2311, 1686),
        (24, 6, '2026-07-29', 86, 368, 416),
    ]
    c.executemany('''
        INSERT INTO entries (id, service_id, date, remaining, spent, estimate)
        VALUES (?, ?, ?, ?, ?, ?)
    ''', entries)

    # ── Проект 2: ИСУП (пустой) ─────────────────────────────────
    c.execute('''
        INSERT INTO projects (id, name, description)
        VALUES (2, 'ИСУП', 'Информационная система управления проектами')
    ''')

    # Настройки ИСУП (по умолчанию)
    c.execute('''
        INSERT INTO settings (project_id)
        VALUES (2)
    ''')

    create_jira_tables(conn)
    create_notify_tables(conn)
    create_auth_tables(conn)
    upgrade_settings(conn)
    ensure_org_calendar(conn)
    create_indexes(conn)

    conn.commit()
    conn.close()
    print(f'База данных инициализирована: {DB_PATH}')
    print('Пользователь по умолчанию: admin / Test198!')
    print('Созданы проекты: АИСУЗ (с данными), ИСУП (пустой)')


if __name__ == '__main__':
    init_db()
