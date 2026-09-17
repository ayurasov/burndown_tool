#!/usr/bin/env python3
"""Burndown Tracker — Flask backend с SQLite.

Запуск для разработки:
    python app.py

Запуск в продакшене (через gunicorn):
    gunicorn -w 4 -b 0.0.0.0:5000 app:app
"""

import os
import sqlite3
import json
from functools import wraps
from datetime import datetime

from flask import (
    Flask, request, jsonify, session, render_template,
    redirect, g, Response
)
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__, static_folder='static', template_folder='templates')
app.secret_key = os.environ.get('SECRET_KEY', 'burndown-secret-key-change-in-production')
DB_PATH = os.environ.get('DB_PATH', os.path.join(os.path.dirname(os.path.abspath(__file__)), 'burndown.db'))


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


def audit_log(page, action, entity_type=None, entity_id=None, project_id=None, changes=None, entity_name=None):
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
            'targetDate': s['target_date'], 'sortOrder': s['sort_order'] if s['sort_order'] is not None else 0
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
    db = get_db()
    max_order = db.execute('SELECT COALESCE(MAX(sort_order), 0) FROM services WHERE project_id = ?', (pid,)).fetchone()[0]
    cur = db.execute(
        'INSERT INTO services (project_id, etap, name, target_date, sort_order) VALUES (?, ?, ?, ?, ?)',
        (pid, data.get('etap', ''), data.get('name', ''), data.get('targetDate', ''), max_order + 1)
    )
    db.commit()
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
    changes = []
    if data.get('etap', svc['etap']) != svc['etap']:
        changes.append({'field': 'etap', 'old': svc['etap'], 'new': data.get('etap')})
    if data.get('name', svc['name']) != svc['name']:
        changes.append({'field': 'name', 'old': svc['name'], 'new': data.get('name')})
    if data.get('targetDate', svc['target_date']) != svc['target_date']:
        changes.append({'field': 'targetDate', 'old': svc['target_date'], 'new': data.get('targetDate')})
    db.execute(
        'UPDATE services SET etap = ?, name = ?, target_date = ? WHERE id = ?',
        (data.get('etap', svc['etap']), data.get('name', svc['name']),
         data.get('targetDate', svc['target_date']), sid)
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
    cur = db.execute(
        'INSERT INTO entries (service_id, date, remaining, spent, estimate) VALUES (?, ?, ?, ?, ?)',
        (sid, data.get('date'), data.get('remaining'), data.get('spent'), data.get('estimate'))
    )
    db.commit()
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
    changes = []
    for field, old_val, new_val in [
        ('date', entry['date'], data.get('date', entry['date'])),
        ('remaining', entry['remaining'], data.get('remaining')),
        ('spent', entry['spent'], data.get('spent')),
        ('estimate', entry['estimate'], data.get('estimate')),
    ]:
        if str(old_val) != str(new_val):
            changes.append({'field': field, 'old': str(old_val), 'new': str(new_val)})
    db.execute(
        'UPDATE entries SET date = ?, remaining = ?, spent = ?, estimate = ? WHERE id = ?',
        (data.get('date', entry['date']),
         data.get('remaining'), data.get('spent'), data.get('estimate'), eid)
    )
    db.commit()
    if changes:
        audit_log('data', 'update', 'entry', eid, svc['project_id'], changes, entity_name=svc['name'])
    return jsonify({'success': True})


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
    return jsonify({'success': True})


# ─── API: Stage targets ─────────────────────────────────────────

@app.route('/api/projects/<int:pid>/stage_targets', methods=['PUT'])
@login_required
def api_update_stage_target(pid):
    if not can_edit_project(session['user_id'], pid):
        return jsonify({'error': 'Нет прав на редактирование'}), 403
    data = request.get_json() or {}
    stage_name = data.get('stageName')
    target_date = data.get('targetDate')
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

@app.route('/api/projects/<int:pid>/settings', methods=['PUT'])
@login_required
def api_update_settings(pid):
    if not can_edit_project(session['user_id'], pid):
        return jsonify({'error': 'Нет прав на редактирование'}), 403
    data = request.get_json() or {}
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
    audit_log('settings', 'update', 'settings', None, pid, [{'field': 'settings', 'old': 'previous', 'new': 'updated'}])
    return jsonify({'success': True})


# ─── API: Users (admin only) ─────────────────────────────────────

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
            'targetDate': s['target_date'], 'sortOrder': s['sort_order'] if s['sort_order'] is not None else 0
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
        'project': {'name': project['name'], 'description': project['description']},
        'services': svc_list,
        'entries': ent_list,
        'stageTargets': {r['stage_name']: r['target_date'] for r in stage_targets},
        'settings': settings_dict,
    })


# ─── API: Audit log (admin only) ─────────────────────────────────

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
    if project_id:
        conditions.append('project_id = ?')
        params.append(int(project_id))
    where_clause = ' WHERE ' + ' AND '.join(conditions) if conditions else ''
    rows = db.execute(
        f'SELECT * FROM audit_log{where_clause} ORDER BY id DESC LIMIT 500',
        params
    ).fetchall()
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
    stage_targets = db.execute('SELECT * FROM stage_targets WHERE project_id = ?', (pid,)).fetchall()
    settings = db.execute('SELECT * FROM settings WHERE project_id = ?', (pid,)).fetchone()

    page_titles = {
        'dashboard': 'Дашборд сгорания',
        'stages': 'Свод по этапам',
        'data': 'Данные по датам',
    }
    page_title = page_titles.get(page_type, page_type)

    # Build HTML for PDF
    html_parts = ['<!DOCTYPE html><html lang="ru"><head><meta charset="UTF-8">']
    html_parts.append('<style>')
    html_parts.append('body{font-family:"Helvetica Neue",Arial,sans-serif;font-size:12px;color:#1c2126;margin:20px;}')
    html_parts.append('h1{font-size:20px;color:#01696f;margin-bottom:4px;}')
    html_parts.append('h2{font-size:14px;color:#01696f;margin-top:20px;margin-bottom:8px;}')
    html_parts.append('.sub{color:#5c6470;font-size:11px;margin-bottom:16px;}')
    html_parts.append('.meta{color:#5c6470;font-size:10px;margin-bottom:20px;border-bottom:1px solid #dfe2e5;padding-bottom:8px;}')
    html_parts.append('table{width:100%;border-collapse:collapse;margin-bottom:16px;}')
    html_parts.append('th{background:#01696f;color:#fff;padding:6px 8px;text-align:left;font-size:11px;}')
    html_parts.append('td{padding:5px 8px;border-bottom:1px solid #dfe2e5;font-size:11px;}')
    html_parts.append('.mono{font-family:"Courier New",monospace;}')
    html_parts.append('.kpi-grid{display:flex;gap:12px;margin-bottom:16px;}')
    html_parts.append('.kpi-card{flex:1;border:1px solid #dfe2e5;border-radius:6px;padding:8px 12px;}')
    html_parts.append('.kpi-label{font-size:10px;color:#5c6470;}')
    html_parts.append('.kpi-value{font-size:18px;font-weight:600;}')
    html_parts.append('.kpi-note{font-size:10px;color:#5c6470;}')
    html_parts.append('.total-row td{font-weight:bold;background:#eef0f2;}')
    html_parts.append('</style></head><body>')

    now_str = datetime.now().strftime('%d.%m.%Y %H:%M')
    html_parts.append(f'<h1>{page_title}</h1>')
    html_parts.append(f'<div class="sub">Проект: {project["name"]} — {project["description"] or ""}</div>')
    html_parts.append(f'<div class="meta">Сформировано: {now_str}</div>')

    # Build stage groups
    stage_groups = {}
    for s in services:
        stage_groups.setdefault(s['etap'], []).append(s)

    if page_type == 'dashboard':
        # KPIs
        html_parts.append('<h2>Ключевые показатели</h2>')
        html_parts.append('<div class="kpi-grid">')
        total_remaining = sum(e['remaining'] or 0 for e in entries if e['remaining'] is not None)
        total_spent = sum(e['spent'] or 0 for e in entries if e['spent'] is not None)
        total_estimate = sum(e['estimate'] or 0 for e in entries if e['estimate'] is not None)
        html_parts.append(f'<div class="kpi-card"><div class="kpi-label">Остаток (всего)</div><div class="kpi-value">{total_remaining:.1f}</div></div>')
        html_parts.append(f'<div class="kpi-card"><div class="kpi-label">Освоено (всего)</div><div class="kpi-value">{total_spent:.1f}</div></div>')
        html_parts.append(f'<div class="kpi-card"><div class="kpi-label">Оценка (всего)</div><div class="kpi-value">{total_estimate:.1f}</div></div>')
        html_parts.append(f'<div class="kpi-card"><div class="kpi-label">Эпиков</div><div class="kpi-value">{len(services)}</div></div>')
        html_parts.append('</div>')

        # Summary table
        html_parts.append('<h2>Сводная таблица по эпикам</h2>')
        html_parts.append('<table><thead><tr><th>Этап</th><th>Эпик</th><th>Целевая дата</th><th>Остаток</th><th>Освоено</th><th>Оценка</th></tr></thead><tbody>')
        for s in services:
            svc_entries = [e for e in entries if e['service_id'] == s['id']]
            last = svc_entries[-1] if svc_entries else None
            html_parts.append(f'<tr><td>{s["etap"]}</td><td>{s["name"]}</td><td class="mono">{s["target_date"] or "—"}</td><td class="mono">{last["remaining"] if last and last["remaining"] is not None else "—"}</td><td class="mono">{last["spent"] if last and last["spent"] is not None else "—"}</td><td class="mono">{last["estimate"] if last and last["estimate"] is not None else "—"}</td></tr>')
        html_parts.append(f'<tr class="total-row"><td colspan="3">Итого</td><td class="mono">{total_remaining:.1f}</td><td class="mono">{total_spent:.1f}</td><td class="mono">{total_estimate:.1f}</td></tr>')
        html_parts.append('</tbody></table>')

        # Burn rate table
        all_dates = sorted(set(e['date'] for e in entries if e['remaining'] is not None))
        dates = all_dates[-5:] if len(all_dates) > 5 else all_dates
        if dates:
            html_parts.append('<h2>Burn rate по эпикам (последние 5 дат)</h2>')
            html_parts.append('<table><thead><tr><th>Эпик</th>')
            for d in dates:
                html_parts.append(f'<th class="mono">{d[5:]}</th>')
            html_parts.append('</tr></thead><tbody>')
            for s in services:
                html_parts.append(f'<tr><td>{s["etap"]} — {s["name"]}</td>')
                svc_entries = {e['date']: e['remaining'] for e in entries if e['service_id'] == s['id']}
                for d in dates:
                    val = svc_entries.get(d)
                    html_parts.append(f'<td class="mono">{val if val is not None else "—"}</td>')
                html_parts.append('</tr>')
            html_parts.append('<tr class="total-row"><td>Итого</td>')
            for d in dates:
                total = sum(e['remaining'] for e in entries if e['date'] == d and e['remaining'] is not None)
                html_parts.append(f'<td class="mono">{total:.1f}</td>')
            html_parts.append('</tr></tbody></table>')

    elif page_type == 'stages':
        html_parts.append('<h2>Сводка по этапам</h2>')
        html_parts.append('<table><thead><tr><th>Этап</th><th>Эпиков</th><th>Остаток (сумма)</th><th>Цель</th></tr></thead><tbody>')
        for stage_name, svcs in stage_groups.items():
            svc_ids = [s['id'] for s in svcs]
            svc_entries = [e for e in entries if e['service_id'] in svc_ids]
            last_remaining = sum(e['remaining'] or 0 for e in svc_entries if e['remaining'] is not None)
            target = next((st['target_date'] for st in stage_targets if st['stage_name'] == stage_name), '—')
            html_parts.append(f'<tr><td>{stage_name}</td><td class="mono">{len(svcs)}</td><td class="mono">{last_remaining:.1f}</td><td class="mono">{target}</td></tr>')
        html_parts.append('</tbody></table>')

        # Per-stage details
        for stage_name, svcs in stage_groups.items():
            html_parts.append(f'<h2>{stage_name} — детали по эпикам</h2>')
            html_parts.append('<table><thead><tr><th>Эпик</th><th>Целевая дата</th><th>Остаток</th><th>Освоено</th><th>Оценка</th></tr></thead><tbody>')
            for s in svcs:
                svc_entries = [e for e in entries if e['service_id'] == s['id']]
                last = svc_entries[-1] if svc_entries else None
                html_parts.append(f'<tr><td>{s["name"]}</td><td class="mono">{s["target_date"] or "—"}</td><td class="mono">{last["remaining"] if last and last["remaining"] is not None else "—"}</td><td class="mono">{last["spent"] if last and last["spent"] is not None else "—"}</td><td class="mono">{last["estimate"] if last and last["estimate"] is not None else "—"}</td></tr>')
            html_parts.append('</tbody></table>')

    elif page_type == 'data':
        html_parts.append('<h2>Замеры по датам</h2>')
        for s in services:
            svc_entries = [e for e in entries if e['service_id'] == s['id']]
            if not svc_entries:
                continue
            html_parts.append(f'<h2>{s["etap"]} — {s["name"]}</h2>')
            html_parts.append('<table><thead><tr><th>Дата</th><th>Остаток</th><th>Освоено</th><th>Оценка</th></tr></thead><tbody>')
            for e in svc_entries:
                html_parts.append(f'<tr><td class="mono">{e["date"]}</td><td class="mono">{e["remaining"] if e["remaining"] is not None else "—"}</td><td class="mono">{e["spent"] if e["spent"] is not None else "—"}</td><td class="mono">{e["estimate"] if e["estimate"] is not None else "—"}</td></tr>')
            html_parts.append('</tbody></table>')

    html_parts.append('</body></html>')
    html_content = ''.join(html_parts)

    # Generate PDF with fpdf2
    from fpdf import FPDF
    font_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static', 'fonts')
    pdf = FPDF(orientation='L', unit='mm', format='A4')
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_font('DejaVu', '', os.path.join(font_dir, 'dejavu.ttf'))
    pdf.add_font('DejaVu', 'B', os.path.join(font_dir, 'dejavu_bold.ttf'))
    pdf.add_page()
    pdf.set_font('DejaVu', 'B', 16)
    pdf.set_text_color(1, 105, 111)  # #01696f
    pdf.cell(0, 8, page_title, new_x='LMARGIN', new_y='NEXT')
    pdf.set_font('DejaVu', '', 10)
    pdf.set_text_color(92, 100, 112)
    pdf.cell(0, 5, f"Проект: {project['name']} — {project['description'] or ''}", new_x='LMARGIN', new_y='NEXT')
    pdf.cell(0, 5, f"Сформировано: {now_str}", new_x='LMARGIN', new_y='NEXT')
    pdf.ln(3)
    pdf.set_draw_color(223, 226, 229)
    pdf.line(10, pdf.get_y(), pdf.w-10, pdf.get_y())
    pdf.ln(3)

    pdf.set_text_color(28, 33, 38)

    # Render tables
    # Build stage groups
    stage_groups_pdf = {}
    for s in services:
        stage_groups_pdf.setdefault(s['etap'], []).append(s)

    if page_type == 'dashboard':
        # KPIs
        total_remaining = sum(e['remaining'] or 0 for e in entries if e['remaining'] is not None)
        total_spent = sum(e['spent'] or 0 for e in entries if e['spent'] is not None)
        total_estimate = sum(e['estimate'] or 0 for e in entries if e['estimate'] is not None)

        pdf.set_font('DejaVu', 'B', 11)
        pdf.set_text_color(1, 105, 111)
        pdf.cell(0, 6, 'Ключевые показатели', new_x='LMARGIN', new_y='NEXT')
        pdf.ln(1)
        pdf.set_font('DejaVu', '', 9)
        pdf.set_text_color(28, 33, 38)
        kpi_data = [
            ('Остаток (всего)', f'{total_remaining:.1f}'),
            ('Освоено (всего)', f'{total_spent:.1f}'),
            ('Оценка (всего)', f'{total_estimate:.1f}'),
            ('Эпиков', str(len(services))),
        ]
        col_w = (pdf.w - 20) / 4
        for label, val in kpi_data:
            pdf.set_font('DejaVu', '', 8)
            pdf.set_text_color(92, 100, 112)
            pdf.cell(col_w, 4, label)
        pdf.ln(4)
        for label, val in kpi_data:
            pdf.set_font('DejaVu', 'B', 14)
            pdf.set_text_color(28, 33, 38)
            pdf.cell(col_w, 6, val)
        pdf.ln(6)

        # Summary table
        pdf.ln(2)
        pdf.set_font('DejaVu', 'B', 11)
        pdf.set_text_color(1, 105, 111)
        pdf.cell(0, 6, 'Сводная таблица по эпикам', new_x='LMARGIN', new_y='NEXT')
        pdf.ln(1)

        headers = ['Этап', 'Эпик', 'Целевая дата', 'Остаток', 'Освоено', 'Оценка']
        rows = []
        for s in services:
            svc_e = [e for e in entries if e['service_id'] == s['id']]
            last = svc_e[-1] if svc_e else None
            rows.append([
                s['etap'], s['name'][:35], s['target_date'] or '—',
                _fmt_num(last['remaining']) if last and last['remaining'] is not None else '—',
                _fmt_num(last['spent']) if last and last['spent'] is not None else '—',
                _fmt_num(last['estimate']) if last and last['estimate'] is not None else '—',
            ])
        rows.append(['', 'Итого', '', _fmt_num(total_remaining), _fmt_num(total_spent), _fmt_num(total_estimate)])
        _pdf_table(pdf, headers, rows, total_row=True)

        # Burn rate table
        all_dates = sorted(set(e['date'] for e in entries if e['remaining'] is not None))
        dates = all_dates[-5:] if len(all_dates) > 5 else all_dates
        if dates:
            pdf.ln(2)
            pdf.set_font('DejaVu', 'B', 11)
            pdf.set_text_color(1, 105, 111)
            pdf.cell(0, 6, 'Burn rate по эпикам (последние 5 дат)', new_x='LMARGIN', new_y='NEXT')
            pdf.ln(1)
            br_headers = ['Эпик'] + [d[5:].replace('-', '.') for d in dates]
            br_rows = []
            for s in services:
                svc_entries = {e['date']: e['remaining'] for e in entries if e['service_id'] == s['id']}
                row = [f"{s['etap']} — {s['name'][:25]}"]
                for d in dates:
                    val = svc_entries.get(d)
                    row.append(_fmt_num(val) if val is not None else '—')
                br_rows.append(row)
            # Total row
            total_row = ['Итого']
            for d in dates:
                total = sum(e['remaining'] for e in entries if e['date'] == d and e['remaining'] is not None)
                total_row.append(_fmt_num(total))
            br_rows.append(total_row)
            _pdf_table(pdf, br_headers, br_rows, total_row=True)

    elif page_type == 'stages':
        pdf.set_font('DejaVu', 'B', 11)
        pdf.set_text_color(1, 105, 111)
        pdf.cell(0, 6, 'Сводка по этапам', new_x='LMARGIN', new_y='NEXT')
        pdf.ln(1)
        st_headers = ['Этап', 'Эпиков', 'Остаток (сумма)', 'Цель']
        st_rows = []
        for stage_name, svcs in stage_groups_pdf.items():
            svc_ids = [s['id'] for s in svcs]
            svc_e = [e for e in entries if e['service_id'] in svc_ids]
            last_remaining = sum(e['remaining'] or 0 for e in svc_e if e['remaining'] is not None)
            target = next((st['target_date'] for st in stage_targets if st['stage_name'] == stage_name), '—')
            st_rows.append([stage_name, str(len(svcs)), _fmt_num(last_remaining), target])
        _pdf_table(pdf, st_headers, st_rows)

        for stage_name, svcs in stage_groups_pdf.items():
            pdf.ln(2)
            pdf.set_font('DejaVu', 'B', 11)
            pdf.set_text_color(1, 105, 111)
            pdf.cell(0, 6, f'{stage_name} — детали по эпикам', new_x='LMARGIN', new_y='NEXT')
            pdf.ln(1)
            d_headers = ['Эпик', 'Целевая дата', 'Остаток', 'Освоено', 'Оценка']
            d_rows = []
            for s in svcs:
                svc_e = [e for e in entries if e['service_id'] == s['id']]
                last = svc_e[-1] if svc_e else None
                d_rows.append([
                    s['name'][:40], s['target_date'] or '—',
                    _fmt_num(last['remaining']) if last and last['remaining'] is not None else '—',
                    _fmt_num(last['spent']) if last and last['spent'] is not None else '—',
                    _fmt_num(last['estimate']) if last and last['estimate'] is not None else '—',
                ])
            _pdf_table(pdf, d_headers, d_rows)

    elif page_type == 'data':
        for s in services:
            svc_e = [e for e in entries if e['service_id'] == s['id']]
            if not svc_e:
                continue
            pdf.set_font('DejaVu', 'B', 11)
            pdf.set_text_color(1, 105, 111)
            pdf.cell(0, 6, f"{s['etap']} — {s['name']}", new_x='LMARGIN', new_y='NEXT')
            pdf.ln(1)
            d_headers = ['Дата', 'Остаток', 'Освоено', 'Оценка']
            d_rows = []
            for e in svc_e:
                d_rows.append([
                    e['date'],
                    _fmt_num(e['remaining']) if e['remaining'] is not None else '—',
                    _fmt_num(e['spent']) if e['spent'] is not None else '—',
                    _fmt_num(e['estimate']) if e['estimate'] is not None else '—',
                ])
            _pdf_table(pdf, d_headers, d_rows)

    pdf_bytes = pdf.output()
    page_names = {'dashboard': 'дашборд', 'stages': 'свод_по_этапам', 'data': 'данные_по_датам'}
    filename_page = page_names.get(page_type, page_type)
    filename = f"Burndown_{filename_page}_{datetime.now().strftime('%Y-%m-%d_%H%M')}.pdf"
    from urllib.parse import quote
    filename_ascii = filename.encode('ascii', 'replace').decode('ascii')
    filename_quoted = quote(filename)
    return Response(bytes(pdf_bytes), mimetype='application/pdf',
                    headers={'Content-Disposition': f"attachment; filename=\"{filename_ascii}\"; filename*=UTF-8''{filename_quoted}"})


def _fmt_num(v):
    if v is None:
        return '—'
    try:
        return f'{float(v):.1f}'
    except (ValueError, TypeError):
        return str(v)


def _pdf_table(pdf, headers, rows, total_row=False):
    col_count = len(headers)
    page_w = pdf.w - 20
    col_w = page_w / col_count
    # Header
    pdf.set_font('DejaVu', 'B', 8)
    pdf.set_fill_color(1, 105, 111)
    pdf.set_text_color(255, 255, 255)
    for i, h in enumerate(headers):
        pdf.cell(col_w, 5, str(h), border=1, fill=True)
    pdf.ln(5)
    # Rows
    pdf.set_text_color(28, 33, 38)
    for ri, row in enumerate(rows):
        is_total = total_row and ri == len(rows) - 1
        pdf.set_font('DejaVu', 'B' if is_total else '', 8)
        if is_total:
            pdf.set_fill_color(238, 240, 242)
        else:
            pdf.set_fill_color(255, 255, 255)
        for i, cell in enumerate(row):
            pdf.cell(col_w, 5, str(cell), border=1, fill=True)
        pdf.ln(5)


if __name__ == '__main__':
    if not os.path.exists(DB_PATH):
        from init_db import init_db
        init_db()
    app.run(host='0.0.0.0', port=5000, debug=True)
