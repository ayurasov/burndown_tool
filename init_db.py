#!/usr/bin/env python3
"""Инициализация базы данных SQLite для Burndown Tracker.

Создаёт все таблицы и заполняет начальными данными:
- Пользователь admin / Test198! (роль: администратор)
- Проект «АИСУЗ» с предзаполненными эпиками и замерами
- Пустой проект «ИСУП»
"""

import sqlite3
import os
from werkzeug.security import generate_password_hash

DB_PATH = os.environ.get('DB_PATH', 'burndown.db')


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

    conn.commit()
    conn.close()
    print(f'База данных инициализирована: {DB_PATH}')
    print('Пользователь по умолчанию: admin / Test198!')
    print('Созданы проекты: АИСУЗ (с данными), ИСУП (пустой)')


if __name__ == '__main__':
    init_db()
