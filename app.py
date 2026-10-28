#!/usr/bin/env python3
"""Burndown Tracker — Flask backend с SQLite.

Запуск для разработки:
    python app.py

Запуск в продакшене (через gunicorn):
    gunicorn -w 4 -b 0.0.0.0:5000 app:app
"""

import os
import re
import math
import secrets
import sqlite3
import json
import logging
import threading
import time
from functools import wraps
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from urllib.parse import quote

from flask import (
    Flask, request, jsonify, session, render_template,
    redirect, g, Response
)
from werkzeug.security import generate_password_hash, check_password_hash

from init_db import create_indexes, create_jira_tables, create_notify_tables
import forecast as fc
import jira_integration as jira
import notify

app = Flask(__name__, static_folder='static', template_folder='templates')
DB_PATH = os.environ.get('DB_PATH', os.path.join(os.path.dirname(os.path.abspath(__file__)), 'burndown.db'))


def load_secret_key():
    """SECRET_KEY из окружения; иначе случайный ключ, сохраняемый рядом с БД (общий для всех воркеров)."""
    env_key = os.environ.get('SECRET_KEY')
    if env_key:
        return env_key
    key_path = os.path.join(os.path.dirname(os.path.abspath(DB_PATH)), '.secret_key')
    try:
        with open(key_path, encoding='utf-8') as f:
            key = f.read().strip()
            if key:
                return key
    except FileNotFoundError:
        pass
    key = secrets.token_hex(32)
    try:
        fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(key)
    except FileExistsError:
        # другой воркер успел создать файл — используем его ключ
        with open(key_path, encoding='utf-8') as f:
            return f.read().strip()
    return key


app.secret_key = load_secret_key()
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    # при работе по HTTPS включите: SESSION_COOKIE_SECURE=1
    SESSION_COOKIE_SECURE=os.environ.get('SESSION_COOKIE_SECURE', '').lower() in ('1', 'true', 'yes'),
)


def migrate_db():
    """Дополняет таблицу services полями is_done и stage_group для старых баз и создаёт индексы."""
    if not os.path.exists(DB_PATH):
        return
    conn = sqlite3.connect(DB_PATH)
    try:
        cols = [r[1] for r in conn.execute('PRAGMA table_info(services)')]
        if 'is_done' not in cols:
            conn.execute('ALTER TABLE services ADD COLUMN is_done INTEGER DEFAULT 0')
        if 'stage_group' not in cols:
            conn.execute('ALTER TABLE services ADD COLUMN stage_group TEXT')
        create_jira_tables(conn)
        create_notify_tables(conn)
        create_indexes(conn)
        conn.commit()
    finally:
        conn.close()


migrate_db()


# ─── Database helpers ───────────────────────────────────────────

def get_db():
    if 'db' not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute('PRAGMA foreign_keys = ON')
    return g.db


@app.teardown_appcontext
def close_db(error):
    db = g.pop('db', None)
    if db is not None:
        db.close()


NO_STAGE = 'Без этапа'


def stage_key(s):
    """Ключ группировки для «Свода по этапам»: явная группа эпика или этап без подсетапа."""
    try:
        sg = (s['stage_group'] or '').strip()
    except (KeyError, IndexError):
        sg = ''
    if sg:
        return sg
    etap = (s['etap'] or '').strip()
    m = re.match(r'^(Этап\s+\d+)(\.\d+)?$', etap, re.IGNORECASE)
    return m.group(1) if m else (etap or NO_STAGE)


class ValidationError(ValueError):
    pass


def parse_hours(value, field):
    """Часы: пусто → None; иначе конечное число ≥ 0."""
    if value is None or value == '':
        return None
    if isinstance(value, bool):
        raise ValidationError(f'Поле «{field}»: ожидается число')
    try:
        num = float(value)
    except (TypeError, ValueError):
        raise ValidationError(f'Поле «{field}»: ожидается число')
    if not math.isfinite(num) or num < 0:
        raise ValidationError(f'Поле «{field}»: число должно быть не меньше 0')
    return num


def parse_date(value, field, required=True):
    """Дата в формате ГГГГ-ММ-ДД; пусто допустимо, если required=False."""
    if value is None or value == '':
        if required:
            raise ValidationError(f'Укажите поле «{field}»')
        return value
    try:
        datetime.strptime(str(value), '%Y-%m-%d')
    except ValueError:
        raise ValidationError(f'Поле «{field}»: некорректная дата')
    return str(value)


@app.errorhandler(ValidationError)
def handle_validation_error(err):
    return jsonify({'error': str(err)}), 400


@app.errorhandler(jira.JiraError)
def handle_jira_error(err):
    return jsonify({'error': str(err)}), 502


def text_value(value):
    """Строковое поле запроса: None и нестроковые значения приводятся к строке без пробелов по краям."""
    return '' if value is None else str(value).strip()


MAX_PHOTO_CHARS = 2_200_000  # ~1,5 МБ изображения в base64 (как ограничение в интерфейсе)


def parse_photo(value):
    """Фото — data:image/... URL не больше лимита; пустая строка удаляет фото."""
    if value is None or value == '':
        return value
    if not isinstance(value, str) or not value.startswith('data:image/'):
        raise ValidationError('Фото должно быть изображением')
    if len(value) > MAX_PHOTO_CHARS:
        raise ValidationError('Файл слишком большой (макс. 1,5 МБ)')
    return value


def text_change(old, new, field=None):
    """Изменение текстового поля для реестра: пустое значение и None считаются одинаковыми,
    в реестр пишется None вместо строки «None»."""
    old_s = '' if old is None else str(old)
    new_s = '' if new is None else str(new)
    if field is None:
        return old_s != new_s
    return {'field': field, 'old': old_s or None, 'new': new_s or None}


HOUR_FIELDS = {'remaining': 'Остаток', 'spent': 'Затрачено', 'estimate': 'Оценка'}
SETTINGS_FIELDS = [
    ('forecastMethod', 'forecast_method'),
    ('riskBufferDays', 'risk_buffer_days'),
    ('burnrateMethod', 'burnrate_method'),
    ('burnrateWindowDays', 'burnrate_window_days'),
]


def parse_entry_values(data):
    return {f: parse_hours(data.get(f), label) for f, label in HOUR_FIELDS.items()}


# ─── Auth helpers ────────────────────────────────────────────────

def get_current_user():
    if 'user_id' not in session:
        return None
    db = get_db()
    return db.execute('SELECT * FROM users WHERE id = ?', (session['user_id'],)).fetchone()


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        # сессия удалённого пользователя больше не действует
        if not get_current_user():
            session.clear()
            return jsonify({'error': 'Не авторизован'}), 401
        return f(*args, **kwargs)
    return decorated


def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        user = get_current_user()
        if not user:
            session.clear()
            return jsonify({'error': 'Не авторизован'}), 401
        if user['role'] != 'admin':
            return jsonify({'error': 'Доступ запрещён. Требуется роль администратора.'}), 403
        return f(*args, **kwargs)
    return decorated


def user_project_role(user_id, project_id):
    """Возвращает роль пользователя в проекте: 'admin', 'manager', 'user' или None."""
    db = get_db()
    user = db.execute('SELECT * FROM users WHERE id = ?', (user_id,)).fetchone()
    if user and user['role'] == 'admin':
        return 'admin'
    up = db.execute(
        'SELECT * FROM user_projects WHERE user_id = ? AND project_id = ?',
        (user_id, project_id)
    ).fetchone()
    if up:
        return up['role']
    return None


def can_edit_project(user_id, project_id):
    role = user_project_role(user_id, project_id)
    return role in ('admin', 'manager')


def audit_log(page, action, entity_type=None, entity_id=None, project_id=None, changes=None, entity_name=None,
              commit=True, actor=None):
    """Log a change to audit_log table. changes is a list of {field, old, new} dicts.

    actor — имя автора для фоновых операций без сессии пользователя (например, автозаполнение).
    """
    db = get_db()
    uid = None if actor else session.get('user_id')
    username = actor or 'system'
    if uid:
        u = db.execute('SELECT username FROM users WHERE id = ?', (uid,)).fetchone()
        if u:
            username = u['username']
    project_name = None
    if project_id:
        p = db.execute('SELECT name FROM projects WHERE id = ?', (project_id,)).fetchone()
        if p:
            project_name = p['name']
    if not changes:
        changes = [{'field': None, 'old': None, 'new': None}]
    for ch in changes:
        db.execute('''INSERT INTO audit_log
            (user_id, username, project_id, project_name, page, action,
             entity_type, entity_id, entity_name, field_name, old_value, new_value)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
            (uid, username, project_id, project_name, page, action,
             entity_type, entity_id, entity_name, ch.get('field'), ch.get('old'), ch.get('new')))
    if commit:
        db.commit()


def can_view_project(user_id, project_id):
    return user_project_role(user_id, project_id) is not None


# ─── Page routes ─────────────────────────────────────────────────

@app.route('/')
def index():
    if not get_current_user():
        session.clear()
        return redirect('/login')
    return render_template('index.html')


@app.route('/login')
def login_page():
    return render_template('login.html')


# ─── API: Auth ───────────────────────────────────────────────────

@app.route('/api/auth/login', methods=['POST'])
def api_login():
    data = request.get_json() or {}
    username = text_value(data.get('username'))
    password = data.get('password') or ''
    if not username or not password or not isinstance(password, str):
        return jsonify({'error': 'Введите логин и пароль'}), 400

    db = get_db()
    user = db.execute('SELECT * FROM users WHERE username = ?', (username,)).fetchone()
    if user and check_password_hash(user['password_hash'], password):
        session.clear()
        session['user_id'] = user['id']
        return jsonify({
            'success': True,
            'user': {
                'id': user['id'],
                'username': user['username'],
                'displayName': user['display_name'],
                'email': user['email'] or '',
                'position': user['position'] or '',
                'photo': user['photo'] or '',
                'role': user['role']
            }
        })
    return jsonify({'error': 'Неверный логин или пароль'}), 401


@app.route('/api/auth/logout', methods=['POST'])
def api_logout():
    session.clear()
    return jsonify({'success': True})


@app.route('/api/auth/me')
def api_me():
    user = get_current_user()
    if not user:
        return jsonify({'error': 'Не авторизован'}), 401
    return jsonify({
        'user': {
            'id': user['id'],
            'username': user['username'],
            'displayName': user['display_name'],
            'email': user['email'] or '',
            'position': user['position'] or '',
            'photo': user['photo'] or '',
            'role': user['role']
        }
    })


# ─── API: Projects ──────────────────────────────────────────────

@app.route('/api/projects')
@login_required
def api_get_projects():
    db = get_db()
    uid = session['user_id']
    user = db.execute('SELECT * FROM users WHERE id = ?', (uid,)).fetchone()

    if user['role'] == 'admin':
        projects = db.execute('SELECT * FROM projects ORDER BY id').fetchall()
    else:
        projects = db.execute('''
            SELECT p.* FROM projects p
            JOIN user_projects up ON p.id = up.project_id
            WHERE up.user_id = ?
            ORDER BY p.id
        ''', (uid,)).fetchall()

    result = []
    for p in projects:
        role = user_project_role(uid, p['id'])
        result.append({
            'id': p['id'],
            'name': p['name'],
            'description': p['description'],
            'role': role
        })
    return jsonify(result)


@app.route('/api/projects', methods=['POST'])
@admin_required
def api_create_project():
    data = request.get_json() or {}
    name = text_value(data.get('name'))
    if not name:
        return jsonify({'error': 'Укажите название проекта'}), 400

    db = get_db()
    cur = db.execute(
        'INSERT INTO projects (name, description) VALUES (?, ?)',
        (name, data.get('description', ''))
    )
    project_id = cur.lastrowid
    # Создаём настройки по умолчанию
    db.execute('INSERT INTO settings (project_id) VALUES (?)', (project_id,))
    db.commit()
    audit_log('projects', 'create', 'project', project_id, project_id, [{'field': 'name', 'old': None, 'new': name}])
    return jsonify({'id': project_id, 'name': name, 'description': data.get('description', '')})


@app.route('/api/projects/<int:pid>', methods=['PUT'])
@admin_required
def api_update_project(pid):
    data = request.get_json() or {}
    db = get_db()
    project = db.execute('SELECT * FROM projects WHERE id = ?', (pid,)).fetchone()
    if not project:
        return jsonify({'error': 'Проект не найден'}), 404
    name = text_value(data.get('name', project['name']))
    description = data.get('description', project['description'])
    if not name:
        return jsonify({'error': 'Укажите название проекта'}), 400
    changes = []
    if name != project['name']:
        changes.append({'field': 'name', 'old': project['name'], 'new': name})
    if description != project['description']:
        changes.append({'field': 'description', 'old': project['description'], 'new': description})
    db.execute('UPDATE projects SET name = ?, description = ? WHERE id = ?', (name, description, pid))
    db.commit()
    if changes:
        audit_log('projects', 'update', 'project', pid, pid, changes)
    return jsonify({'id': pid, 'name': name, 'description': description})


@app.route('/api/projects/<int:pid>', methods=['DELETE'])
@admin_required
def api_delete_project(pid):
    db = get_db()
    project = db.execute('SELECT * FROM projects WHERE id = ?', (pid,)).fetchone()
    if not project:
        return jsonify({'error': 'Проект не найден'}), 404
    audit_log('projects', 'delete', 'project', pid, pid, [{'field': 'name', 'old': project['name'], 'new': None}],
              entity_name=project['name'])
    db.execute('DELETE FROM projects WHERE id = ?', (pid,))
    db.commit()
    return jsonify({'success': True})


def project_payload(db, pid):
    """Эпики, замеры, цели этапов и настройки проекта в формате фронтенда (camelCase)."""
    services = db.execute('SELECT * FROM services WHERE project_id = ? ORDER BY sort_order, id', (pid,)).fetchall()
    entries = db.execute(
        'SELECT e.* FROM entries e JOIN services s ON s.id = e.service_id '
        'WHERE s.project_id = ? ORDER BY e.date, e.id', (pid,)).fetchall()
    stage_targets = db.execute('SELECT * FROM stage_targets WHERE project_id = ?', (pid,)).fetchall()
    settings = db.execute('SELECT * FROM settings WHERE project_id = ?', (pid,)).fetchone()
    return {
        'services': [{
            'id': s['id'], 'project_id': s['project_id'],
            'etap': s['etap'], 'name': s['name'],
            'targetDate': s['target_date'], 'sortOrder': s['sort_order'] if s['sort_order'] is not None else 0,
            'isDone': bool(s['is_done']), 'stageGroup': s['stage_group'] or ''
        } for s in services],
        'entries': [{
            'id': e['id'], 'service_id': e['service_id'],
            'date': e['date'], 'remaining': e['remaining'],
            'spent': e['spent'], 'estimate': e['estimate']
        } for e in entries],
        'stageTargets': {r['stage_name']: r['target_date'] for r in stage_targets},
        # пустые значения в БД не передаём — фронтенд подставит значения по умолчанию
        'settings': {key: settings[col] for key, col in SETTINGS_FIELDS if settings and settings[col] is not None},
    }


@app.route('/api/projects/<int:pid>/data')
@login_required
def api_get_project_data(pid):
    if not can_view_project(session['user_id'], pid):
        return jsonify({'error': 'Нет доступа к проекту'}), 403
    return jsonify(project_payload(get_db(), pid))


# ─── API: Services (epics) ───────────────────────────────────────

@app.route('/api/projects/<int:pid>/services', methods=['POST'])
@login_required
def api_create_service(pid):
    if not can_edit_project(session['user_id'], pid):
        return jsonify({'error': 'Нет прав на редактирование'}), 403
    data = request.get_json() or {}
    parse_date(data.get('targetDate'), 'Целевая дата', required=False)
    db = get_db()
    max_order = db.execute('SELECT COALESCE(MAX(sort_order), 0) FROM services WHERE project_id = ?', (pid,)).fetchone()[0]
    cur = db.execute(
        'INSERT INTO services (project_id, etap, name, target_date, sort_order, is_done, stage_group) VALUES (?, ?, ?, ?, ?, ?, ?)',
        (pid, data.get('etap', ''), data.get('name', ''), data.get('targetDate', ''), max_order + 1,
         1 if data.get('isDone') else 0, data.get('stageGroup', ''))
    )
    db.commit()
    audit_log('services', 'create', 'service', cur.lastrowid, pid,
              [{'field': 'name', 'old': None, 'new': data.get('name', '')}], entity_name=data.get('name', ''))
    return jsonify({'id': cur.lastrowid})


@app.route('/api/services/<int:sid>', methods=['PUT'])
@login_required
def api_update_service(sid):
    db = get_db()
    svc = db.execute('SELECT * FROM services WHERE id = ?', (sid,)).fetchone()
    if not svc:
        return jsonify({'error': 'Не найдено'}), 404
    if not can_edit_project(session['user_id'], svc['project_id']):
        return jsonify({'error': 'Нет прав на редактирование'}), 403
    data = request.get_json() or {}
    if 'targetDate' in data:
        parse_date(data['targetDate'], 'Целевая дата', required=False)
    is_done = 1 if data.get('isDone', bool(svc['is_done'])) else 0
    stage_group = data.get('stageGroup', svc['stage_group'])
    changes = []
    if data.get('etap', svc['etap']) != svc['etap']:
        changes.append({'field': 'etap', 'old': svc['etap'], 'new': data.get('etap')})
    if data.get('name', svc['name']) != svc['name']:
        changes.append({'field': 'name', 'old': svc['name'], 'new': data.get('name')})
    if data.get('targetDate', svc['target_date']) != svc['target_date']:
        changes.append({'field': 'targetDate', 'old': svc['target_date'], 'new': data.get('targetDate')})
    if is_done != (1 if svc['is_done'] else 0):
        changes.append({'field': 'isDone', 'old': bool(svc['is_done']), 'new': bool(is_done)})
    if (stage_group or '') != (svc['stage_group'] or ''):
        changes.append({'field': 'stageGroup', 'old': svc['stage_group'], 'new': stage_group})
    db.execute(
        'UPDATE services SET etap = ?, name = ?, target_date = ?, is_done = ?, stage_group = ? WHERE id = ?',
        (data.get('etap', svc['etap']), data.get('name', svc['name']),
         data.get('targetDate', svc['target_date']), is_done, stage_group, sid)
    )
    db.commit()
    if changes:
        audit_log('services', 'update', 'service', sid, svc['project_id'], changes, entity_name=svc['name'])
    return jsonify({'success': True})


@app.route('/api/services/<int:sid>', methods=['DELETE'])
@login_required
def api_delete_service(sid):
    db = get_db()
    svc = db.execute('SELECT * FROM services WHERE id = ?', (sid,)).fetchone()
    if not svc:
        return jsonify({'error': 'Не найдено'}), 404
    if not can_edit_project(session['user_id'], svc['project_id']):
        return jsonify({'error': 'Нет прав на редактирование'}), 403
    db.execute('DELETE FROM services WHERE id = ?', (sid,))
    db.commit()
    audit_log('services', 'delete', 'service', sid, svc['project_id'],
              [{'field': 'name', 'old': svc['name'], 'new': None}], entity_name=svc['name'])
    return jsonify({'success': True})


# ─── API: Reorder services ───────────────────────────────────────

@app.route('/api/projects/<int:pid>/services/reorder', methods=['POST'])
@login_required
def api_reorder_services(pid):
    if not can_edit_project(session['user_id'], pid):
        return jsonify({'error': 'Нет прав на редактирование'}), 403
    data = request.get_json() or {}
    orders = data.get('orders', [])  # list of {id, sortOrder}
    if not isinstance(orders, list) or not all(
            isinstance(o, dict) and isinstance(o.get('id'), int) and isinstance(o.get('sortOrder'), int) for o in orders):
        return jsonify({'error': 'Некорректный порядок эпиков'}), 400
    db = get_db()
    for item in orders:
        db.execute('UPDATE services SET sort_order = ? WHERE id = ? AND project_id = ?',
                    (item['sortOrder'], item['id'], pid))
    db.commit()
    return jsonify({'success': True})


# ─── API: Entries ────────────────────────────────────────────────

@app.route('/api/services/<int:sid>/entries', methods=['POST'])
@login_required
def api_create_entry(sid):
    db = get_db()
    svc = db.execute('SELECT * FROM services WHERE id = ?', (sid,)).fetchone()
    if not svc:
        return jsonify({'error': 'Не найдено'}), 404
    if not can_edit_project(session['user_id'], svc['project_id']):
        return jsonify({'error': 'Нет прав на редактирование'}), 403
    data = request.get_json() or {}
    date = parse_date(data.get('date'), 'Дата замера')
    vals = parse_entry_values(data)
    if db.execute('SELECT 1 FROM entries WHERE service_id = ? AND date = ?', (sid, date)).fetchone():
        return jsonify({'error': 'Замер на эту дату уже есть'}), 400
    cur = db.execute(
        'INSERT INTO entries (service_id, date, remaining, spent, estimate) VALUES (?, ?, ?, ?, ?)',
        (sid, date, vals['remaining'], vals['spent'], vals['estimate'])
    )
    db.commit()
    audit_log('data', 'create', 'entry', cur.lastrowid, svc['project_id'],
              [{'field': 'date', 'old': None, 'new': data.get('date')}], entity_name=svc['name'])
    return jsonify({'id': cur.lastrowid})


@app.route('/api/entries/<int:eid>', methods=['PUT'])
@login_required
def api_update_entry(eid):
    db = get_db()
    entry = db.execute('SELECT * FROM entries WHERE id = ?', (eid,)).fetchone()
    if not entry:
        return jsonify({'error': 'Не найдено'}), 404
    svc = db.execute('SELECT * FROM services WHERE id = ?', (entry['service_id'],)).fetchone()
    if not svc or not can_edit_project(session['user_id'], svc['project_id']):
        return jsonify({'error': 'Нет прав на редактирование'}), 403
    data = request.get_json() or {}
    date = parse_date(data.get('date', entry['date']), 'Дата замера')
    vals = parse_entry_values(data)
    if date != entry['date'] and db.execute(
            'SELECT 1 FROM entries WHERE service_id = ? AND date = ? AND id != ?',
            (entry['service_id'], date, eid)).fetchone():
        return jsonify({'error': 'Замер на эту дату уже есть'}), 400
    changes = []
    for field, old_val, new_val in [('date', entry['date'], date)] + [(f, entry[f], vals[f]) for f in HOUR_FIELDS]:
        if old_val != new_val:
            changes.append({'field': field, 'old': None if old_val is None else str(old_val),
                            'new': None if new_val is None else str(new_val)})
    db.execute(
        'UPDATE entries SET date = ?, remaining = ?, spent = ?, estimate = ? WHERE id = ?',
        (date, vals['remaining'], vals['spent'], vals['estimate'], eid)
    )
    db.commit()
    if changes:
        audit_log('data', 'update', 'entry', eid, svc['project_id'], changes, entity_name=svc['name'])
    return jsonify({'success': True})


@app.route('/api/projects/<int:pid>/measurements', methods=['POST'])
@login_required
def api_bulk_measurements(pid):
    """Замер на одну дату сразу по нескольким эпикам: существующие записи на дату обновляются, новые — создаются."""
    if not can_edit_project(session['user_id'], pid):
        return jsonify({'error': 'Нет прав на редактирование'}), 403
    data = request.get_json() or {}
    date = parse_date(data.get('date'), 'Дата замера')
    items = data.get('items') or []
    if not isinstance(items, list) or not items:
        return jsonify({'error': 'Нет данных для сохранения'}), 400
    db = get_db()
    services = {s['id']: s for s in db.execute('SELECT * FROM services WHERE project_id = ?', (pid,)).fetchall()}
    parsed = []
    for item in items:
        sid = item.get('serviceId') if isinstance(item, dict) else None
        if sid not in services:
            return jsonify({'error': 'Эпик не найден в проекте'}), 400
        vals = parse_entry_values(item)
        if all(v is None for v in vals.values()):
            continue  # пустые строки формы пропускаем
        parsed.append((sid, vals))
    if not parsed:
        return jsonify({'error': 'Заполните хотя бы одно значение'}), 400
    result, _skipped = save_measurements(db, pid, date, parsed, services)
    db.commit()
    return jsonify({'entries': result})


def _num_text(v):
    """Число для реестра изменений: 420.0 → «420»."""
    if v is None:
        return None
    return str(int(v)) if float(v).is_integer() else str(v)


def save_measurements(db, pid, date, parsed, services, actor=None, overwrite=True):
    """Записать замеры на дату: [(serviceId, {remaining, spent, estimate})] → (сохранённые, число пропущенных).

    Существующий замер на дату обновляется; при overwrite=False — остаётся как есть (пропуск).
    Коммит — на стороне вызывающего.
    """
    result, skipped = [], 0
    for sid, vals in parsed:
        existing = db.execute('SELECT * FROM entries WHERE service_id = ? AND date = ?', (sid, date)).fetchone()
        if existing and not overwrite:
            skipped += 1
            continue
        if existing:
            db.execute('UPDATE entries SET remaining = ?, spent = ?, estimate = ? WHERE id = ?',
                       (vals['remaining'], vals['spent'], vals['estimate'], existing['id']))
            eid, action = existing['id'], 'update'
        else:
            eid = db.execute('INSERT INTO entries (service_id, date, remaining, spent, estimate) VALUES (?, ?, ?, ?, ?)',
                             (sid, date, vals['remaining'], vals['spent'], vals['estimate'])).lastrowid
            action = 'create'
        result.append(dict(id=eid, service_id=sid, date=date, **vals))
        changes = [{'field': f, 'old': _num_text(existing[f]) if existing else None, 'new': _num_text(vals[f])}
                   for f in HOUR_FIELDS if not existing or existing[f] != vals[f]]
        if changes:
            audit_log('data', action, 'entry', eid, pid, changes, entity_name=services[sid]['name'], commit=False,
                      actor=actor)
    return result, skipped


@app.route('/api/entries/<int:eid>', methods=['DELETE'])
@login_required
def api_delete_entry(eid):
    db = get_db()
    entry = db.execute('SELECT * FROM entries WHERE id = ?', (eid,)).fetchone()
    if not entry:
        return jsonify({'error': 'Не найдено'}), 404
    svc = db.execute('SELECT * FROM services WHERE id = ?', (entry['service_id'],)).fetchone()
    if not svc or not can_edit_project(session['user_id'], svc['project_id']):
        return jsonify({'error': 'Нет прав на редактирование'}), 403
    db.execute('DELETE FROM entries WHERE id = ?', (eid,))
    db.commit()
    audit_log('data', 'delete', 'entry', eid, svc['project_id'],
              [{'field': 'date', 'old': entry['date'], 'new': None}], entity_name=svc['name'])
    return jsonify({'success': True})


# ─── API: Stage targets ─────────────────────────────────────────

@app.route('/api/projects/<int:pid>/stage_targets', methods=['PUT'])
@login_required
def api_update_stage_target(pid):
    if not can_edit_project(session['user_id'], pid):
        return jsonify({'error': 'Нет прав на редактирование'}), 403
    data = request.get_json() or {}
    stage_name = data.get('stageName')
    target_date = parse_date(data.get('targetDate'), 'Целевая дата', required=False)
    if not stage_name:
        return jsonify({'error': 'Укажите название этапа'}), 400
    db = get_db()
    existing = db.execute(
        'SELECT * FROM stage_targets WHERE project_id = ? AND stage_name = ?',
        (pid, stage_name)
    ).fetchone()
    if existing:
        db.execute('UPDATE stage_targets SET target_date = ? WHERE id = ?',
                    (target_date, existing['id']))
    else:
        db.execute('INSERT INTO stage_targets (project_id, stage_name, target_date) VALUES (?, ?, ?)',
                    (pid, stage_name, target_date))
    db.commit()
    old_target = existing['target_date'] if existing else None
    if text_change(old_target, target_date):
        audit_log('stages', 'update', 'stage_target', None, pid, [text_change(old_target, target_date, stage_name)],
                  entity_name=stage_name)
    return jsonify({'success': True})


# ─── API: Settings ──────────────────────────────────────────────

@app.route('/api/projects/<int:pid>/settings', methods=['PUT'])
@login_required
def api_update_settings(pid):
    if not can_edit_project(session['user_id'], pid):
        return jsonify({'error': 'Нет прав на редактирование'}), 403
    data = {k: v for k, v in (request.get_json() or {}).items() if v is not None}  # null — не менять
    if 'forecastMethod' in data and data['forecastMethod'] not in ('linear', 'average'):
        raise ValidationError('Недопустимый метод прогноза')
    if 'burnrateMethod' in data and data['burnrateMethod'] not in ('all', 'rolling'):
        raise ValidationError('Недопустимый метод расчёта burn rate')
    for key, minimum in (('riskBufferDays', 0), ('burnrateWindowDays', 1)):
        if key in data:
            if isinstance(data[key], bool) or not isinstance(data[key], int) or data[key] < minimum:
                raise ValidationError(f'Поле «{key}»: целое число не меньше {minimum}')
    db = get_db()
    existing = db.execute('SELECT * FROM settings WHERE project_id = ?', (pid,)).fetchone()
    if existing:
        db.execute('''
            UPDATE settings SET
                forecast_method = ?,
                risk_buffer_days = ?,
                burnrate_method = ?,
                burnrate_window_days = ?
            WHERE project_id = ?
        ''', (
            data.get('forecastMethod', existing['forecast_method']),
            data.get('riskBufferDays', existing['risk_buffer_days']),
            data.get('burnrateMethod', existing['burnrate_method']),
            data.get('burnrateWindowDays', existing['burnrate_window_days']),
            pid
        ))
    else:
        db.execute('''
            INSERT INTO settings (project_id, forecast_method, risk_buffer_days, burnrate_method, burnrate_window_days)
            VALUES (?, ?, ?, ?, ?)
        ''', (
            pid,
            data.get('forecastMethod', 'linear'),
            data.get('riskBufferDays', 3),
            data.get('burnrateMethod', 'all'),
            data.get('burnrateWindowDays', 14)
        ))
    db.commit()
    changes = []
    for key, col in SETTINGS_FIELDS:
        old_val = existing[col] if existing else None
        if key in data and str(old_val) != str(data[key]):
            changes.append({'field': key, 'old': None if old_val is None else str(old_val), 'new': str(data[key])})
    if changes:
        audit_log('settings', 'update', 'settings', None, pid, changes)
    return jsonify({'success': True})


# ─── API: Связь с Jira (срез данных из Structure) ────────────────

def _button_field_map(row):
    try:
        return jira.normalize_field_map(json.loads(row['field_map'])) if row['field_map'] else jira.DEFAULT_FIELD_MAP
    except (ValueError, TypeError):
        return jira.DEFAULT_FIELD_MAP


def _jira_button_json(db, row):
    mappings = db.execute('SELECT service_id, issue_keys, include_subtree FROM jira_snapshot_mappings '
                          'WHERE button_id = ? ORDER BY id', (row['id'],)).fetchall()
    sched = db.execute('SELECT * FROM jira_schedules WHERE button_id = ?', (row['id'],)).fetchone()
    return {'id': row['id'], 'name': row['name'], 'structureId': row['structure_id'],
            'structureName': row['structure_name'] or '',
            'fieldMap': _button_field_map(row),
            'lastSuccessAt': row['last_success_at'],
            'schedule': _schedule_json(sched),
            'history': _history_job_json(db.execute('SELECT * FROM jira_history_jobs WHERE button_id = ? '
                                                    'ORDER BY id DESC LIMIT 1', (row['id'],)).fetchone()),
            'mappings': [{'serviceId': m['service_id'], 'issueKeys': m['issue_keys'],
                          'includeSubtree': bool(m['include_subtree'])} for m in mappings]}


def _jira_client(db, pid, override=None):
    """Клиент Jira по сохранённому подключению проекта; override — данные формы (для проверки до сохранения)."""
    saved = db.execute('SELECT * FROM jira_connections WHERE project_id = ?', (pid,)).fetchone()
    conf = dict(override or {})
    if not override and not saved:
        raise jira.JiraError('Подключение к Jira для проекта не настроено')
    base_url = text_value(conf.get('baseUrl')) if override else saved['base_url']
    instance_type = conf.get('instanceType') if override else saved['instance_type']
    login = text_value(conf.get('login')) if override else (saved['login'] or '')
    insecure = bool(conf.get('acceptInvalidCerts')) if override else bool(saved['accept_invalid_certs'])
    token = conf.get('token') or ''
    if not token:
        # сохранённый токен уходит только на тот же адрес Jira, для которого он был введён
        if not saved or not saved['token_encrypted'] or base_url.rstrip('/') != saved['base_url']:
            raise jira.JiraError('Укажите токен или пароль')
        token = jira.decrypt_token(app.secret_key, saved['token_encrypted'])
    if instance_type != 'server' and not login:
        raise jira.JiraError('Укажите email или логин')
    return jira.JiraClient(base_url, instance_type, login, token, accept_invalid_certs=insecure)


def _jira_error(e):
    return jsonify({'error': str(e)}), 502


def _parse_jira_button(db, pid, data):
    name = text_value(data.get('name'))
    if not name:
        raise ValidationError('Укажите название кнопки')
    structure_id = data.get('structureId')
    if structure_id in (None, ''):
        structure_id = None
    elif isinstance(structure_id, bool) or not str(structure_id).isdigit():
        raise ValidationError('ID структуры — целое число')
    else:
        structure_id = int(structure_id)
    project_services = {r['id'] for r in db.execute('SELECT id FROM services WHERE project_id = ?', (pid,))}
    mappings, bad_keys = [], []
    for m in data.get('mappings') or []:
        if not isinstance(m, dict) or m.get('serviceId') not in project_services:
            raise ValidationError('Эпик не найден в проекте')
        keys, invalid = jira.parse_issue_keys(text_value(m.get('issueKeys')))
        bad_keys += invalid
        if keys:
            mappings.append((m['serviceId'], ', '.join(keys), 1 if m.get('includeSubtree', True) else 0))
    if bad_keys:
        raise ValidationError('Некорректные ключи задач: ' + ', '.join(bad_keys))
    if not mappings:
        raise ValidationError('Укажите задачи Jira хотя бы для одного эпика')
    try:
        field_map = jira.normalize_field_map(data.get('fieldMap'))
    except ValueError as e:
        raise ValidationError(str(e))
    return name, structure_id, text_value(data.get('structureName')) or None, mappings, field_map


def _save_jira_mappings(db, button_id, mappings):
    db.execute('DELETE FROM jira_snapshot_mappings WHERE button_id = ?', (button_id,))
    db.executemany('INSERT INTO jira_snapshot_mappings (button_id, service_id, issue_keys, include_subtree) '
                   'VALUES (?, ?, ?, ?)', [(button_id, *m) for m in mappings])


def _jira_button_or_404(db, bid):
    row = db.execute('SELECT * FROM jira_snapshot_buttons WHERE id = ?', (bid,)).fetchone()
    if not row:
        return None, (jsonify({'error': 'Кнопка не найдена'}), 404)
    if not can_edit_project(session['user_id'], row['project_id']):
        return None, (jsonify({'error': 'Нет прав на редактирование'}), 403)
    return row, None


@app.route('/api/projects/<int:pid>/jira')
@login_required
def api_get_jira(pid):
    if not can_edit_project(session['user_id'], pid):
        return jsonify({'error': 'Нет прав на редактирование'}), 403
    db = get_db()
    conn_row = db.execute('SELECT * FROM jira_connections WHERE project_id = ?', (pid,)).fetchone()
    buttons = db.execute('SELECT * FROM jira_snapshot_buttons WHERE project_id = ? ORDER BY sort_order, id',
                         (pid,)).fetchall()
    return jsonify({'connection': jira.connection_json(conn_row),
                    'buttons': [_jira_button_json(db, b) for b in buttons]})


@app.route('/api/projects/<int:pid>/jira/connection', methods=['PUT'])
@login_required
def api_save_jira_connection(pid):
    if not can_edit_project(session['user_id'], pid):
        return jsonify({'error': 'Нет прав на редактирование'}), 403
    data = request.get_json() or {}
    base_url = text_value(data.get('baseUrl')).rstrip('/')
    instance_type = data.get('instanceType')
    login = text_value(data.get('login'))
    token = data.get('token') or ''
    if not re.match(r'^https?://\S+$', base_url):
        raise ValidationError('Адрес Jira должен начинаться с http:// или https://')
    if instance_type not in jira.INSTANCE_TYPES:
        raise ValidationError('Недопустимый тип подключения')
    if instance_type != 'server' and not login:
        raise ValidationError('Укажите email или логин')
    if not isinstance(token, str):
        raise ValidationError('Токен — строка')
    db = get_db()
    existing = db.execute('SELECT * FROM jira_connections WHERE project_id = ?', (pid,)).fetchone()
    if not token and not (existing and existing['token_encrypted']):
        raise ValidationError('Укажите токен или пароль')
    if not token and existing['base_url'] != base_url:
        raise ValidationError('Адрес Jira изменён — введите токен или пароль заново')
    encrypted = jira.encrypt_token(app.secret_key, token) if token else existing['token_encrypted']
    insecure = 1 if data.get('acceptInvalidCerts') else 0
    if existing:
        db.execute('UPDATE jira_connections SET base_url = ?, instance_type = ?, login = ?, token_encrypted = ?, '
                   'accept_invalid_certs = ? WHERE project_id = ?',
                   (base_url, instance_type, login, encrypted, insecure, pid))
    else:
        db.execute('INSERT INTO jira_connections (project_id, base_url, instance_type, login, token_encrypted, '
                   'accept_invalid_certs) VALUES (?, ?, ?, ?, ?, ?)',
                   (pid, base_url, instance_type, login, encrypted, insecure))
    db.commit()
    changes = [text_change(existing['base_url'] if existing else None, base_url, 'jiraUrl'),
               text_change(existing['instance_type'] if existing else None, instance_type, 'jiraAuth'),
               text_change(existing['login'] if existing else None, login, 'jiraLogin')]
    changes = [c for c in changes if c['old'] != c['new']]
    if token:
        changes.append({'field': 'jiraToken', 'old': None, 'new': 'изменён'})
    if changes:
        audit_log('jira', 'update' if existing else 'create', 'jira_connection', None, pid, changes)
    return jsonify(jira.connection_json(
        db.execute('SELECT * FROM jira_connections WHERE project_id = ?', (pid,)).fetchone()))


@app.route('/api/projects/<int:pid>/jira/connection', methods=['DELETE'])
@login_required
def api_delete_jira_connection(pid):
    if not can_edit_project(session['user_id'], pid):
        return jsonify({'error': 'Нет прав на редактирование'}), 403
    db = get_db()
    if db.execute('DELETE FROM jira_connections WHERE project_id = ?', (pid,)).rowcount:
        db.commit()
        audit_log('jira', 'delete', 'jira_connection', None, pid)
    return jsonify({'success': True})


@app.route('/api/projects/<int:pid>/jira/connection/test', methods=['POST'])
@login_required
def api_test_jira_connection(pid):
    if not can_edit_project(session['user_id'], pid):
        return jsonify({'error': 'Нет прав на редактирование'}), 403
    data = request.get_json(silent=True) or None
    try:
        user = _jira_client(get_db(), pid, override=data).myself()
    except jira.JiraError as e:
        return _jira_error(e)
    return jsonify({'success': True, 'user': user})


@app.route('/api/projects/<int:pid>/jira/structures')
@login_required
def api_jira_structures(pid):
    if not can_edit_project(session['user_id'], pid):
        return jsonify({'error': 'Нет прав на редактирование'}), 403
    try:
        return jsonify(_jira_client(get_db(), pid).list_structures())
    except jira.JiraError as e:
        return _jira_error(e)


@app.route('/api/projects/<int:pid>/jira/buttons', methods=['POST'])
@login_required
def api_create_jira_button(pid):
    if not can_edit_project(session['user_id'], pid):
        return jsonify({'error': 'Нет прав на редактирование'}), 403
    db = get_db()
    name, structure_id, structure_name, mappings, field_map = _parse_jira_button(db, pid, request.get_json() or {})
    order = db.execute('SELECT COALESCE(MAX(sort_order), 0) FROM jira_snapshot_buttons WHERE project_id = ?',
                       (pid,)).fetchone()[0]
    bid = db.execute('INSERT INTO jira_snapshot_buttons (project_id, name, structure_id, structure_name, sort_order, '
                     'field_map) VALUES (?, ?, ?, ?, ?, ?)',
                     (pid, name, structure_id, structure_name, order + 1, json.dumps(field_map))).lastrowid
    _save_jira_mappings(db, bid, mappings)
    db.commit()
    audit_log('jira', 'create', 'jira_button', bid, pid, [{'field': 'name', 'old': None, 'new': name}], entity_name=name)
    return jsonify(_jira_button_json(db, db.execute('SELECT * FROM jira_snapshot_buttons WHERE id = ?', (bid,)).fetchone()))


@app.route('/api/jira/buttons/<int:bid>', methods=['PUT'])
@login_required
def api_update_jira_button(bid):
    db = get_db()
    row, err = _jira_button_or_404(db, bid)
    if err:
        return err
    name, structure_id, structure_name, mappings, field_map = _parse_jira_button(
        db, row['project_id'], request.get_json() or {})
    db.execute('UPDATE jira_snapshot_buttons SET name = ?, structure_id = ?, structure_name = ?, field_map = ? '
               'WHERE id = ?', (name, structure_id, structure_name, json.dumps(field_map), bid))
    _save_jira_mappings(db, bid, mappings)
    db.commit()
    audit_log('jira', 'update', 'jira_button', bid, row['project_id'],
              [text_change(row['name'], name, 'name')] if row['name'] != name else None, entity_name=name)
    return jsonify(_jira_button_json(db, db.execute('SELECT * FROM jira_snapshot_buttons WHERE id = ?', (bid,)).fetchone()))


@app.route('/api/jira/buttons/<int:bid>', methods=['DELETE'])
@login_required
def api_delete_jira_button(bid):
    db = get_db()
    row, err = _jira_button_or_404(db, bid)
    if err:
        return err
    db.execute('DELETE FROM jira_snapshot_buttons WHERE id = ?', (bid,))
    db.commit()
    audit_log('jira', 'delete', 'jira_button', bid, row['project_id'],
              [{'field': 'name', 'old': row['name'], 'new': None}], entity_name=row['name'])
    return jsonify({'success': True})


@app.route('/api/jira/buttons/<int:bid>/snapshot', methods=['POST'])
@login_required
def api_jira_snapshot(bid):
    """Срез из Jira по кнопке: значения для эпиков. Замер сохраняет пользователь после проверки,
    а состав эпиков по задачам фиксируется сразу — на день среза (date в теле запроса, по умолчанию сегодня)."""
    db = get_db()
    row, err = _jira_button_or_404(db, bid)
    if err:
        return err
    snap_date = parse_date((request.get_json(silent=True) or {}).get('date'), 'Дата среза', required=False) \
        or datetime.now().date().isoformat()
    button = _jira_button_json(db, row)
    try:
        items = jira.compute_snapshot(_jira_client(db, row['project_id']), row['structure_id'], button['mappings'],
                                      button['fieldMap'])
    except jira.JiraError as e:
        return _jira_error(e)
    store_issue_snapshots(db, row['project_id'], items, snap_date)
    if _snapshot_has_values(items):
        _mark_snapshot_success(db, bid)
        button = _jira_button_json(db, db.execute('SELECT * FROM jira_snapshot_buttons WHERE id = ?', (bid,)).fetchone())
    db.commit()
    return jsonify({'button': button, 'items': items})


def store_issue_snapshots(db, pid, items, snap_date):
    """Сохранить состав эпиков по задачам на день среза и убрать списки задач из items (ответ остаётся лёгким).

    Повторный срез за тот же день заменяет состав эпика. Коммит — на стороне вызывающего.
    """
    services = {r['id'] for r in db.execute('SELECT id FROM services WHERE project_id = ?', (pid,))}
    captured = _utc_now_iso()
    for it in items:
        issues = it.pop('issues', None) or []
        if it['serviceId'] not in services or not issues:
            continue
        db.execute('DELETE FROM jira_issue_snapshots WHERE service_id = ? AND snap_date = ?', (it['serviceId'], snap_date))
        db.executemany(
            'INSERT OR REPLACE INTO jira_issue_snapshots (service_id, snap_date, issue_key, summary, status, category, '
            'remaining, spent, estimate, container, problems, captured_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
            [(it['serviceId'], snap_date, i['key'], i['summary'], i['status'], i['category'], i['remaining'], i['spent'],
              i['estimate'], 1 if i['container'] else 0, ','.join(i['problems']), captured) for i in issues])


VOLUME_SQL = 'COALESCE(estimate, COALESCE(spent, 0) + COALESCE(remaining, 0))'


@app.route('/api/projects/<int:pid>/jira/composition')
@login_required
def api_jira_composition(pid):
    """Состав эпиков по задачам Jira: последний срез (статусы, объём, проблемы данных) и история для burn-up."""
    if not can_view_project(session['user_id'], pid):
        return jsonify({'error': 'Нет доступа'}), 403
    db = get_db()
    history, latest = jira_composition(db, pid)
    conn = db.execute('SELECT base_url FROM jira_connections WHERE project_id = ?', (pid,)).fetchone()
    return jsonify({'baseUrl': conn['base_url'] if conn else '', 'latest': latest, 'history': history,
                    'problemLabels': jira.PROBLEMS})


def _days(a, b):
    return (datetime.strptime(b, '%Y-%m-%d') - datetime.strptime(a, '%Y-%m-%d')).days


def burnup_forecast(history, settings=None):
    """Когда «Выполнено» догонит «Объём» (как в интерфейсе): темпы — наклоны регрессии по срезам,
    при скользящем burn rate — по срезам окна. → {'date', 'converges', 'doneRate', 'scopeRate'} или None."""
    if not history or len(history) < 2:
        return None
    pts = history
    if settings and settings['burnrate_method'] == 'rolling':
        cutoff = (datetime.strptime(history[-1]['date'], '%Y-%m-%d')
                  - timedelta(days=settings['burnrate_window_days'] or 14)).date().isoformat()
        windowed = [p for p in history if p['date'] >= cutoff]
        pts = windowed if len(windowed) >= 2 else history[-2:]
    base = pts[0]['date']
    if _days(base, pts[-1]['date']) <= 0:
        return None

    def slope(field):
        xs = [_days(base, p['date']) for p in pts]
        ys = [p[field] for p in pts]
        n = len(xs)
        denom = n * sum(x * x for x in xs) - sum(xs) ** 2
        return (n * sum(x * y for x, y in zip(xs, ys)) - sum(xs) * sum(ys)) / denom if denom else None

    done_rate, scope_rate = slope('done'), slope('scope')
    if done_rate is None or scope_rate is None:
        return None
    last = history[-1]
    out = {'doneRate': done_rate, 'scopeRate': scope_rate, 'converges': False, 'date': None}
    gap = last['scope'] - last['done']
    if gap <= 0.005:
        return dict(out, converges=True, date=last['date'])
    speed = done_rate - scope_rate
    if speed <= 0 or gap / speed > 3 * 365:
        return out
    return dict(out, converges=True, date=(datetime.strptime(last['date'], '%Y-%m-%d')
                                           + timedelta(days=math.ceil(gap / speed))).date().isoformat())


def aggregate_composition(history, latest, sids):
    """Состав группы эпиков: на каждую дату — сумма последних известных срезов эпиков (как в интерфейсе)."""
    hists = [history[sid] for sid in sids if history.get(sid)]
    if not hists:
        return None
    out = []
    for day in sorted({p['date'] for h in hists for p in h}):
        agg = {'date': day, 'scope': 0.0, 'done': 0.0, 'total': 0, 'doneCount': 0,
               'counts': {c: 0 for c in jira.STATUS_CATEGORIES}}
        for h in hists:
            known = [p for p in h if p['date'] <= day]
            if not known:
                continue
            p = known[-1]
            agg['scope'] += p['scope']
            agg['done'] += p['done']
            agg['total'] += p['total']
            agg['doneCount'] += p['doneCount']
            for c in jira.STATUS_CATEGORIES:
                agg['counts'][c] += p['counts'][c]
        agg['scope'], agg['done'] = round(agg['scope'], 2), round(agg['done'], 2)
        out.append(agg)
    problems = {}
    for sid in sids:
        for code, n in (latest.get(sid) or {}).get('problems', {}).items():
            problems[code] = problems.get(code, 0) + n
    return out, dict(out[-1], problems=problems)


def _composition_pdf_row(label, history, latest, settings, target=None):
    pct = f"{round(latest['done'] / latest['scope'] * 100)}%" if latest['scope'] else '—'
    f = burnup_forecast(history, settings)
    forecast = '—' if not f else (_fmt_date(f['date']) if f['converges'] else 'не сходится')
    if f and f['converges'] and target and f['date'] > target:
        forecast += ' (позже)'
    return [label, str(latest['counts']['done']), str(latest['counts']['indeterminate']), str(latest['counts']['new']),
            pct, _fmt_num(latest['scope']), forecast, str(sum(latest['problems'].values()) or '—'), _fmt_date(latest['date'])]


COMPOSITION_PDF_HEAD = ['', 'Готово', 'В работе', 'Не начато', 'Выполнено', 'Объём, ч', 'Прогноз по задачам',
                        'Неполные данные', 'Срез']
COMPOSITION_PDF_WIDTHS = [3.2, 0.8, 0.9, 0.9, 1, 0.9, 1.7, 1.4, 1]


def jira_composition(db, pid):
    """Состав эпиков проекта по срезам задач → (история по датам, последний срез с проблемами данных)."""
    rows = db.execute(
        f'''SELECT s.service_id, s.snap_date, s.category,
                   SUM(CASE WHEN s.container THEN 0 ELSE 1 END) AS cnt,
                   SUM({VOLUME_SQL}) AS volume, SUM(COALESCE(s.remaining, 0)) AS rem
            FROM jira_issue_snapshots s JOIN services v ON v.id = s.service_id
            WHERE v.project_id = ? GROUP BY s.service_id, s.snap_date, s.category
            ORDER BY s.service_id, s.snap_date''', (pid,)).fetchall()
    history, latest = {}, {}
    for r in rows:
        sid = r['service_id']
        points = history.setdefault(sid, [])
        if not points or points[-1]['date'] != r['snap_date']:
            points.append({'date': r['snap_date'], 'scope': 0.0, 'done': 0.0, 'total': 0, 'doneCount': 0,
                           'counts': {c: 0 for c in jira.STATUS_CATEGORIES},
                           'volume': {c: 0.0 for c in jira.STATUS_CATEGORIES}})
        p = points[-1]
        cat = r['category'] if r['category'] in jira.STATUS_CATEGORIES else 'new'
        p['counts'][cat] += r['cnt']
        p['volume'][cat] = round(p['volume'][cat] + (r['volume'] or 0), 2)
        p['total'] += r['cnt']
        p['scope'] = round(p['scope'] + (r['volume'] or 0), 2)
        if cat == 'done':
            p['doneCount'] += r['cnt']
            p['done'] = round(p['done'] + (r['volume'] or 0), 2)
    for sid, points in history.items():
        last = dict(points[-1], problems={})
        for pr in db.execute('SELECT problems FROM jira_issue_snapshots WHERE service_id = ? AND snap_date = ? '
                             "AND problems != ''", (sid, last['date'])):
            for code in pr['problems'].split(','):
                last['problems'][code] = last['problems'].get(code, 0) + 1
        latest[sid] = last
    return history, latest


@app.route('/api/services/<int:sid>/jira/issues')
@login_required
def api_jira_service_issues(sid):
    """Задачи Jira эпика на дату среза (по умолчанию — последний срез)."""
    db = get_db()
    svc = db.execute('SELECT * FROM services WHERE id = ?', (sid,)).fetchone()
    if not svc:
        return jsonify({'error': 'Эпик не найден'}), 404
    if not can_view_project(session['user_id'], svc['project_id']):
        return jsonify({'error': 'Нет доступа'}), 403
    dates = _snapshot_dates(db, sid)
    snap_date = parse_date(request.args.get('date'), 'Дата среза', required=False) or (dates[-1] if dates else None)
    prev_date = max((d for d in dates if snap_date and d < snap_date), default=None)
    prev = _snapshot_issues(db, sid, prev_date) if prev_date else {}
    issues = []
    for key, i in _snapshot_issues(db, sid, snap_date).items():
        p = prev.get(key)
        i['prev'] = {m: p[m] for m in ('remaining', 'spent', 'estimate', 'category')} if p else None
        issues.append(i)
    issues.sort(key=lambda i: jira.issue_sort_key(i['key']))
    return jsonify({'date': snap_date, 'prevDate': prev_date, 'baseUrl': _jira_base_url(db, svc['project_id']),
                    'issues': issues, 'problemLabels': jira.PROBLEMS})


def _jira_base_url(db, pid):
    conn = db.execute('SELECT base_url FROM jira_connections WHERE project_id = ?', (pid,)).fetchone()
    return conn['base_url'] if conn else ''


def _snapshot_dates(db, sid):
    return [r[0] for r in db.execute('SELECT DISTINCT snap_date FROM jira_issue_snapshots WHERE service_id = ? '
                                     'ORDER BY snap_date', (sid,))]


def _snapshot_issues(db, sid, snap_date):
    """Задачи эпика на дату среза → {ключ: задача}."""
    if not snap_date:
        return {}
    rows = db.execute('SELECT * FROM jira_issue_snapshots WHERE service_id = ? AND snap_date = ?', (sid, snap_date))
    return {r['issue_key']: {'key': r['issue_key'], 'summary': r['summary'] or '', 'status': r['status'] or '',
                             'category': r['category'], 'remaining': r['remaining'], 'spent': r['spent'],
                             'estimate': r['estimate'], 'container': bool(r['container']),
                             'problems': [p for p in (r['problems'] or '').split(',') if p]} for r in rows}


def scope_changes(before, after):
    """Разложение изменения объёма эпика между двумя срезами: новые, убранные, переоценённые задачи,
    а также закрытые и переоткрытые за период. Объём «до» + новые − убранные ± переоценка = объём «после»."""
    vol = jira.issue_volume

    def brief(i, **extra):
        return dict({'key': i['key'], 'summary': i['summary'], 'status': i['status'], 'category': i['category'],
                     'volume': round(vol(i), 2)}, **extra)

    def group(items, value):
        items = sorted(items, key=lambda i: -abs(value(i)))
        return {'count': len(items), 'volume': round(sum(value(i) for i in items), 2), 'issues': items}

    added = [brief(i) for k, i in after.items() if k not in before]
    removed = [brief(i) for k, i in before.items() if k not in after]
    reestimated, closed, reopened = [], [], []
    for key in before.keys() & after.keys():
        a, b = before[key], after[key]
        delta = round(vol(b) - vol(a), 2)
        if delta:
            reestimated.append(brief(b, before=round(vol(a), 2), delta=delta))
        if b['category'] == 'done' and a['category'] != 'done':
            closed.append(brief(b))
        elif a['category'] == 'done' and b['category'] != 'done':
            reopened.append(brief(b))
    return {
        'scopeFrom': round(sum(vol(i) for i in before.values()), 2),
        'scopeTo': round(sum(vol(i) for i in after.values()), 2),
        'doneFrom': round(sum(vol(i) for i in before.values() if i['category'] == 'done'), 2),
        'doneTo': round(sum(vol(i) for i in after.values() if i['category'] == 'done'), 2),
        'added': group(added, lambda i: i['volume']),
        'removed': group(removed, lambda i: -i['volume']),
        'reestimated': group(reestimated, lambda i: i['delta']),
        'closed': group(closed, lambda i: i['volume']),
        'reopened': group(reopened, lambda i: i['volume']),
    }


@app.route('/api/services/<int:sid>/jira/changes')
@login_required
def api_jira_service_changes(sid):
    """Что изменилось в задачах эпика между двумя срезами (по умолчанию — предпоследний и последний)."""
    db = get_db()
    svc = db.execute('SELECT * FROM services WHERE id = ?', (sid,)).fetchone()
    if not svc:
        return jsonify({'error': 'Эпик не найден'}), 404
    if not can_view_project(session['user_id'], svc['project_id']):
        return jsonify({'error': 'Нет доступа'}), 403
    dates = _snapshot_dates(db, sid)
    to_date = parse_date(request.args.get('to'), 'Дата «по»', required=False) or (dates[-1] if dates else None)
    from_date = parse_date(request.args.get('from'), 'Дата «с»', required=False) or \
        max((d for d in dates if to_date and d < to_date), default=None)
    out = {'dates': dates, 'from': from_date, 'to': to_date, 'baseUrl': _jira_base_url(db, svc['project_id'])}
    if not from_date or not to_date:
        return jsonify(out)
    if from_date not in dates or to_date not in dates:
        raise ValidationError('На выбранную дату нет среза задач')
    if from_date >= to_date:
        raise ValidationError('Дата «с» должна быть раньше даты «по»')
    out.update(scope_changes(_snapshot_issues(db, sid, from_date), _snapshot_issues(db, sid, to_date)))
    return jsonify(out)


def _snapshot_has_values(items):
    return any(it[m] is not None for it in items for m in jira.METRICS)


def _mark_snapshot_success(db, bid):
    """Успешный срез открывает автозаполнение по расписанию для кнопки."""
    db.execute('UPDATE jira_snapshot_buttons SET last_success_at = ? WHERE id = ?', (_utc_now_iso(), bid))
    db.commit()


@app.route('/api/projects/<int:pid>/jira/fields')
@login_required
def api_jira_fields(pid):
    if not can_edit_project(session['user_id'], pid):
        return jsonify({'error': 'Нет прав на редактирование'}), 403
    try:
        return jsonify(_jira_client(get_db(), pid).list_fields())
    except jira.JiraError as e:
        return _jira_error(e)


# ─── Автозаполнение по расписанию ────────────────────────────────

AUTOFILL_ACTOR = 'Автозаполнение'
SCHEDULER_INTERVAL = 30  # секунд между проверками расписаний
logger = logging.getLogger('burndown.autofill')


def _utc_now_iso():
    """UTC в формате CURRENT_TIMESTAMP SQLite — интерфейс показывает его в локальном времени."""
    return datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')


def _zone(name):
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValidationError(f'Неизвестный часовой пояс: {name}')


def _schedule_days(text):
    return [int(d) for d in (text or '').split(',') if d.strip().isdigit()]


def _next_run(sched, now_utc=None):
    """Ближайший запуск по расписанию в его часовом поясе (ISO без смещения) или None."""
    if not sched or not sched['enabled']:
        return None
    days = set(_schedule_days(sched['days']))
    if not days:
        return None
    try:
        tz = ZoneInfo(sched['timezone'])
    except (ZoneInfoNotFoundError, ValueError):
        return None
    local = (now_utc or datetime.now(timezone.utc)).astimezone(tz)
    hh, mm = (int(x) for x in sched['run_time'].split(':'))
    for shift in range(0, 8):
        day = local.date() + timedelta(days=shift)
        if day.isoweekday() not in days:
            continue
        if shift == 0 and (local.strftime('%H:%M') >= sched['run_time'] or sched['last_run_date'] == day.isoformat()):
            continue
        return datetime(day.year, day.month, day.day, hh, mm).isoformat(timespec='minutes')
    return None


def _schedule_json(sched):
    if not sched:
        return {'enabled': False, 'days': [1, 2, 3, 4, 5], 'time': '09:00', 'timezone': 'Europe/Moscow',
                'overwrite': True, 'lastRunAt': None, 'lastStatus': None, 'lastMessage': None, 'nextRunAt': None}
    return {'enabled': bool(sched['enabled']), 'days': _schedule_days(sched['days']), 'time': sched['run_time'],
            'timezone': sched['timezone'], 'overwrite': bool(sched['overwrite']), 'lastRunAt': sched['last_run_at'],
            'lastStatus': sched['last_status'], 'lastMessage': sched['last_message'], 'nextRunAt': _next_run(sched)}


@app.route('/api/jira/buttons/<int:bid>/schedule', methods=['PUT'])
@login_required
def api_save_jira_schedule(bid):
    db = get_db()
    row, err = _jira_button_or_404(db, bid)
    if err:
        return err
    data = request.get_json() or {}
    enabled = bool(data.get('enabled'))
    days = data.get('days') or []
    if not isinstance(days, list) or not all(isinstance(d, int) and 1 <= d <= 7 for d in days):
        raise ValidationError('Дни недели — числа от 1 (пн) до 7 (вс)')
    days = sorted(set(days))
    run_time = text_value(data.get('time'))
    if not re.fullmatch(r'([01]\d|2[0-3]):[0-5]\d', run_time):
        raise ValidationError('Время запуска — в формате ЧЧ:ММ')
    tz_name = text_value(data.get('timezone')) or 'Europe/Moscow'
    tz = _zone(tz_name)
    if enabled and not days:
        raise ValidationError('Выберите хотя бы один день недели')
    if enabled and not row['last_success_at']:
        raise ValidationError('Автозаполнение доступно после первого успешного среза по этой кнопке')
    existing = db.execute('SELECT * FROM jira_schedules WHERE button_id = ?', (bid,)).fetchone()
    # если сегодняшнее время запуска уже прошло, первый автоматический запуск — в следующий день по расписанию
    local = datetime.now(timezone.utc).astimezone(tz)
    last_run_date = existing['last_run_date'] if existing else None
    if enabled and local.strftime('%H:%M') >= run_time and not (existing and existing['enabled']):
        last_run_date = local.date().isoformat()
    values = (1 if enabled else 0, ','.join(str(d) for d in days), run_time, tz_name,
              1 if data.get('overwrite', True) else 0, last_run_date)
    if existing:
        db.execute('UPDATE jira_schedules SET enabled = ?, days = ?, run_time = ?, timezone = ?, overwrite = ?, '
                   'last_run_date = ? WHERE button_id = ?', values + (bid,))
    else:
        db.execute('INSERT INTO jira_schedules (enabled, days, run_time, timezone, overwrite, last_run_date, button_id) '
                   'VALUES (?, ?, ?, ?, ?, ?, ?)', values + (bid,))
    db.commit()
    audit_log('jira', 'update', 'jira_schedule', bid, row['project_id'],
              [{'field': 'autofill', 'old': None,
                'new': (f"{','.join(str(d) for d in days)} в {run_time} ({tz_name})" if enabled else 'выключено')}],
              entity_name=row['name'])
    return jsonify(_jira_button_json(db, db.execute('SELECT * FROM jira_snapshot_buttons WHERE id = ?', (bid,)).fetchone()))


def run_autofill(db, button_row, local_date, actor, overwrite=True):
    """Срез по кнопке и запись замеров на local_date → (статус, сообщение). Ошибки Jira не выбрасываются."""
    pid = button_row['project_id']
    try:
        button = _jira_button_json(db, button_row)
        items = jira.compute_snapshot(_jira_client(db, pid), button_row['structure_id'], button['mappings'],
                                      button['fieldMap'])
    except jira.JiraError as e:
        return 'error', str(e)
    store_issue_snapshots(db, pid, items, local_date)
    services = {s['id']: s for s in db.execute('SELECT * FROM services WHERE project_id = ?', (pid,)).fetchall()}
    parsed = []
    for it in items:
        if it['serviceId'] not in services or all(it[m] is None for m in jira.METRICS):
            continue
        vals = {m: it[m] for m in jira.METRICS}
        # показатели без значения из Jira («не заполнять», пустое поле) не затирают уже внесённые вручную
        existing = db.execute('SELECT * FROM entries WHERE service_id = ? AND date = ?',
                              (it['serviceId'], local_date)).fetchone()
        if existing:
            vals = {m: existing[m] if v is None else v for m, v in vals.items()}
        parsed.append((it['serviceId'], vals))
    if not parsed:
        return 'error', 'Jira не вернула значений ни по одному эпику'
    saved, skipped = save_measurements(db, pid, local_date, parsed, services, actor=actor, overwrite=overwrite)
    db.execute('UPDATE jira_snapshot_buttons SET last_success_at = ? WHERE id = ?', (_utc_now_iso(), button_row['id']))
    warnings = sum(len(it['warnings']) for it in items)
    message = f'Сохранено замеров за {datetime.strptime(local_date, "%Y-%m-%d"):%d.%m.%Y}: {len(saved)}'
    if skipped:
        message += f'; пропущено (замер уже был): {skipped}'
    if warnings:
        message += f'; предупреждений: {warnings}'
    return 'ok', message


def _finish_autofill(db, button_row, status, message, actor):
    db.execute('UPDATE jira_schedules SET last_run_at = ?, last_status = ?, last_message = ? WHERE button_id = ?',
               (_utc_now_iso(), status, message, button_row['id']))
    audit_log('jira', 'autofill', 'jira_button', button_row['id'], button_row['project_id'],
              [{'field': 'result', 'old': None, 'new': message}], entity_name=button_row['name'], commit=False,
              actor=actor)
    if status == 'error':
        queue_autofill_error(db, button_row, message)
    db.commit()


@app.route('/api/jira/buttons/<int:bid>/schedule/run', methods=['POST'])
@login_required
def api_run_jira_schedule(bid):
    """«Запустить сейчас»: то же, что автоматический запуск, — срез и запись замеров на сегодня."""
    db = get_db()
    row, err = _jira_button_or_404(db, bid)
    if err:
        return err
    if not row['last_success_at']:
        raise ValidationError('Сначала выполните срез вручную хотя бы один раз')
    sched = db.execute('SELECT * FROM jira_schedules WHERE button_id = ?', (bid,)).fetchone()
    tz = _zone(sched['timezone'] if sched else 'Europe/Moscow')
    if not sched:
        db.execute('INSERT INTO jira_schedules (button_id) VALUES (?)', (bid,))
        sched = db.execute('SELECT * FROM jira_schedules WHERE button_id = ?', (bid,)).fetchone()
    local_date = datetime.now(timezone.utc).astimezone(tz).date().isoformat()
    status, message = run_autofill(db, row, local_date, None, overwrite=bool(sched['overwrite']))
    _finish_autofill(db, row, status, message, None)
    button = _jira_button_json(db, db.execute('SELECT * FROM jira_snapshot_buttons WHERE id = ?', (bid,)).fetchone())
    return jsonify({'status': status, 'message': message, 'button': button}), (200 if status == 'ok' else 502)


# ─── Восстановление истории по журналу изменений Jira ────────────

HISTORY_MAX_DATES = 370
HISTORY_STALE = timedelta(minutes=15)  # задача без обновлений дольше — считается прерванной


def _history_job_json(job):
    if not job:
        return None
    status = job['status']
    if status == 'running' and job['updated_at'] and \
            datetime.now(timezone.utc) - datetime.strptime(job['updated_at'], '%Y-%m-%d %H:%M:%S').replace(
                tzinfo=timezone.utc) > HISTORY_STALE:
        status = 'stale'
    return {'id': job['id'], 'buttonId': job['button_id'], 'status': status, 'progress': job['progress'],
            'total': job['total'], 'message': job['message'] or '', 'params': json.loads(job['params'] or '{}'),
            'createdBy': job['created_by'], 'createdAt': job['created_at'], 'finishedAt': job['finished_at']}


def history_dates(date_from, date_to, step_days):
    """Даты восстановления: от date_from с шагом, последняя — всегда date_to."""
    start = datetime.strptime(date_from, '%Y-%m-%d').date()
    end = datetime.strptime(date_to, '%Y-%m-%d').date()
    out, day = [], start
    while day <= end:
        out.append(day.isoformat())
        day += timedelta(days=step_days)
    if out and out[-1] != end.isoformat():
        out.append(end.isoformat())
    return out


@app.route('/api/jira/buttons/<int:bid>/history', methods=['POST'])
@login_required
def api_start_jira_history(bid):
    """Восстановить замеры и состав задач на прошлые даты по журналу изменений Jira (в фоне)."""
    db = get_db()
    row, err = _jira_button_or_404(db, bid)
    if err:
        return err
    data = request.get_json() or {}
    date_from = parse_date(data.get('from'), 'Дата «с»')
    date_to = parse_date(data.get('to'), 'Дата «по»')
    step = data.get('step') or 'week'
    if step not in ('day', 'week'):
        raise ValidationError('Шаг — день или неделя')
    tz_name = text_value(data.get('timezone')) or 'Europe/Moscow'
    tz = _zone(tz_name)
    if date_from > date_to:
        raise ValidationError('Дата «с» должна быть не позже даты «по»')
    if date_to > datetime.now(timezone.utc).astimezone(tz).date().isoformat():
        raise ValidationError('Восстановить можно только прошедшие даты')
    dates = history_dates(date_from, date_to, 1 if step == 'day' else 7)
    if len(dates) > HISTORY_MAX_DATES:
        raise ValidationError(f'Слишком много дат ({len(dates)}): не больше {HISTORY_MAX_DATES} — увеличьте шаг или сократите период')
    if not row['last_success_at']:
        raise ValidationError('Сначала выполните срез по кнопке вручную хотя бы один раз')
    running = _history_job_json(db.execute("SELECT * FROM jira_history_jobs WHERE button_id = ? AND status = 'running' "
                                           'ORDER BY id DESC LIMIT 1', (bid,)).fetchone())
    if running and running['status'] == 'running':
        return jsonify({'error': 'Восстановление по этой кнопке уже выполняется', 'job': running}), 409
    params = {'from': date_from, 'to': date_to, 'step': step, 'timezone': tz_name, 'dates': len(dates),
              'keepExisting': bool(data.get('keepExisting', True))}
    now = _utc_now_iso()
    user = get_current_user()
    jid = db.execute('INSERT INTO jira_history_jobs (button_id, status, message, params, created_by, created_at, '
                     "updated_at) VALUES (?, 'running', ?, ?, ?, ?, ?)",
                     (bid, 'Подготовка…', json.dumps(params), user['username'], now, now)).lastrowid
    db.commit()
    if app.config.get('HISTORY_SYNC'):  # тесты: выполнить сразу, без потока
        run_history_job(jid)
    else:
        threading.Thread(target=_history_thread, args=(jid,), name=f'jira-history-{jid}', daemon=True).start()
    job = db.execute('SELECT * FROM jira_history_jobs WHERE id = ?', (jid,)).fetchone()
    return jsonify(_history_job_json(job)), 202


@app.route('/api/jira/history-jobs/<int:jid>')
@login_required
def api_jira_history_job(jid):
    db = get_db()
    job = db.execute('SELECT * FROM jira_history_jobs WHERE id = ?', (jid,)).fetchone()
    if not job:
        return jsonify({'error': 'Задача не найдена'}), 404
    _row, err = _jira_button_or_404(db, job['button_id'])
    if err:
        return err
    return jsonify(_history_job_json(job))


def _history_thread(jid):
    with app.app_context():
        run_history_job(jid)


def _history_update(db, jid, **fields):
    fields['updated_at'] = _utc_now_iso()
    db.execute('UPDATE jira_history_jobs SET ' + ', '.join(f'{k} = ?' for k in fields) + ' WHERE id = ?',
               list(fields.values()) + [jid])
    db.commit()


def run_history_job(jid):
    """Восстановление: текущий состав задач кнопки + журнал изменений → замеры и состав на прошлые даты.

    Существующие срезы состава не перезаписываются; замеры на даты, где они уже есть, — только при keepExisting=False.
    Показатели без значения из Jira не затирают внесённые вручную.
    """
    db = get_db()
    job = db.execute('SELECT * FROM jira_history_jobs WHERE id = ?', (jid,)).fetchone()
    button_row = db.execute('SELECT * FROM jira_snapshot_buttons WHERE id = ?', (job['button_id'],)).fetchone()
    params = json.loads(job['params'])
    pid, actor = button_row['project_id'], job['created_by']
    try:
        tz = ZoneInfo(params['timezone'])
        moments = {d: datetime.strptime(d, '%Y-%m-%d').replace(hour=23, minute=59, second=59, tzinfo=tz)
                   for d in history_dates(params['from'], params['to'], 1 if params['step'] == 'day' else 7)}
        button = _jira_button_json(db, button_row)

        def progress(done, total):
            _history_update(db, jid, progress=done, total=total, message=f'Журнал изменений: {done} из {total} задач')

        results, warnings = jira.compute_history(_jira_client(db, pid), button_row['structure_id'], button['mappings'],
                                                 button['fieldMap'], moments, progress)
        _history_update(db, jid, message='Сохранение замеров…')
        services = {s['id']: s for s in db.execute('SELECT * FROM services WHERE project_id = ?', (pid,)).fetchall()}
        saved = skipped = 0
        days_saved = set()
        for day, items in sorted(results.items()):
            existing = {r[0] for r in db.execute('SELECT DISTINCT service_id FROM jira_issue_snapshots WHERE snap_date = ?',
                                                 (day,))}
            for it in items:
                if it['serviceId'] in existing:
                    it['issues'] = []  # настоящий срез за этот день ценнее восстановленного
            store_issue_snapshots(db, pid, items, day)
            parsed = []
            for it in items:
                if it['serviceId'] not in services or all(it[m] is None for m in jira.METRICS):
                    continue
                vals = {m: it[m] for m in jira.METRICS}
                entry = db.execute('SELECT * FROM entries WHERE service_id = ? AND date = ?',
                                   (it['serviceId'], day)).fetchone()
                if entry:
                    vals = {m: entry[m] if v is None else v for m, v in vals.items()}
                parsed.append((it['serviceId'], vals))
            done, skip = save_measurements(db, pid, day, parsed, services, actor=actor,
                                           overwrite=not params['keepExisting'])
            saved += len(done)
            skipped += skip
            if done:
                days_saved.add(day)
            db.commit()
        message = f'Восстановлено замеров: {saved} на {len(days_saved)} дат(ы)'
        if skipped:
            message += f'; пропущено (замер уже был): {skipped}'
        if warnings:
            message += '. ' + '; '.join(warnings)
        audit_log('jira', 'restore', 'jira_button', button_row['id'], pid,
                  [{'field': 'result', 'old': None,
                    'new': f"{_fmt_date(params['from'])} — {_fmt_date(params['to'])}: {message}"}],
                  entity_name=button_row['name'], commit=False, actor=actor)
        _history_update(db, jid, status='done', message=message, finished_at=_utc_now_iso())
    except jira.JiraError as e:
        db.rollback()
        _history_update(db, jid, status='error', message=str(e), finished_at=_utc_now_iso())
    except Exception as e:  # noqa: BLE001 — ошибка фоновой задачи показывается пользователю
        db.rollback()
        logger.exception('history job %s failed', jid)
        _history_update(db, jid, status='error', message=f'Внутренняя ошибка: {e}', finished_at=_utc_now_iso())


def run_due_schedules(now_utc=None):
    """Выполнить наступившие запуски. Один запуск в день на кнопку: день «захватывается» атомарно в БД,
    поэтому при нескольких воркерах/процессах срез выполнит только один из них.
    Если сервер был недоступен во время запуска, срез выполнится позже в тот же день."""
    now_utc = now_utc or datetime.now(timezone.utc)
    db = get_db()
    rows = db.execute('SELECT s.*, b.project_id FROM jira_schedules s JOIN jira_snapshot_buttons b ON b.id = s.button_id '
                      'WHERE s.enabled = 1').fetchall()
    done = 0
    for sched in rows:
        try:
            local = now_utc.astimezone(ZoneInfo(sched['timezone']))
        except (ZoneInfoNotFoundError, ValueError):
            continue
        today = local.date().isoformat()
        if (local.isoweekday() not in _schedule_days(sched['days']) or local.strftime('%H:%M') < sched['run_time']
                or sched['last_run_date'] == today):
            continue
        claimed = db.execute('UPDATE jira_schedules SET last_run_date = ? WHERE button_id = ? AND enabled = 1 '
                             'AND (last_run_date IS NULL OR last_run_date != ?)',
                             (today, sched['button_id'], today)).rowcount
        db.commit()
        if not claimed:
            continue
        button = db.execute('SELECT * FROM jira_snapshot_buttons WHERE id = ?', (sched['button_id'],)).fetchone()
        try:
            status, message = run_autofill(db, button, today, AUTOFILL_ACTOR, overwrite=bool(sched['overwrite']))
        except Exception as e:  # noqa: BLE001 — сбой одного запуска не должен останавливать планировщик
            db.rollback()
            logger.exception('autofill failed for button %s', sched['button_id'])
            status, message = 'error', f'Внутренняя ошибка: {e}'
        _finish_autofill(db, button, status, message, AUTOFILL_ACTOR)
        done += 1
    return done


# ─── Уведомления: настройки отправки, подписки, дайджест, оповещения ───

ALERT_TYPES = ('autofill', 'status', 'chance', 'scope', 'problems')
NOTIFY_SECRETS = ('smtp_password', 'telegram_token')
STATUS_TITLES = {'ok': 'Успеваем', 'risk': 'Риск', 'danger': 'Не успеваем', 'notarget': 'Нет цели', 'unknown': 'Нет данных'}
STATUS_COLORS = {'ok': '#437a22', 'risk': '#964219', 'danger': '#a12c7b', 'notarget': '#5c6470', 'unknown': '#5c6470'}
TIME_RE = re.compile(r'^([01]\d|2[0-3]):[0-5]\d$')


def notify_settings(db):
    """Настройки отправки из app_settings; секреты расшифрованы (None, если ключ сервера сменился)."""
    cfg = {r['key']: r['value'] for r in db.execute('SELECT key, value FROM app_settings')}
    for key in NOTIFY_SECRETS:
        enc = cfg.get(key)
        try:
            cfg[key] = jira.decrypt_token(app.secret_key, enc) if enc else None
        except jira.JiraError:
            cfg[key] = None
    return cfg


def _set_setting(db, key, value):
    if value is None:
        db.execute('DELETE FROM app_settings WHERE key = ?', (key,))
    else:
        db.execute('INSERT OR REPLACE INTO app_settings (key, value) VALUES (?, ?)', (key, str(value)))


def smtp_config(cfg):
    return {'host': cfg.get('smtp_host'), 'port': cfg.get('smtp_port'), 'security': cfg.get('smtp_security') or 'starttls',
            'login': cfg.get('smtp_login'), 'password': cfg.get('smtp_password'), 'sender': cfg.get('smtp_from')}


def telegram_client(cfg):
    return notify.Telegram(cfg.get('telegram_token'),
                           app.config.get('TELEGRAM_API') or os.environ.get('TELEGRAM_API') or 'https://api.telegram.org')


def _notify_settings_json(cfg):
    return {'smtp': {'host': cfg.get('smtp_host') or '', 'port': int(cfg.get('smtp_port') or 587),
                     'security': cfg.get('smtp_security') or 'starttls', 'login': cfg.get('smtp_login') or '',
                     'sender': cfg.get('smtp_from') or '', 'hasPassword': bool(cfg.get('smtp_password'))},
            'telegram': {'hasToken': bool(cfg.get('telegram_token')), 'botName': cfg.get('telegram_bot_name') or ''},
            'publicUrl': cfg.get('public_url') or ''}


def notify_log(db, user, project, kind, channel, status, message):
    db.execute('INSERT INTO notification_log (user_id, username, project_id, project_name, kind, channel, status, '
               'message, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
               (user['id'] if user else None, user['username'] if user else None, project['id'] if project else None,
                project['name'] if project else None, kind, channel, status, (message or '')[:500], _utc_now_iso()))


@app.route('/api/admin/notify-settings')
@admin_required
def api_get_notify_settings():
    return jsonify(_notify_settings_json(notify_settings(get_db())))


@app.route('/api/admin/notify-settings', methods=['PUT'])
@admin_required
def api_save_notify_settings():
    db = get_db()
    data = request.get_json() or {}
    smtp = data.get('smtp') if isinstance(data.get('smtp'), dict) else {}
    host = text_value(smtp.get('host'))
    security = smtp.get('security') or 'starttls'
    if security not in notify.SMTP_SECURITY:
        raise ValidationError('Защита соединения — без шифрования, STARTTLS или SSL')
    try:
        port = int(smtp.get('port') or (465 if security == 'ssl' else 587))
    except (TypeError, ValueError):
        raise ValidationError('Порт SMTP — число')
    if not 1 <= port <= 65535:
        raise ValidationError('Порт SMTP — от 1 до 65535')
    sender = text_value(smtp.get('sender'))
    if host and not re.fullmatch(r'[^@\s]+@[^@\s]+\.[^@\s]+', sender):
        raise ValidationError('Укажите адрес отправителя, например burndown@company.ru')
    public_url = text_value(data.get('publicUrl')).rstrip('/')
    if public_url and not re.match(r'^https?://\S+$', public_url):
        raise ValidationError('Адрес Burndown должен начинаться с http:// или https://')
    old = notify_settings(db)
    changes = []
    for key, value, label in (('smtp_host', host, 'smtpHost'), ('smtp_port', port if host else None, 'smtpPort'),
                              ('smtp_security', security if host else None, 'smtpSecurity'),
                              ('smtp_login', text_value(smtp.get('login')) or None, 'smtpLogin'),
                              ('smtp_from', sender or None, 'smtpFrom'), ('public_url', public_url or None, 'publicUrl')):
        value = value if value not in ('', None) else None
        if str(old.get(key) or '') != str(value or ''):
            changes.append({'field': label, 'old': old.get(key), 'new': None if value is None else str(value)})
        _set_setting(db, key, value)
    password = smtp.get('password') or ''
    if smtp.get('clearPassword') or (password and not host):
        _set_setting(db, 'smtp_password', None)
        changes.append({'field': 'smtpPassword', 'old': None, 'new': 'удалён'})
    elif password:
        _set_setting(db, 'smtp_password', jira.encrypt_token(app.secret_key, password))
        changes.append({'field': 'smtpPassword', 'old': None, 'new': 'изменён'})
    tg = data.get('telegram') if isinstance(data.get('telegram'), dict) else {}
    token = text_value(tg.get('token'))
    if tg.get('clearToken'):
        _set_setting(db, 'telegram_token', None)
        _set_setting(db, 'telegram_bot_name', None)
        changes.append({'field': 'telegramToken', 'old': None, 'new': 'удалён'})
    elif token:
        _set_setting(db, 'telegram_token', jira.encrypt_token(app.secret_key, token))
        try:
            _set_setting(db, 'telegram_bot_name', telegram_client({'telegram_token': token}).bot_name() or None)
        except notify.NotifyError:
            _set_setting(db, 'telegram_bot_name', None)
        changes.append({'field': 'telegramToken', 'old': None, 'new': 'изменён'})
    db.commit()
    if changes:
        audit_log('notify', 'update', 'notify_settings', None, None, changes, entity_name='Рассылки')
    return jsonify(_notify_settings_json(notify_settings(db)))


@app.route('/api/admin/notify-settings/test-email', methods=['POST'])
@admin_required
def api_test_email():
    db = get_db()
    to = text_value((request.get_json() or {}).get('to'))
    if not re.fullmatch(r'[^@\s]+@[^@\s]+\.[^@\s]+', to):
        raise ValidationError('Укажите email получателя')
    try:
        notify.send_email(smtp_config(notify_settings(db)), to, 'Burndown: тестовое письмо',
                          'Почта для уведомлений Burndown настроена правильно.')
    except notify.NotifyError as e:
        notify_log(db, get_current_user(), None, 'test', 'email', 'error', str(e))
        db.commit()
        return jsonify({'error': str(e)}), 502
    notify_log(db, get_current_user(), None, 'test', 'email', 'ok', to)
    db.commit()
    return jsonify({'ok': True})


@app.route('/api/admin/notify-settings/test-telegram', methods=['POST'])
@admin_required
def api_test_telegram():
    db = get_db()
    try:
        name = telegram_client(notify_settings(db)).bot_name()
    except notify.NotifyError as e:
        return jsonify({'error': str(e)}), 502
    _set_setting(db, 'telegram_bot_name', name or None)
    db.commit()
    return jsonify({'botName': name})


@app.route('/api/admin/notify-log')
@admin_required
def api_notify_log():
    rows = get_db().execute('SELECT * FROM notification_log ORDER BY id DESC LIMIT 100').fetchall()
    return jsonify([{'id': r['id'], 'username': r['username'], 'projectName': r['project_name'], 'kind': r['kind'],
                     'channel': r['channel'], 'status': r['status'], 'message': r['message'],
                     'createdAt': r['created_at']} for r in rows])


# ── Привязка Telegram в профиле

@app.route('/api/profile/telegram/link', methods=['POST'])
@login_required
def api_telegram_link():
    db = get_db()
    cfg = notify_settings(db)
    if not cfg.get('telegram_token'):
        return jsonify({'error': 'Администратор ещё не подключил Telegram-бота'}), 400
    bot = cfg.get('telegram_bot_name')
    if not bot:
        try:
            bot = telegram_client(cfg).bot_name()
        except notify.NotifyError as e:
            return jsonify({'error': str(e)}), 502
        _set_setting(db, 'telegram_bot_name', bot)
    code = secrets.token_hex(4)
    db.execute('UPDATE users SET telegram_link_code = ? WHERE id = ?', (code, session['user_id']))
    db.commit()
    return jsonify({'code': code, 'botName': bot, 'url': f'https://t.me/{bot}?start={code}'})


@app.route('/api/profile/telegram/check', methods=['POST'])
@login_required
def api_telegram_check():
    db = get_db()
    user = get_current_user()
    if not user['telegram_link_code']:
        raise ValidationError('Сначала получите ссылку для привязки')
    cfg = notify_settings(db)
    try:
        client = telegram_client(cfg)
        chat_id = client.find_start(user['telegram_link_code'])
        if chat_id:
            client.send_message(chat_id, '<b>Burndown</b>\nУведомления подключены. Настройте дайджест и оповещения '
                                         'в разделе «Уведомления» проекта.')
    except notify.NotifyError as e:
        return jsonify({'error': str(e)}), 502
    if not chat_id:
        return jsonify({'linked': False})
    db.execute('UPDATE users SET telegram_chat_id = ?, telegram_link_code = NULL WHERE id = ?', (chat_id, user['id']))
    db.commit()
    return jsonify({'linked': True})


@app.route('/api/profile/telegram', methods=['DELETE'])
@login_required
def api_telegram_unlink():
    db = get_db()
    db.execute('UPDATE users SET telegram_chat_id = NULL, telegram_link_code = NULL WHERE id = ?', (session['user_id'],))
    db.execute('UPDATE notification_subscriptions SET via_telegram = 0 WHERE user_id = ?', (session['user_id'],))
    db.commit()
    return jsonify({'linked': False})


# ── Подписка пользователя на проект

def _subscription(db, uid, pid):
    return db.execute('SELECT * FROM notification_subscriptions WHERE user_id = ? AND project_id = ?',
                      (uid, pid)).fetchone()


def _next_digest(sub):
    if not sub or not sub['digest_enabled']:
        return None
    return _next_run({'enabled': 1, 'days': sub['digest_days'], 'timezone': sub['timezone'],
                      'run_time': sub['digest_time'], 'last_run_date': sub['last_digest_date']})


def _subscription_json(db, user, sub):
    cfg = notify_settings(db)
    types = (sub['alert_types'] if sub else 'autofill,status,chance,scope,problems').split(',')
    return {
        'viaEmail': bool(sub['via_email']) if sub else True,
        'viaTelegram': bool(sub['via_telegram']) if sub else False,
        'digest': {'enabled': bool(sub and sub['digest_enabled']),
                   'days': _schedule_days(sub['digest_days']) if sub else [1],
                   'time': sub['digest_time'] if sub else '09:00', 'timezone': sub['timezone'] if sub else 'Europe/Moscow',
                   'pdf': bool(sub and sub['digest_pdf']), 'lastAt': sub['last_digest_at'] if sub else None,
                   'lastStatus': sub['last_digest_status'] if sub else None,
                   'lastMessage': sub['last_digest_message'] if sub else None, 'nextAt': _next_digest(sub)},
        'alerts': {'enabled': bool(sub and sub['alerts_enabled']), 'types': [t for t in types if t in ALERT_TYPES],
                   'chanceThreshold': sub['chance_threshold'] if sub else 50,
                   'scopeThreshold': sub['scope_threshold'] if sub else 10},
        'channels': {'email': user['email'] or '', 'emailReady': bool(cfg.get('smtp_host') and cfg.get('smtp_from')),
                     'telegramReady': bool(cfg.get('telegram_token')), 'botName': cfg.get('telegram_bot_name') or '',
                     'telegramLinked': bool(user['telegram_chat_id'])},
    }


@app.route('/api/projects/<int:pid>/notifications')
@login_required
def api_get_notifications(pid):
    if not can_view_project(session['user_id'], pid):
        return jsonify({'error': 'Нет доступа'}), 403
    db = get_db()
    return jsonify(_subscription_json(db, get_current_user(), _subscription(db, session['user_id'], pid)))


@app.route('/api/projects/<int:pid>/notifications', methods=['PUT'])
@login_required
def api_save_notifications(pid):
    if not can_view_project(session['user_id'], pid):
        return jsonify({'error': 'Нет доступа'}), 403
    db = get_db()
    user = get_current_user()
    data = request.get_json() or {}
    digest = data.get('digest') if isinstance(data.get('digest'), dict) else {}
    alerts = data.get('alerts') if isinstance(data.get('alerts'), dict) else {}
    via_email, via_tg = bool(data.get('viaEmail')), bool(data.get('viaTelegram'))
    days = digest.get('days') or []
    if not isinstance(days, list) or not all(isinstance(d, int) and 1 <= d <= 7 for d in days):
        raise ValidationError('Дни недели — числа от 1 (пн) до 7 (вс)')
    days = sorted(set(days))
    run_time = text_value(digest.get('time')) or '09:00'
    if not TIME_RE.match(run_time):
        raise ValidationError('Время дайджеста — в формате ЧЧ:ММ')
    tz_name = text_value(digest.get('timezone')) or 'Europe/Moscow'
    tz = _zone(tz_name)
    types = alerts.get('types') or []
    if not isinstance(types, list) or any(t not in ALERT_TYPES for t in types):
        raise ValidationError('Неизвестный вид оповещения')
    try:
        chance = int(alerts.get('chanceThreshold', 50))
        scope = int(alerts.get('scopeThreshold', 10))
    except (TypeError, ValueError):
        raise ValidationError('Пороги — целые числа')
    if not 1 <= chance <= 99 or not 1 <= scope <= 1000:
        raise ValidationError('Порог шанса — от 1 до 99%, порог роста объёма — от 1 до 1000%')
    digest_on, alerts_on = bool(digest.get('enabled')), bool(alerts.get('enabled'))
    if digest_on and not days:
        raise ValidationError('Выберите хотя бы один день для дайджеста')
    if alerts_on and not types:
        raise ValidationError('Выберите хотя бы один вид оповещений')
    if digest_on or alerts_on:
        _check_channels(db, user, via_email, via_tg)
    sub = _subscription(db, user['id'], pid)
    local = datetime.now(timezone.utc).astimezone(tz)
    last_date = sub['last_digest_date'] if sub else None
    # дайджест включили после сегодняшнего времени отправки — первый уйдёт в следующий день по расписанию
    if digest_on and local.strftime('%H:%M') >= run_time and not (sub and sub['digest_enabled']):
        last_date = local.date().isoformat()
    values = (1 if via_email else 0, 1 if via_tg else 0, 1 if digest_on else 0, ','.join(map(str, days)), run_time,
              tz_name, 1 if digest.get('pdf') else 0, last_date, 1 if alerts_on else 0, ','.join(types), chance, scope)
    if sub:
        db.execute('UPDATE notification_subscriptions SET via_email = ?, via_telegram = ?, digest_enabled = ?, '
                   'digest_days = ?, digest_time = ?, timezone = ?, digest_pdf = ?, last_digest_date = ?, '
                   'alerts_enabled = ?, alert_types = ?, chance_threshold = ?, scope_threshold = ? WHERE id = ?',
                   values + (sub['id'],))
    else:
        db.execute('INSERT INTO notification_subscriptions (via_email, via_telegram, digest_enabled, digest_days, '
                   'digest_time, timezone, digest_pdf, last_digest_date, alerts_enabled, alert_types, chance_threshold, '
                   'scope_threshold, user_id, project_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                   values + (user['id'], pid))
    db.commit()
    return jsonify(_subscription_json(db, user, _subscription(db, user['id'], pid)))


def _check_channels(db, user, via_email, via_tg):
    cfg = notify_settings(db)
    if not via_email and not via_tg:
        raise ValidationError('Выберите канал: почта или Telegram')
    if via_email and not (cfg.get('smtp_host') and cfg.get('smtp_from')):
        raise ValidationError('Администратор ещё не настроил отправку почты')
    if via_email and not user['email']:
        raise ValidationError('В вашем профиле не указан email — обратитесь к администратору')
    if via_tg and not cfg.get('telegram_token'):
        raise ValidationError('Администратор ещё не подключил Telegram-бота')
    if via_tg and not user['telegram_chat_id']:
        raise ValidationError('Сначала привяжите Telegram')


@app.route('/api/projects/<int:pid>/notifications/preview')
@login_required
def api_digest_preview(pid):
    if not can_view_project(session['user_id'], pid):
        return jsonify({'error': 'Нет доступа'}), 403
    db = get_db()
    overview = project_overview(db, pid)
    sub = _subscription(db, session['user_id'], pid)
    prev = json.loads(sub['digest_snapshot']) if sub and sub['digest_snapshot'] else None
    msg = render_digest(overview, prev, notify_settings(db).get('public_url'))
    return jsonify({'subject': msg['subject'], 'html': msg['html'], 'overview': overview})


@app.route('/api/projects/<int:pid>/notifications/test', methods=['POST'])
@login_required
def api_digest_test(pid):
    """Отправить дайджест сейчас по выбранным каналам подписки (для проверки)."""
    if not can_view_project(session['user_id'], pid):
        return jsonify({'error': 'Нет доступа'}), 403
    db = get_db()
    user = get_current_user()
    sub = _subscription(db, user['id'], pid)
    if not sub:
        raise ValidationError('Сначала сохраните настройки уведомлений')
    _check_channels(db, user, bool(sub['via_email']), bool(sub['via_telegram']))
    results = send_digest(db, user, sub, save_state=False)
    db.commit()
    code = 200 if all(r['status'] == 'ok' for r in results) else 502
    return jsonify({'results': results}), code


# ── Состояние проекта для писем: та же логика, что в интерфейсе (forecast.py)

def project_overview(db, pid):
    project = db.execute('SELECT * FROM projects WHERE id = ?', (pid,)).fetchone()
    settings_row = db.execute('SELECT * FROM settings WHERE project_id = ?', (pid,)).fetchone()
    settings = fc.settings_dict(settings_row)
    services = db.execute('SELECT * FROM services WHERE project_id = ? AND NOT COALESCE(is_done, 0) '
                          'ORDER BY sort_order, id', (pid,)).fetchall()
    rows = {s['id']: [dict(r) for r in db.execute('SELECT date, remaining, spent, estimate FROM entries WHERE '
                                                  'service_id = ? ORDER BY date', (s['id'],))] for s in services}
    history, latest = jira_composition(db, pid)
    epics = []
    for s in services:
        r = rows[s['id']]
        st = fc.compute_status([e['date'] for e in r], [e['remaining'] for e in r], s['target_date'] or None, settings)
        comp = latest.get(s['id'])
        hist = history.get(s['id']) or []
        epics.append({
            'id': s['id'], 'name': s['name'], 'etap': s['etap'], 'target': s['target_date'] or None,
            'remaining': st['lastRemaining'], 'status': st['status'], 'forecast': st['forecastZero'],
            'noBurn': st['noBurn'], 'probability': st['mc']['probability'] if st['mc'] else None,
            'p50': st['mc']['p50'] if st['mc'] else None, 'lastDate': st['lastDate'] if r else None,
            'scope': comp['scope'] if comp else None, 'problems': sum(comp['problems'].values()) if comp else None,
            'scopePrev': hist[-2]['scope'] if len(hist) >= 2 else None})
    stage_targets = {r['stage_name']: r['target_date'] for r in
                     db.execute('SELECT * FROM stage_targets WHERE project_id = ?', (pid,))}
    groups = {}
    for s in services:
        groups.setdefault(stage_key(s), []).append(s['id'])
    stages = []
    for name, ids in groups.items():
        dates, series = fc.aggregate_series([rows[i] for i in ids])
        st = fc.compute_status(dates, series['remaining'], stage_targets.get(name) or None, settings)
        stages.append({'name': name, 'target': stage_targets.get(name) or None, 'remaining': st['lastRemaining'],
                       'status': st['status'], 'forecast': st['forecastZero'], 'noBurn': st['noBurn'],
                       'probability': st['mc']['probability'] if st['mc'] else None})
    autofill = [{'button': b['name'], 'lastStatus': b['last_status'], 'lastMessage': b['last_message'],
                 'lastRunAt': b['last_run_at']} for b in db.execute(
        'SELECT b.name, s.last_status, s.last_message, s.last_run_at FROM jira_snapshot_buttons b '
        'JOIN jira_schedules s ON s.button_id = b.id WHERE b.project_id = ? AND s.enabled = 1 ORDER BY b.sort_order, b.id',
        (pid,))]
    counts = {k: sum(1 for e in epics if e['status'] == k) for k in ('ok', 'risk', 'danger')}
    return {'project': {'id': project['id'], 'name': project['name']}, 'date': datetime.now().date().isoformat(),
            'kpi': dict(counts, epics=len(epics), remaining=round(sum(e['remaining'] or 0 for e in epics), 2)),
            'epics': epics, 'stages': stages, 'autofill': autofill}


def _pct(p):
    return '—' if p is None else f'{fc.js_round(p * 100)}%'  # как в интерфейсе: 90,5 → 91


def _ru_num(v):
    """Число по-русски, как в интерфейсе: 1 188,3."""
    text = f'{v:,.2f}'.rstrip('0').rstrip('.')
    return text.replace(',', '\u00a0').replace('.', ',')


def _hours(v):
    return '—' if v is None else _ru_num(v) + ' ч'


def render_digest(ov, prev, public_url):
    """Дайджест проекта → subject, text, html (письмо), telegram (HTML Telegram). prev — состояние прошлого дайджеста."""
    prev_epics = {str(e['id']): e for e in (prev or {}).get('epics', [])}
    k = ov['kpi']
    worse = [e for e in ov['epics'] if e['status'] in ('risk', 'danger')]
    subject = f"Burndown · {ov['project']['name']}: " + (
        f"под угрозой {len(worse)} из {k['epics']} эпиков" if worse else f"сроки в порядке ({k['epics']} эпиков)")
    head = (f"Успевают: {k['ok']} · риск: {k['risk']} · не успевают: {k['danger']} · "
            f"остаток: {_ru_num(k['remaining'])} ч")

    def change(e):
        p = prev_epics.get(str(e['id']))
        if not p:
            return ''
        parts = []
        if p.get('status') != e['status'] and p.get('status') in STATUS_TITLES:
            parts.append(f"{STATUS_TITLES[p['status']]} → {STATUS_TITLES[e['status']]}")
        if p.get('probability') is not None and e['probability'] is not None:
            d = fc.js_round((e['probability'] - p['probability']) * 100)
            if d:
                parts.append(f"шанс {'+' if d > 0 else ''}{d} п.п.")
        if p.get('remaining') is not None and e['remaining'] is not None and p['remaining'] != e['remaining']:
            d = round(e['remaining'] - p['remaining'], 2)
            parts.append(f"остаток {'+' if d > 0 else ''}{_ru_num(d)} ч")
        return ', '.join(parts)

    lines = [subject, head, '']
    for e in ov['epics']:
        ch = change(e)
        lines.append(f"• {e['name']} — {STATUS_TITLES[e['status']]}; остаток {_hours(e['remaining'])}; "
                     f"прогноз {_fmt_date(e['forecast']) if e['forecast'] else ('не сгорает' if e['noBurn'] else '—')}; "
                     f"цель {_fmt_date(e['target'])}; шанс {_pct(e['probability'])}"
                     + (f"; с прошлого дайджеста: {ch}" if ch else ''))
    errors = [a for a in ov['autofill'] if a['lastStatus'] == 'error']
    if errors:
        lines += ['', 'Автозаполнение с ошибкой:'] + [f"• {a['button']}: {a['lastMessage']}" for a in errors]
    if public_url:
        lines += ['', f'Открыть Burndown: {public_url}']
    text = '\n'.join(lines)

    esc = notify.esc
    row_html = []
    for e in ov['epics']:
        ch = change(e)
        color = STATUS_COLORS[e['status']]
        forecast_text = _fmt_date(e['forecast']) if e['forecast'] else ('не сгорает' if e['noBurn'] else '—')
        row_html.append(
            f"<tr><td style='padding:6px 8px;border-bottom:1px solid #e3e7f0'>{esc(e['name'])}"
            + (f"<div style='font-size:12px;color:#964219'>{esc(ch)}</div>" if ch else '') + "</td>"
            f"<td style='padding:6px 8px;border-bottom:1px solid #e3e7f0;color:{color};font-weight:600;white-space:nowrap'>"
            f"{esc(STATUS_TITLES[e['status']])}</td>"
            f"<td style='padding:6px 8px;border-bottom:1px solid #e3e7f0;text-align:right;white-space:nowrap'>{esc(_hours(e['remaining']))}</td>"
            f"<td style='padding:6px 8px;border-bottom:1px solid #e3e7f0;white-space:nowrap'>{esc(forecast_text)}</td>"
            f"<td style='padding:6px 8px;border-bottom:1px solid #e3e7f0;white-space:nowrap'>{esc(_fmt_date(e['target']))}</td>"
            f"<td style='padding:6px 8px;border-bottom:1px solid #e3e7f0;text-align:right'>{esc(_pct(e['probability']))}</td></tr>")
    th = "style='padding:6px 8px;text-align:left;font-size:12px;color:#5c6470;border-bottom:2px solid #01696f'"
    html_body = (
        "<div style='font-family:Arial,sans-serif;color:#1c2126;max-width:760px'>"
        f"<h2 style='color:#01696f;margin:0 0 4px'>{esc(ov['project']['name'])}</h2>"
        f"<div style='color:#5c6470;margin-bottom:12px'>Дайджест Burndown на {esc(_fmt_date(ov['date']))}</div>"
        f"<p style='margin:0 0 12px'><b>{esc(head)}</b></p>"
        "<table style='border-collapse:collapse;width:100%;font-size:14px'><thead><tr>"
        f"<th {th}>Эпик</th><th {th}>Статус</th><th {th}>Остаток</th><th {th}>Прогноз «0»</th><th {th}>Цель</th>"
        f"<th {th}>Шанс успеть</th></tr></thead><tbody>{''.join(row_html)}</tbody></table>"
        + (("<p style='margin:16px 0 4px'><b>Автозаполнение с ошибкой</b></p><ul>"
            + ''.join(f"<li>{esc(a['button'])}: {esc(a['lastMessage'])}</li>" for a in errors) + '</ul>') if errors else '')
        + (f"<p style='margin-top:16px'><a href='{esc(public_url)}' style='color:#01696f'>Открыть Burndown</a></p>"
           if public_url else '')
        + "<p style='color:#99a1aa;font-size:12px;margin-top:16px'>Настроить или отключить дайджест — в разделе "
          "«Уведомления» проекта.</p></div>")

    tg = [f"<b>{esc(ov['project']['name'])}</b> — дайджест на {esc(_fmt_date(ov['date']))}", esc(head), '']
    icons = {'ok': '✅', 'risk': '⚠️', 'danger': '⛔', 'notarget': '◽', 'unknown': '◽'}
    for e in ov['epics']:
        ch = change(e)
        tg.append(f"{icons[e['status']]} <b>{esc(e['name'])}</b>: {esc(_hours(e['remaining']))}, "
                  f"шанс {esc(_pct(e['probability']))}" + (f"\n    <i>{esc(ch)}</i>" if ch else ''))
    if errors:
        tg += ['', '<b>Автозаполнение с ошибкой:</b>'] + [f"• {esc(a['button'])}: {esc(a['lastMessage'])}" for a in errors]
    if public_url:
        tg += ['', f"<a href=\"{esc(public_url)}\">Открыть Burndown</a>"]
    return {'subject': subject, 'text': text, 'html': html_body, 'telegram': '\n'.join(tg)}


def _snapshot_for_digest(ov):
    return json.dumps({'epics': [{k: e[k] for k in ('id', 'status', 'probability', 'remaining')} for e in ov['epics']]},
                      ensure_ascii=False)


def deliver(db, user, sub, project, kind, subject, text, html_body, telegram_html, attachments=()):
    """Отправить по каналам подписки → [{channel, status, message}]; каждая попытка — в журнал."""
    cfg = notify_settings(db)
    results = []
    if sub['via_email']:
        try:
            notify.send_email(smtp_config(cfg), user['email'], subject, text, html_body, attachments)
            results.append({'channel': 'email', 'status': 'ok', 'message': user['email']})
        except notify.NotifyError as e:
            results.append({'channel': 'email', 'status': 'error', 'message': str(e)})
    if sub['via_telegram']:
        try:
            if not user['telegram_chat_id']:
                raise notify.NotifyError('Telegram не привязан')
            client = telegram_client(cfg)
            client.send_message(user['telegram_chat_id'], telegram_html)
            for name, data, _mime in attachments:
                client.send_document(user['telegram_chat_id'], name, data, subject)
            results.append({'channel': 'telegram', 'status': 'ok', 'message': ''})
        except notify.NotifyError as e:
            results.append({'channel': 'telegram', 'status': 'error', 'message': str(e)})
    for r in results:
        notify_log(db, user, project, kind, r['channel'], r['status'], r['message'])
    return results


def send_digest(db, user, sub, save_state=True):
    project = db.execute('SELECT * FROM projects WHERE id = ?', (sub['project_id'],)).fetchone()
    ov = project_overview(db, project['id'])
    prev = json.loads(sub['digest_snapshot']) if sub['digest_snapshot'] else None
    msg = render_digest(ov, prev, notify_settings(db).get('public_url'))
    attachments = []
    if sub['digest_pdf']:
        data, filename = build_project_pdf(db, project, 'dashboard')
        attachments.append((filename, data, 'application/pdf'))
    results = deliver(db, user, sub, project, 'digest', msg['subject'], msg['text'], msg['html'], msg['telegram'],
                      attachments)
    ok = all(r['status'] == 'ok' for r in results)
    message = '; '.join(('почта' if r['channel'] == 'email' else 'Telegram') + ': ' +
                        ('отправлено' if r['status'] == 'ok' else r['message']) for r in results)
    db.execute('UPDATE notification_subscriptions SET last_digest_at = ?, last_digest_status = ?, last_digest_message = ?'
               + (', digest_snapshot = ?' if save_state and ok else '') + ' WHERE id = ?',
               (_utc_now_iso(), 'ok' if ok else 'error', message)
               + ((_snapshot_for_digest(ov),) if save_state and ok else ()) + (sub['id'],))
    return results


def run_due_digests(now_utc=None):
    """Дайджесты по расписанию подписок: один раз в день на подписку (день захватывается атомарно, как автозаполнение)."""
    now_utc = now_utc or datetime.now(timezone.utc)
    db = get_db()
    sent = 0
    for sub in db.execute('SELECT * FROM notification_subscriptions WHERE digest_enabled = 1').fetchall():
        try:
            local = now_utc.astimezone(ZoneInfo(sub['timezone']))
        except (ZoneInfoNotFoundError, ValueError):
            continue
        today = local.date().isoformat()
        if (local.isoweekday() not in _schedule_days(sub['digest_days']) or local.strftime('%H:%M') < sub['digest_time']
                or sub['last_digest_date'] == today):
            continue
        claimed = db.execute('UPDATE notification_subscriptions SET last_digest_date = ? WHERE id = ? AND digest_enabled = 1 '
                             'AND (last_digest_date IS NULL OR last_digest_date != ?)', (today, sub['id'], today)).rowcount
        db.commit()
        if not claimed:
            continue
        user = db.execute('SELECT * FROM users WHERE id = ?', (sub['user_id'],)).fetchone()
        if not user or user_project_role(user['id'], sub['project_id']) is None:
            continue  # доступ к проекту отозван
        try:
            send_digest(db, user, db.execute('SELECT * FROM notification_subscriptions WHERE id = ?', (sub['id'],)).fetchone())
        except Exception:  # noqa: BLE001 — сбой одного дайджеста не останавливает остальные
            db.rollback()
            logger.exception('digest %s failed', sub['id'])
        db.commit()
        sent += 1
    return sent


# ── Оповещения: сравнение состояния проекта с прошлым

def project_state(ov):
    state = {}
    for e in ov['epics']:
        state[f"epic:{e['id']}"] = {'name': e['name'], 'status': e['status'], 'probability': e['probability'],
                                    'scope': e['scope'], 'problems': e['problems'], 'kind': 'epic'}
    for st in ov['stages']:
        state[f"stage:{st['name']}"] = {'name': st['name'], 'status': st['status'], 'probability': st['probability'],
                                        'scope': None, 'problems': None, 'kind': 'stage'}
    return state


def alert_events(old, new, sub):
    """События для подписчика по его видам оповещений и порогам: [(вид, текст)]."""
    types = set((sub['alert_types'] or '').split(','))
    events = []
    for key, n in new.items():
        o = old.get(key)
        if not o:
            continue
        label = ('Эпик' if n['kind'] == 'epic' else 'Этап') + f" «{n['name']}»"
        if 'status' in types and n['status'] in fc.STATUS_RANK and o['status'] in fc.STATUS_RANK \
                and fc.STATUS_RANK[n['status']] > fc.STATUS_RANK[o['status']]:
            events.append(('status', f"{label}: {STATUS_TITLES[o['status']]} → {STATUS_TITLES[n['status']]}"))
        thr = sub['chance_threshold'] / 100
        if 'chance' in types and o['probability'] is not None and n['probability'] is not None \
                and o['probability'] >= thr > n['probability']:
            events.append(('chance', f"{label}: шанс успеть {_pct(o['probability'])} → {_pct(n['probability'])} "
                                     f"(порог {sub['chance_threshold']}%)"))
        if 'scope' in types and o['scope'] and n['scope'] is not None \
                and (n['scope'] - o['scope']) / o['scope'] * 100 >= sub['scope_threshold']:
            events.append(('scope', f"{label}: объём задач Jira вырос на "
                                    f"{fc.js_round((n['scope'] - o['scope']) / o['scope'] * 100)}% "
                                    f"({_ru_num(o['scope'])} → {_ru_num(n['scope'])} ч)"))
        if 'problems' in types and n['problems'] is not None and n['problems'] > (o['problems'] or 0):
            events.append(('problems', f"{label}: задач с неполными данными {o['problems'] or 0} → {n['problems']}"))
    return events


def _project_fingerprint(db, pid):
    audit = db.execute('SELECT MAX(id) FROM audit_log WHERE project_id = ?', (pid,)).fetchone()[0]
    snap = db.execute('SELECT MAX(s.id) FROM jira_issue_snapshots s JOIN services v ON v.id = s.service_id '
                      'WHERE v.project_id = ?', (pid,)).fetchone()[0]
    return f'{audit or 0}:{snap or 0}'


def run_alert_checks():
    """Проекты с подписками на оповещения, где что-то изменилось (реестр, срезы Jira): сравнить состояние с прошлым
    и поставить письма в очередь. Проект обрабатывает один процесс — отпечаток изменений захватывается атомарно."""
    db = get_db()
    queued = 0
    pids = [r[0] for r in db.execute('SELECT DISTINCT project_id FROM notification_subscriptions WHERE alerts_enabled = 1')]
    for pid in pids:
        fp = _project_fingerprint(db, pid)
        db.execute('INSERT OR IGNORE INTO notification_marks (project_id) VALUES (?)', (pid,))
        claimed = db.execute('UPDATE notification_marks SET fingerprint = ?, checked_at = ? WHERE project_id = ? '
                             'AND (fingerprint IS NULL OR fingerprint != ?)', (fp, _utc_now_iso(), pid, fp)).rowcount
        db.commit()
        if not claimed:
            continue
        ov = project_overview(db, pid)
        new = project_state(ov)
        old = {r['entity']: dict(r) for r in db.execute('SELECT * FROM notification_state WHERE project_id = ?', (pid,))}
        for sub in db.execute('SELECT * FROM notification_subscriptions WHERE project_id = ? AND alerts_enabled = 1',
                              (pid,)).fetchall():
            events = alert_events(old, new, sub)
            if events:
                queue_alert(db, sub['user_id'], pid, ov['project']['name'], [text for _kind, text in events])
                queued += 1
        db.execute('DELETE FROM notification_state WHERE project_id = ?', (pid,))
        db.executemany('INSERT INTO notification_state (project_id, entity, name, status, probability, scope, problems) '
                       'VALUES (?, ?, ?, ?, ?, ?, ?)',
                       [(pid, key, v['name'], v['status'], v['probability'], v['scope'], v['problems'])
                        for key, v in new.items()])
        db.commit()
    return queued


def queue_alert(db, user_id, pid, project_name, lines):
    subject = f'Burndown · {project_name}: ' + (lines[0] if len(lines) == 1 else f'{len(lines)} изменения в сроках и данных')
    text = '\n'.join('• ' + line for line in lines)
    html_body = ("<div style='font-family:Arial,sans-serif;color:#1c2126'>"
                 f"<h3 style='color:#01696f;margin:0 0 8px'>{notify.esc(project_name)}</h3><ul>"
                 + ''.join(f'<li>{notify.esc(line)}</li>' for line in lines) + '</ul></div>')
    db.execute('INSERT INTO notification_outbox (user_id, project_id, kind, subject, body_text, body_html, created_at) '
               "VALUES (?, ?, 'alert', ?, ?, ?, ?)", (user_id, pid, subject, text, html_body, _utc_now_iso()))


def queue_autofill_error(db, button_row, message):
    """Сбой автозаполнения — подписчикам проекта с оповещением «autofill»."""
    project = db.execute('SELECT name FROM projects WHERE id = ?', (button_row['project_id'],)).fetchone()
    for sub in db.execute('SELECT * FROM notification_subscriptions WHERE project_id = ? AND alerts_enabled = 1',
                          (button_row['project_id'],)).fetchall():
        if 'autofill' in (sub['alert_types'] or '').split(','):
            queue_alert(db, sub['user_id'], button_row['project_id'], project['name'],
                        [f"Автозаполнение «{button_row['name']}» не выполнено: {message}"])


def deliver_outbox():
    db = get_db()
    done = 0
    for item in db.execute("SELECT * FROM notification_outbox WHERE status = 'pending' ORDER BY id").fetchall():
        claimed = db.execute("UPDATE notification_outbox SET status = 'sending' WHERE id = ? AND status = 'pending'",
                             (item['id'],)).rowcount
        db.commit()
        if not claimed:
            continue
        user = db.execute('SELECT * FROM users WHERE id = ?', (item['user_id'],)).fetchone()
        sub = _subscription(db, item['user_id'], item['project_id'])
        project = db.execute('SELECT * FROM projects WHERE id = ?', (item['project_id'],)).fetchone()
        status = 'skipped'
        if user and sub and project and user_project_role(user['id'], project['id']) is not None:
            tg = f"<b>{notify.esc(item['subject'])}</b>\n{notify.esc(item['body_text'])}"
            results = deliver(db, user, sub, project, item['kind'], item['subject'], item['body_text'],
                              item['body_html'], tg)
            status = 'sent' if results and all(r['status'] == 'ok' for r in results) else 'error'
        db.execute('UPDATE notification_outbox SET status = ? WHERE id = ?', (status, item['id']))
        db.commit()
        done += 1
    return done


def run_notifications(now_utc=None):
    """Шаг планировщика: оповещения, дайджесты, доставка очереди. Сбой одной части не мешает остальным."""
    for step in (run_alert_checks, lambda: run_due_digests(now_utc), deliver_outbox):
        try:
            step()
        except Exception:  # noqa: BLE001
            get_db().rollback()
            logger.exception('notifications step failed')


def _scheduler_loop():
    while True:
        time.sleep(SCHEDULER_INTERVAL)
        try:
            with app.app_context():
                run_due_schedules()
                run_notifications()
        except Exception:  # noqa: BLE001
            logger.exception('scheduler tick failed')


def start_scheduler():
    """Фоновый планировщик автозаполнения (отключается BURNDOWN_SCHEDULER=0, например в тестах)."""
    if os.environ.get('BURNDOWN_SCHEDULER', '1') == '0':
        return None
    thread = threading.Thread(target=_scheduler_loop, name='burndown-autofill', daemon=True)
    thread.start()
    return thread


# ─── API: Users (admin only) ─────────────────────────────────────

USER_ROLES = ('user', 'manager', 'admin')

@app.route('/api/users')
@admin_required
def api_get_users():
    db = get_db()
    users = db.execute('SELECT id, username, display_name, email, position, photo, role FROM users ORDER BY id').fetchall()
    return jsonify([{
        'id': u['id'],
        'username': u['username'],
        'displayName': u['display_name'],
        'email': u['email'] or '',
        'position': u['position'] or '',
        'photo': u['photo'] or '',
        'role': u['role']
    } for u in users])


@app.route('/api/users', methods=['POST'])
@admin_required
def api_create_user():
    data = request.get_json() or {}
    username = text_value(data.get('username'))
    password = data.get('password') or ''
    if not username or not password or not isinstance(password, str):
        return jsonify({'error': 'Укажите логин и пароль'}), 400
    parse_photo(data.get('photo'))

    db = get_db()
    existing = db.execute('SELECT id FROM users WHERE username = ?', (username,)).fetchone()
    if existing:
        return jsonify({'error': 'Пользователь с таким логином уже существует'}), 400

    if data.get('role', 'user') not in USER_ROLES:
        return jsonify({'error': 'Недопустимая роль'}), 400
    cur = db.execute(
        'INSERT INTO users (username, password_hash, display_name, email, position, photo, role) VALUES (?, ?, ?, ?, ?, ?, ?)',
        (username, generate_password_hash(password), data.get('displayName', ''), data.get('email', ''), data.get('position', ''), data.get('photo', ''), data.get('role', 'user'))
    )
    db.commit()
    audit_log('users', 'create', 'user', cur.lastrowid, None, [{'field': 'username', 'old': None, 'new': username}])
    return jsonify({'id': cur.lastrowid, 'username': username, 'displayName': data.get('displayName', ''), 'email': data.get('email', ''), 'position': data.get('position', ''), 'photo': data.get('photo', ''), 'role': data.get('role', 'user')})


@app.route('/api/users/<int:uid>', methods=['PUT'])
@admin_required
def api_update_user(uid):
    data = request.get_json() or {}
    db = get_db()
    user = db.execute('SELECT * FROM users WHERE id = ?', (uid,)).fetchone()
    if not user:
        return jsonify({'error': 'Пользователь не найден'}), 404

    new_username = text_value(data.get('username', user['username']))
    if new_username != user['username']:
        existing_un = db.execute('SELECT id FROM users WHERE username = ? AND id != ?', (new_username, uid)).fetchone()
        if existing_un:
            return jsonify({'error': 'Пользователь с таким логином уже существует'}), 400
    display_name = data.get('displayName', user['display_name'])
    email = data.get('email', user['email'])
    position = data.get('position', user['position'])
    role = data.get('role', user['role'])
    if role not in USER_ROLES:
        return jsonify({'error': 'Недопустимая роль'}), 400
    if not new_username:
        return jsonify({'error': 'Укажите логин'}), 400
    if uid == session['user_id'] and role != 'admin':
        return jsonify({'error': 'Нельзя снять с себя роль администратора'}), 400
    photo = parse_photo(data.get('photo', user['photo']))
    password = data.get('password')

    if password:
        db.execute(
            'UPDATE users SET username = ?, display_name = ?, email = ?, position = ?, photo = ?, role = ?, password_hash = ? WHERE id = ?',
            (new_username, display_name, email, position, photo, role, generate_password_hash(password), uid)
        )
    else:
        db.execute(
            'UPDATE users SET username = ?, display_name = ?, email = ?, position = ?, photo = ?, role = ? WHERE id = ?',
            (new_username, display_name, email, position, photo, role, uid)
        )
    db.commit()
    changes = []
    for field, old_val, new_val in [
        ('username', user['username'], new_username),
        ('displayName', user['display_name'], display_name),
        ('email', user['email'], email),
        ('position', user['position'], position),
        ('role', user['role'], role),
    ]:
        if text_change(old_val, new_val):
            changes.append(text_change(old_val, new_val, field))
    if changes:
        audit_log('users', 'update', 'user', uid, None, changes)
    return jsonify({'success': True})


@app.route('/api/users/<int:uid>', methods=['DELETE'])
@admin_required
def api_delete_user(uid):
    if uid == session['user_id']:
        return jsonify({'error': 'Нельзя удалить самого себя'}), 400
    db = get_db()
    user = db.execute('SELECT username FROM users WHERE id = ?', (uid,)).fetchone()
    if not user:
        return jsonify({'error': 'Пользователь не найден'}), 404
    audit_log('users', 'delete', 'user', uid, None, [{'field': 'username', 'old': user['username'], 'new': None}])
    db.execute('DELETE FROM users WHERE id = ?', (uid,))
    db.commit()
    return jsonify({'success': True})


# ─── API: User-Project assignments (admin only) ──────────────────

@app.route('/api/users/<int:uid>/projects')
@admin_required
def api_get_user_projects(uid):
    db = get_db()
    ups = db.execute('''
        SELECT up.*, p.name as project_name
        FROM user_projects up
        JOIN projects p ON up.project_id = p.id
        WHERE up.user_id = ?
        ORDER BY p.id
    ''', (uid,)).fetchall()
    return jsonify([{
        'projectId': r['project_id'],
        'projectName': r['project_name'],
        'role': r['role']
    } for r in ups])


@app.route('/api/users/<int:uid>/projects', methods=['POST'])
@admin_required
def api_assign_user_project(uid):
    data = request.get_json() or {}
    project_id = data.get('projectId')
    role = data.get('role', 'user')
    if not project_id:
        return jsonify({'error': 'Укажите проект'}), 400
    if role not in ('user', 'manager'):
        return jsonify({'error': 'Недопустимая роль'}), 400

    db = get_db()
    if not db.execute('SELECT 1 FROM users WHERE id = ?', (uid,)).fetchone():
        return jsonify({'error': 'Пользователь не найден'}), 404
    if not db.execute('SELECT 1 FROM projects WHERE id = ?', (project_id,)).fetchone():
        return jsonify({'error': 'Проект не найден'}), 404
    existing = db.execute(
        'SELECT * FROM user_projects WHERE user_id = ? AND project_id = ?',
        (uid, project_id)
    ).fetchone()
    if existing:
        db.execute('UPDATE user_projects SET role = ? WHERE id = ?', (role, existing['id']))
    else:
        db.execute(
            'INSERT INTO user_projects (user_id, project_id, role) VALUES (?, ?, ?)',
            (uid, project_id, role)
        )
    db.commit()
    return jsonify({'success': True})


@app.route('/api/users/<int:uid>/projects/<int:pid>', methods=['DELETE'])
@admin_required
def api_unassign_user_project(uid, pid):
    db = get_db()
    db.execute('DELETE FROM user_projects WHERE user_id = ? AND project_id = ?', (uid, pid))
    db.commit()
    return jsonify({'success': True})


# ─── API: Profile (current user) ─────────────────────────────────

@app.route('/api/profile', methods=['PUT'])
@login_required
def api_update_profile():
    data = request.get_json() or {}
    db = get_db()
    uid = session['user_id']
    user = db.execute('SELECT * FROM users WHERE id = ?', (uid,)).fetchone()
    if not user:
        return jsonify({'error': 'Пользователь не найден'}), 404
    photo = parse_photo(data.get('photo', user['photo']))
    password = data.get('password')
    # ФИО, email и должность меняет только администратор (как в интерфейсе); остальным — фото и пароль
    is_admin = user['role'] == 'admin'
    display_name = data.get('displayName', user['display_name']) if is_admin else user['display_name']
    email = data.get('email', user['email']) if is_admin else user['email']
    position = data.get('position', user['position']) if is_admin else user['position']
    if password:
        db.execute('UPDATE users SET photo = ?, display_name = ?, email = ?, position = ?, password_hash = ? WHERE id = ?',
                   (photo, display_name, email, position, generate_password_hash(password), uid))
    else:
        db.execute('UPDATE users SET photo = ?, display_name = ?, email = ?, position = ? WHERE id = ?',
                   (photo, display_name, email, position, uid))
    db.commit()
    changes = []
    for field, old_val, new_val in [
        ('displayName', user['display_name'], display_name),
        ('email', user['email'], email),
        ('position', user['position'], position),
    ]:
        if text_change(old_val, new_val):
            changes.append(text_change(old_val, new_val, field))
    if changes:
        audit_log('profile', 'update', 'user', uid, None, changes)
    return jsonify({'success': True})


# ─── API: Export/Import ──────────────────────────────────────────

@app.route('/api/projects/<int:pid>/export')
@admin_required
def api_export_project(pid):
    if not can_view_project(session['user_id'], pid):
        return jsonify({'error': 'Нет доступа'}), 403
    db = get_db()
    project = db.execute('SELECT * FROM projects WHERE id = ?', (pid,)).fetchone()
    if not project:
        return jsonify({'error': 'Проект не найден'}), 404
    payload = json.dumps(dict({'project': {'name': project['name'], 'description': project['description']}},
                              **project_payload(db, pid)), ensure_ascii=False, indent=2)
    filename = f"Burndown_export_{datetime.now().strftime('%Y-%m-%d_%H%M')}.json"
    return Response(payload, mimetype='application/json',
                    headers={'Content-Disposition': f"attachment; filename=\"{filename}\"; filename*=UTF-8''{quote(filename)}"})


# ─── API: Audit log (admin only) ─────────────────────────────────

AUDIT_PAGE_SIZE = 200


@app.route('/api/audit')
@admin_required
def api_get_audit_log():
    db = get_db()
    # Build query with optional filters
    conditions = []
    params = []
    username = request.args.get('username', '').strip()
    page = request.args.get('page', '').strip()
    project_id = request.args.get('projectId', '').strip()
    if username:
        conditions.append('username = ?')
        params.append(username)
    if page:
        conditions.append('page = ?')
        params.append(page)
    if project_id.isdigit():
        conditions.append('project_id = ?')
        params.append(int(project_id))
    before_id = request.args.get('beforeId', '').strip()
    if before_id.isdigit():
        conditions.append('id < ?')
        params.append(int(before_id))
    limit = request.args.get('limit', '')
    limit = min(max(int(limit), 1), 1000) if limit.isdigit() else AUDIT_PAGE_SIZE
    where_clause = ' WHERE ' + ' AND '.join(conditions) if conditions else ''
    rows = db.execute(
        f'SELECT * FROM audit_log{where_clause} ORDER BY id DESC LIMIT ?',
        params + [limit + 1]
    ).fetchall()
    has_more = len(rows) > limit
    rows = rows[:limit]
    # Get unique values for filters
    usernames = [r['username'] for r in db.execute('SELECT DISTINCT username FROM audit_log ORDER BY username').fetchall()]
    pages = [r['page'] for r in db.execute('SELECT DISTINCT page FROM audit_log ORDER BY page').fetchall()]
    projects = db.execute('SELECT id, name FROM projects ORDER BY id').fetchall()
    return jsonify({
        'entries': [{
            'id': r['id'],
            'username': r['username'],
            'projectId': r['project_id'],
            'projectName': r['project_name'],
            'page': r['page'],
            'action': r['action'],
            'entityType': r['entity_type'],
            'entityId': r['entity_id'],
            'entityName': r['entity_name'],
            'fieldName': r['field_name'],
            'oldValue': r['old_value'],
            'newValue': r['new_value'],
            'createdAt': r['created_at'],
        } for r in rows],
        'hasMore': has_more,
        'filters': {
            'usernames': usernames,
            'pages': pages,
            'projects': [{'id': p['id'], 'name': p['name']} for p in projects],
        }
    })


# ─── API: PDF export ─────────────────────────────────────────────

@app.route('/api/projects/<int:pid>/pdf')
@login_required
def api_export_pdf(pid):
    if not can_view_project(session['user_id'], pid):
        return jsonify({'error': 'Нет доступа к проекту'}), 403
    page_type = request.args.get('page', 'dashboard')
    if page_type not in PDF_PAGES:
        return jsonify({'error': 'Неизвестный раздел'}), 400
    db = get_db()
    project = db.execute('SELECT * FROM projects WHERE id = ?', (pid,)).fetchone()
    if not project:
        return jsonify({'error': 'Проект не найден'}), 404
    pdf_bytes, filename = build_project_pdf(db, project, page_type, request.args.get('epics'),
                                            request.args.get('showDone') == '1')
    filename_ascii = filename.encode('ascii', 'replace').decode('ascii')
    return Response(pdf_bytes, mimetype='application/pdf',
                    headers={'Content-Disposition': f"attachment; filename=\"{filename_ascii}\"; filename*=UTF-8''{quote(filename)}"})


def build_project_pdf(db, project, page_type, epics_arg=None, show_done=False):
    """PDF-отчёт раздела → (байты, имя файла). epics_arg — выбранные эпики дашборда («1,2»), show_done — сданные в своде."""
    pid = project['id']
    services = db.execute('SELECT * FROM services WHERE project_id = ? ORDER BY sort_order, id', (pid,)).fetchall()
    service_ids = [s['id'] for s in services]
    entries = []
    if service_ids:
        placeholders = ','.join('?' * len(service_ids))
        entries = db.execute(f'SELECT * FROM entries WHERE service_id IN ({placeholders}) ORDER BY date', service_ids).fetchall()
    stage_targets = {r['stage_name']: r['target_date'] for r in
                     db.execute('SELECT * FROM stage_targets WHERE project_id = ?', (pid,)).fetchall()}

    # Фильтры интерфейса: выбранные эпики дашборда, учёт сданных эпиков в своде по этапам
    if page_type == 'dashboard' and epics_arg is not None:
        selected = {int(x) for x in epics_arg.split(',') if x.strip().isdigit()}
        services = [s for s in services if s['id'] in selected]
    elif page_type == 'stages' and not show_done:
        services = [s for s in services if not s['is_done']]

    by_service = {}
    for e in entries:
        by_service.setdefault(e['service_id'], []).append(e)

    def last_value(sid, field):
        """Последнее заполненное значение поля по эпику (как на дашборде)."""
        for e in reversed(by_service.get(sid, [])):
            if e[field] is not None:
                return e[field]
        return None

    def sum_last(svcs, field):
        vals = [last_value(s['id'], field) for s in svcs]
        vals = [v for v in vals if v is not None]
        return sum(vals) if vals else None

    page_title = PDF_PAGES[page_type][0]
    now_str = datetime.now().strftime('%d.%m.%Y %H:%M')

    from fpdf import FPDF
    font_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static', 'fonts')
    pdf = FPDF(orientation='L', unit='mm', format='A4')
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_font('DejaVu', '', os.path.join(font_dir, 'dejavu.ttf'))
    pdf.add_font('DejaVu', 'B', os.path.join(font_dir, 'dejavu_bold.ttf'))
    pdf.add_page()
    pdf.set_font('DejaVu', 'B', 16)
    pdf.set_text_color(*PDF_PRIMARY)
    pdf.cell(0, 8, page_title, new_x='LMARGIN', new_y='NEXT')
    pdf.set_font('DejaVu', '', 10)
    pdf.set_text_color(*PDF_MUTED)
    subtitle = f"Проект: {project['name']}" + (f" — {project['description']}" if project['description'] else '')
    pdf.cell(0, 5, subtitle, new_x='LMARGIN', new_y='NEXT')
    pdf.cell(0, 5, f"Сформировано: {now_str}", new_x='LMARGIN', new_y='NEXT')
    pdf.ln(3)
    pdf.set_draw_color(*PDF_DIVIDER)
    pdf.line(pdf.l_margin, pdf.get_y(), pdf.w - pdf.r_margin, pdf.get_y())
    pdf.ln(3)

    if page_type == 'dashboard':
        total_remaining = sum_last(services, 'remaining')
        total_spent = sum_last(services, 'spent')
        total_estimate = sum_last(services, 'estimate')

        _pdf_heading(pdf, 'Ключевые показатели')
        kpi_data = [
            ('Остаток, ч', _fmt_num(total_remaining)),
            ('Затрачено, ч', _fmt_num(total_spent)),
            ('Оценка, ч', _fmt_num(total_estimate)),
            ('Эпиков', str(len(services))),
        ]
        col_w = (pdf.w - pdf.l_margin - pdf.r_margin) / len(kpi_data)
        pdf.set_font('DejaVu', '', 8)
        pdf.set_text_color(*PDF_MUTED)
        for label, _ in kpi_data:
            pdf.cell(col_w, 4, label)
        pdf.ln(4)
        pdf.set_font('DejaVu', 'B', 14)
        pdf.set_text_color(*PDF_TEXT)
        for _, val in kpi_data:
            pdf.cell(col_w, 7, val)
        pdf.ln(9)

        _pdf_heading(pdf, 'Сводная таблица по эпикам (последний замер)')
        rows = [[s['etap'], s['name'], _fmt_date(s['target_date']),
                 _fmt_num(last_value(s['id'], 'remaining')),
                 _fmt_num(last_value(s['id'], 'spent')),
                 _fmt_num(last_value(s['id'], 'estimate'))] for s in services]
        rows.append(['Итого', '', '', _fmt_num(total_remaining), _fmt_num(total_spent), _fmt_num(total_estimate)])
        _pdf_table(pdf, ['Этап', 'Эпик', 'Целевая дата', 'Остаток, ч', 'Затрачено, ч', 'Оценка, ч'], rows,
                   widths=[1.2, 3.5, 1.2, 1, 1, 1], total_row=True)

        all_dates = sorted({e['date'] for e in entries
                            if e['remaining'] is not None and e['service_id'] in {s['id'] for s in services}})
        dates = all_dates[-5:]
        if dates:
            _pdf_heading(pdf, 'Burn rate по эпикам, ч/день (последние 5 дат)')
            br_rows, all_rates = [], []
            for s in services:
                rates = _daily_rates(by_service.get(s['id'], []), dates)
                all_rates.append(rates)
                br_rows.append([f"{s['etap']} — {s['name']}"] + [_fmt_num(r) for r in rates])
            totals = []
            for i in range(len(dates)):
                vals = [r[i] for r in all_rates if r[i] is not None]
                totals.append(sum(vals) if vals else None)
            br_rows.append(['Итого по проекту'] + [_fmt_num(t) for t in totals])
            _pdf_table(pdf, ['Эпик'] + [_fmt_date(d)[:5] for d in dates], br_rows,
                       widths=[4] + [1] * len(dates), total_row=True)

        history, latest = jira_composition(db, pid)
        settings = db.execute('SELECT * FROM settings WHERE project_id = ?', (pid,)).fetchone()
        with_comp = [s for s in services if s['id'] in latest]
        comp_rows = [_composition_pdf_row(s['name'], history[s['id']], latest[s['id']], settings, s['target_date'])
                     for s in with_comp]
        if len(with_comp) > 1:
            agg_history, agg_latest = aggregate_composition(history, latest, [s['id'] for s in with_comp])
            comp_rows.append(_composition_pdf_row('Итого по проекту', agg_history, agg_latest, settings))
        if comp_rows:
            _pdf_heading(pdf, 'Задачи Jira по эпикам (последний срез)')
            _pdf_table(pdf, ['Эпик'] + COMPOSITION_PDF_HEAD[1:], comp_rows, widths=COMPOSITION_PDF_WIDTHS,
                       total_row=len(with_comp) > 1)

    elif page_type == 'stages':
        stage_groups = {}
        for s in services:
            stage_groups.setdefault(stage_key(s), []).append(s)
        _pdf_heading(pdf, 'Сводка по этапам')
        st_rows = [[name, str(len(svcs)), _fmt_num(sum_last(svcs, 'remaining')), _fmt_date(stage_targets.get(name))]
                   for name, svcs in stage_groups.items()]
        _pdf_table(pdf, ['Этап', 'Эпиков', 'Остаток (сумма), ч', 'Цель'], st_rows, widths=[3, 1, 1.5, 1.5])

        for stage_name, svcs in stage_groups.items():
            _pdf_heading(pdf, f'{stage_name} — детали по эпикам')
            d_rows = [[s['name'], _fmt_date(s['target_date']),
                       _fmt_num(last_value(s['id'], 'remaining')),
                       _fmt_num(last_value(s['id'], 'spent')),
                       _fmt_num(last_value(s['id'], 'estimate'))] for s in svcs]
            _pdf_table(pdf, ['Эпик', 'Целевая дата', 'Остаток, ч', 'Затрачено, ч', 'Оценка, ч'], d_rows,
                       widths=[4, 1.2, 1, 1, 1])

        history, latest = jira_composition(db, pid)
        settings = db.execute('SELECT * FROM settings WHERE project_id = ?', (pid,)).fetchone()
        comp_rows, all_ids = [], []
        for stage_name, svcs in stage_groups.items():
            ids = [s['id'] for s in svcs if s['id'] in latest]
            agg = aggregate_composition(history, latest, ids)
            if agg:
                all_ids += ids
                comp_rows.append(_composition_pdf_row(stage_name, agg[0], agg[1], settings, stage_targets.get(stage_name)))
        if comp_rows:
            agg_history, agg_latest = aggregate_composition(history, latest, all_ids)
            comp_rows.append(_composition_pdf_row('Итого по проекту', agg_history, agg_latest, settings))
            _pdf_heading(pdf, 'Задачи Jira по этапам (последний срез)')
            _pdf_table(pdf, ['Этап'] + COMPOSITION_PDF_HEAD[1:], comp_rows, widths=COMPOSITION_PDF_WIDTHS, total_row=True)

    elif page_type == 'data':
        for s in services:
            svc_e = by_service.get(s['id'], [])
            if not svc_e:
                continue
            _pdf_heading(pdf, f"{s['etap']} — {s['name']}")
            d_rows = [[_fmt_date(e['date']), _fmt_num(e['remaining']), _fmt_num(e['spent']), _fmt_num(e['estimate'])]
                      for e in svc_e]
            _pdf_table(pdf, ['Дата', 'Остаток, ч', 'Затрачено, ч', 'Оценка, ч'], d_rows)

    filename = f"Burndown_{PDF_PAGES[page_type][1]}_{datetime.now().strftime('%Y-%m-%d_%H%M')}.pdf"
    return bytes(pdf.output()), filename


PDF_PAGES = {
    'dashboard': ('Дашборд сгорания', 'дашборд'),
    'stages': ('Свод по этапам', 'свод_по_этапам'),
    'data': ('Данные по датам', 'данные_по_датам'),
}
PDF_PRIMARY = (1, 105, 111)    # #01696f
PDF_TEXT = (28, 33, 38)
PDF_MUTED = (92, 100, 112)
PDF_DIVIDER = (223, 226, 229)


def _fmt_num(v):
    if v is None:
        return '—'
    try:
        return f'{float(v):.1f}'
    except (ValueError, TypeError):
        return str(v)


def _fmt_date(d):
    """YYYY-MM-DD → DD.MM.YYYY."""
    if not d:
        return '—'
    parts = str(d).split('-')
    return '.'.join(reversed(parts)) if len(parts) == 3 else str(d)


def _daily_rates(svc_entries, dates):
    """Дневной burn rate эпика на каждую дату: (предыдущий заполненный остаток − текущий) / дней."""
    filled = [e for e in svc_entries if e['remaining'] is not None]
    rates = []
    for d in dates:
        idx = next((i for i, e in enumerate(filled) if e['date'] == d), -1)
        if idx <= 0:
            rates.append(None)
            continue
        prev = filled[idx - 1]
        days = (datetime.strptime(d, '%Y-%m-%d') - datetime.strptime(prev['date'], '%Y-%m-%d')).days
        rates.append((prev['remaining'] - filled[idx]['remaining']) / days if days > 0 else None)
    return rates


def _pdf_heading(pdf, text):
    pdf.ln(2)
    pdf.set_font('DejaVu', 'B', 11)
    pdf.set_text_color(*PDF_PRIMARY)
    pdf.cell(0, 6, text, new_x='LMARGIN', new_y='NEXT')
    pdf.ln(1)


def _pdf_fit(pdf, text, width):
    """Обрезает текст с многоточием под ширину ячейки."""
    text = str(text)
    if pdf.get_string_width(text) <= width:
        return text
    while text and pdf.get_string_width(text + '…') > width:
        text = text[:-1]
    return text + '…'


def _pdf_table(pdf, headers, rows, widths=None, total_row=False):
    """Таблица на всю ширину страницы; widths — относительные доли колонок, числовые колонки выравниваются вправо."""
    page_w = pdf.w - pdf.l_margin - pdf.r_margin
    widths = widths or [1] * len(headers)
    col_w = [page_w * w / sum(widths) for w in widths]
    row_h = 6

    def header():
        pdf.set_font('DejaVu', 'B', 8)
        pdf.set_fill_color(*PDF_PRIMARY)
        pdf.set_text_color(255, 255, 255)
        pdf.set_draw_color(*PDF_DIVIDER)
        for w, h in zip(col_w, headers):
            pdf.cell(w, row_h, _pdf_fit(pdf, h, w - 2), border=1, fill=True)
        pdf.ln(row_h)

    header()
    for ri, row in enumerate(rows):
        if pdf.will_page_break(row_h):
            pdf.add_page()
            header()
        is_total = total_row and ri == len(rows) - 1
        pdf.set_font('DejaVu', 'B' if is_total else '', 8)
        pdf.set_text_color(*PDF_TEXT)
        pdf.set_fill_color(*((238, 240, 242) if is_total else (255, 255, 255)))
        for ci, (w, cell) in enumerate(zip(col_w, row)):
            text = str(cell)
            numeric = ci > 0 and (text == '—' or re.fullmatch(r'-?\d+(\.\d+)?', text) is not None)
            pdf.cell(w, row_h, _pdf_fit(pdf, text, w - 2), border=1, fill=True, align='R' if numeric else 'L')
        pdf.ln(row_h)


start_scheduler()


if __name__ == '__main__':
    if not os.path.exists(DB_PATH):
        from init_db import init_db
        init_db()
    app.run(host='0.0.0.0', port=5000, debug=True)
