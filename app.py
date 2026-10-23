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
from functools import wraps
from datetime import datetime
from urllib.parse import quote

from flask import (
    Flask, request, jsonify, session, render_template,
    redirect, g, Response
)
from werkzeug.security import generate_password_hash, check_password_hash

from init_db import create_indexes

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


def stage_key(s):
    """Ключ группировки для «Свода по этапам»: явная группа эпика или этап без подсетапа."""
    try:
        sg = (s['stage_group'] or '').strip()
    except (KeyError, IndexError):
        sg = ''
    if sg:
        return sg
    etap = s['etap'] or ''
    m = re.match(r'^(Этап\s+\d+)(\.\d+)?$', etap, re.IGNORECASE)
    return m.group(1) if m else etap


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


HOUR_FIELDS = {'remaining': 'Остаток', 'spent': 'Затрачено', 'estimate': 'Оценка'}


def parse_entry_values(data):
    return {f: parse_hours(data.get(f), label) for f, label in HOUR_FIELDS.items()}


def row_to_dict(row):
    return dict(row) if row else None


def rows_to_dict(rows):
    return [dict(r) for r in rows]


# ─── Auth helpers ────────────────────────────────────────────────

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return jsonify({'error': 'Не авторизован'}), 401
        return f(*args, **kwargs)
    return decorated


def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return jsonify({'error': 'Не авторизован'}), 401
        db = get_db()
        user = db.execute('SELECT * FROM users WHERE id = ?', (session['user_id'],)).fetchone()
        if not user or user['role'] != 'admin':
            return jsonify({'error': 'Доступ запрещён. Требуется роль администратора.'}), 403
        return f(*args, **kwargs)
    return decorated


def get_current_user():
    if 'user_id' not in session:
        return None
    db = get_db()
    return db.execute('SELECT * FROM users WHERE id = ?', (session['user_id'],)).fetchone()


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


def audit_log(page, action, entity_type=None, entity_id=None, project_id=None, changes=None, entity_name=None, commit=True):
    """Log a change to audit_log table. changes is a list of {field, old, new} dicts."""
    db = get_db()
    uid = session.get('user_id')
    username = 'system'
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
    if 'user_id' not in session:
        return redirect('/login')
    return render_template('index.html')


@app.route('/login')
def login_page():
    return render_template('login.html')


# ─── API: Auth ───────────────────────────────────────────────────

@app.route('/api/auth/login', methods=['POST'])
def api_login():
    data = request.get_json() or {}
    username = data.get('username', '').strip()
    password = data.get('password', '')
    if not username or not password:
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
    name = data.get('name', '').strip()
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
    name = data.get('name', project['name']).strip()
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


@app.route('/api/projects/<int:pid>/data')
@login_required
def api_get_project_data(pid):
    if not can_view_project(session['user_id'], pid):
        return jsonify({'error': 'Нет доступа к проекту'}), 403

    db = get_db()
    services = db.execute('SELECT * FROM services WHERE project_id = ? ORDER BY sort_order, id', (pid,)).fetchall()
    service_ids = [s['id'] for s in services]
    entries = []
    if service_ids:
        placeholders = ','.join('?' * len(service_ids))
        entries = db.execute(
            f'SELECT * FROM entries WHERE service_id IN ({placeholders}) ORDER BY date', service_ids
        ).fetchall()
    stage_targets = db.execute('SELECT * FROM stage_targets WHERE project_id = ?', (pid,)).fetchall()
    settings = db.execute('SELECT * FROM settings WHERE project_id = ?', (pid,)).fetchone()

    # Convert snake_case to camelCase for frontend compatibility
    svc_list = []
    for s in services:
        svc_list.append({
            'id': s['id'], 'project_id': s['project_id'],
            'etap': s['etap'], 'name': s['name'],
            'targetDate': s['target_date'], 'sortOrder': s['sort_order'] if s['sort_order'] is not None else 0,
            'isDone': bool(s['is_done']), 'stageGroup': s['stage_group'] or ''
        })
    ent_list = []
    for e in entries:
        ent_list.append({
            'id': e['id'], 'service_id': e['service_id'],
            'date': e['date'], 'remaining': e['remaining'],
            'spent': e['spent'], 'estimate': e['estimate']
        })
    settings_dict = {}
    if settings:
        settings_dict = {
            'forecastMethod': settings['forecast_method'],
            'riskBufferDays': settings['risk_buffer_days'],
            'burnrateMethod': settings['burnrate_method'],
            'burnrateWindowDays': settings['burnrate_window_days'],
        }
    return jsonify({
        'services': svc_list,
        'entries': ent_list,
        'stageTargets': {r['stage_name']: r['target_date'] for r in stage_targets},
        'settings': settings_dict,
    })


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

    result = []
    for sid, vals in parsed:
        existing = db.execute('SELECT * FROM entries WHERE service_id = ? AND date = ?', (sid, date)).fetchone()
        if existing:
            db.execute('UPDATE entries SET remaining = ?, spent = ?, estimate = ? WHERE id = ?',
                       (vals['remaining'], vals['spent'], vals['estimate'], existing['id']))
            eid, action = existing['id'], 'update'
        else:
            eid = db.execute('INSERT INTO entries (service_id, date, remaining, spent, estimate) VALUES (?, ?, ?, ?, ?)',
                             (sid, date, vals['remaining'], vals['spent'], vals['estimate'])).lastrowid
            action = 'create'
        result.append(dict(id=eid, service_id=sid, date=date, **vals))
        changes = [{'field': f, 'old': None if not existing or existing[f] is None else str(existing[f]),
                    'new': None if vals[f] is None else str(vals[f])}
                   for f in HOUR_FIELDS if not existing or existing[f] != vals[f]]
        if changes:
            audit_log('data', action, 'entry', eid, pid, changes, entity_name=services[sid]['name'], commit=False)
    db.commit()
    return jsonify({'entries': result})


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
    audit_log('stages', 'update', 'stage_target', None, pid, [{'field': stage_name, 'old': existing['target_date'] if existing else None, 'new': target_date}], entity_name=stage_name)
    return jsonify({'success': True})


# ─── API: Settings ──────────────────────────────────────────────

SETTINGS_FIELDS = [
    ('forecastMethod', 'forecast_method'),
    ('riskBufferDays', 'risk_buffer_days'),
    ('burnrateMethod', 'burnrate_method'),
    ('burnrateWindowDays', 'burnrate_window_days'),
]

@app.route('/api/projects/<int:pid>/settings', methods=['PUT'])
@login_required
def api_update_settings(pid):
    if not can_edit_project(session['user_id'], pid):
        return jsonify({'error': 'Нет прав на редактирование'}), 403
    data = request.get_json() or {}
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
    username = data.get('username', '').strip()
    password = data.get('password', '')
    if not username or not password:
        return jsonify({'error': 'Укажите логин и пароль'}), 400

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

    new_username = data.get('username', user['username']).strip()
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
    photo = data.get('photo', user['photo'])
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
        if str(old_val) != str(new_val):
            changes.append({'field': field, 'old': str(old_val), 'new': str(new_val)})
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
    photo = data.get('photo', user['photo'])
    password = data.get('password')
    # Admin can edit own display_name, email, position via profile too
    display_name = data.get('displayName', user['display_name'])
    email = data.get('email', user['email'])
    position = data.get('position', user['position'])
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
        if str(old_val) != str(new_val):
            changes.append({'field': field, 'old': str(old_val), 'new': str(new_val)})
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
    services = db.execute('SELECT * FROM services WHERE project_id = ? ORDER BY sort_order, id', (pid,)).fetchall()
    service_ids = [s['id'] for s in services]
    entries = []
    if service_ids:
        placeholders = ','.join('?' * len(service_ids))
        entries = db.execute(
            f'SELECT * FROM entries WHERE service_id IN ({placeholders}) ORDER BY date', service_ids
        ).fetchall()
    stage_targets = db.execute('SELECT * FROM stage_targets WHERE project_id = ?', (pid,)).fetchall()
    settings = db.execute('SELECT * FROM settings WHERE project_id = ?', (pid,)).fetchone()
    project = db.execute('SELECT * FROM projects WHERE id = ?', (pid,)).fetchone()

    # Convert to camelCase for frontend
    svc_list = []
    for s in services:
        svc_list.append({
            'id': s['id'], 'project_id': s['project_id'],
            'etap': s['etap'], 'name': s['name'],
            'targetDate': s['target_date'], 'sortOrder': s['sort_order'] if s['sort_order'] is not None else 0,
            'isDone': bool(s['is_done']), 'stageGroup': s['stage_group'] or ''
        })
    ent_list = []
    for e in entries:
        ent_list.append({
            'id': e['id'], 'service_id': e['service_id'],
            'date': e['date'], 'remaining': e['remaining'],
            'spent': e['spent'], 'estimate': e['estimate']
        })
    settings_dict = {}
    if settings:
        settings_dict = {
            'forecastMethod': settings['forecast_method'],
            'riskBufferDays': settings['risk_buffer_days'],
            'burnrateMethod': settings['burnrate_method'],
            'burnrateWindowDays': settings['burnrate_window_days'],
        }
    payload = json.dumps({
        'project': {'name': project['name'], 'description': project['description']},
        'services': svc_list,
        'entries': ent_list,
        'stageTargets': {r['stage_name']: r['target_date'] for r in stage_targets},
        'settings': settings_dict,
    }, ensure_ascii=False, indent=2)
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
    services = db.execute('SELECT * FROM services WHERE project_id = ? ORDER BY sort_order, id', (pid,)).fetchall()
    service_ids = [s['id'] for s in services]
    entries = []
    if service_ids:
        placeholders = ','.join('?' * len(service_ids))
        entries = db.execute(f'SELECT * FROM entries WHERE service_id IN ({placeholders}) ORDER BY date', service_ids).fetchall()
    stage_targets = {r['stage_name']: r['target_date'] for r in
                     db.execute('SELECT * FROM stage_targets WHERE project_id = ?', (pid,)).fetchall()}

    # Фильтры интерфейса: выбранные эпики дашборда, учёт сданных эпиков в своде по этапам
    epics_arg = request.args.get('epics')
    if page_type == 'dashboard' and epics_arg is not None:
        selected = {int(x) for x in epics_arg.split(',') if x.strip().isdigit()}
        services = [s for s in services if s['id'] in selected]
    elif page_type == 'stages' and request.args.get('showDone') != '1':
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

    elif page_type == 'data':
        for s in services:
            svc_e = by_service.get(s['id'], [])
            if not svc_e:
                continue
            _pdf_heading(pdf, f"{s['etap']} — {s['name']}")
            d_rows = [[_fmt_date(e['date']), _fmt_num(e['remaining']), _fmt_num(e['spent']), _fmt_num(e['estimate'])]
                      for e in svc_e]
            _pdf_table(pdf, ['Дата', 'Остаток, ч', 'Затрачено, ч', 'Оценка, ч'], d_rows)

    pdf_bytes = pdf.output()
    filename = f"Burndown_{PDF_PAGES[page_type][1]}_{datetime.now().strftime('%Y-%m-%d_%H%M')}.pdf"
    filename_ascii = filename.encode('ascii', 'replace').decode('ascii')
    return Response(bytes(pdf_bytes), mimetype='application/pdf',
                    headers={'Content-Disposition': f"attachment; filename=\"{filename_ascii}\"; filename*=UTF-8''{quote(filename)}"})


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


if __name__ == '__main__':
    if not os.path.exists(DB_PATH):
        from init_db import init_db
        init_db()
    app.run(host='0.0.0.0', port=5000, debug=True)
