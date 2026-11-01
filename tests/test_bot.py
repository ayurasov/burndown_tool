"""Команды Telegram-бота: привязка «/start», /status, /risks, /week, /projects, опрос long polling'ом."""

import pytest

import app as app_module
from fake_notify import FakeTelegram


@pytest.fixture
def telegram(flask_app, monkeypatch):
    server = FakeTelegram()
    monkeypatch.setitem(flask_app.config, 'TELEGRAM_API', server.url)
    yield server
    server.stop()


@pytest.fixture
def bot(admin, telegram):
    assert admin.put('/api/admin/notify-settings', json={'telegram': {'token': '123:ABC'}}).status_code == 200
    return telegram


def poll(flask_app):
    with flask_app.app_context():
        return app_module.telegram_poll_step(timeout=0)


def reply(flask_app, text, chat_id='777'):
    with flask_app.app_context():
        return app_module.telegram_reply(app_module.get_db(), chat_id, text)


def link(flask_app, client, bot, chat_id):
    code = client.post('/api/profile/telegram/link').get_json()['code']
    bot.user_starts(code, chat_id=chat_id)
    assert poll(flask_app) >= 1


def test_linking_by_start_command_through_poller(admin, flask_app, bot, make_user):
    user = make_user('ivan', project_id=1)
    code = user.post('/api/profile/telegram/link').get_json()['code']
    bot.user_starts('устаревший', chat_id=1)
    bot.user_starts(code, chat_id=777)
    assert poll(flask_app) == 2
    texts = {m['chat_id']: m['text'] for m in bot.state['messages']}
    assert 'устарела' in texts['1'] and 'Уведомления подключены' in texts['777'] and '/risks' in texts['777']
    assert bot.state['commands'][0] == {'command': 'status', 'description': 'Сводка: статус проекта и главное'}
    assert user.post('/api/profile/telegram/check').get_json() == {'linked': True}  # бот уже привязал
    assert user.get('/api/projects/1/notifications').get_json()['channels']['telegramLinked']
    assert poll(flask_app) == 0  # подтверждённые сообщения второй раз не приходят
    assert bot.state['polls'][-1]['offset'] == '3' and bot.state['polls'][-1]['timeout'] == '0'
    link_again = user.post('/api/profile/telegram/link').get_json()['code']
    assert user.post('/api/profile/telegram/check').get_json() == {'linked': False}  # опрос жив — ждём «Start»
    bot.user_starts(link_again, chat_id=888)
    poll(flask_app)
    assert user.post('/api/profile/telegram/check').get_json() == {'linked': True}


def test_chat_belongs_to_last_linked_user(admin, flask_app, bot, make_user):
    first, second = make_user('first', project_id=1), make_user('second', project_id=2)
    link(flask_app, first, bot, 900)
    link(flask_app, second, bot, 900)
    with flask_app.app_context():
        owners = app_module.get_db().execute("SELECT username FROM users WHERE telegram_chat_id = '900'").fetchall()
    assert [r[0] for r in owners] == ['second']


def test_status_risks_week_for_single_project(admin, flask_app, bot, make_user):
    user = make_user('ivan', project_id=1)
    link(flask_app, user, bot, 777)
    status = reply(flask_app, '/status')
    assert '<b>АИСУЗ</b>' in status and 'Эпики: успевают' in status and 'Открыть в Burndown' not in status
    admin.put('/api/admin/notify-settings', json={'publicUrl': 'https://burndown.example.ru/'})
    app_module._RESULT_CACHE.clear()
    assert 'href="https://burndown.example.ru/?project=1"' in reply(flask_app, '/статус')
    risks = reply(flask_app, '/риски')
    assert 'главные риски' in risks and ('⛔' in risks or '⚠️' in risks or 'Рисков по срокам не видно' in risks)
    week = reply(flask_app, '/week@burndown_test_bot')
    assert 'за неделю' in week


def test_project_choice_by_number_and_name(admin, flask_app, bot):
    link(flask_app, admin, bot, 777)
    listing = reply(flask_app, '/projects')
    assert listing.startswith('<b>Ваши проекты</b>') and '1. ' in listing and '2. ' in listing and 'ИСУП' in listing
    assert reply(flask_app, '/status') == listing  # несколько проектов: статус — по всем
    assert '<b>ИСУП</b>' in reply(flask_app, '/status 2') and '<b>АИСУЗ</b>' in reply(flask_app, '/risks аисуз')
    assert 'не найден' in reply(flask_app, '/status Нет такого')
    assert 'не найден' in reply(flask_app, '/status 9')
    admin.post('/api/projects', json={'name': 'АИСУЗ-2'})
    assert 'несколько проектов' in reply(flask_app, '/status АИСУЗ')
    many = reply(flask_app, '/week')
    assert many.count('за неделю') == 3  # по каждому проекту
    assert reply(flask_app, '/risks').count('главные риски') == 3


def test_help_unknown_and_unlinked(admin, flask_app, bot, make_user):
    assert 'не привязан' in reply(flask_app, '/status', chat_id='404')
    user = make_user('ivan')  # без проектов
    link(flask_app, user, bot, 777)
    assert 'Не знаю такую команду' in reply(flask_app, '/foo') and '/status' in reply(flask_app, '/foo')
    assert reply(flask_app, 'привет').startswith('<b>Burndown</b> — команды')
    assert reply(flask_app, '/start').startswith('<b>Burndown</b> — команды')
    assert reply(flask_app, '/help') == reply(flask_app, '/помощь')
    assert 'нет доступа ни к одному проекту' in reply(flask_app, '/status')
    assert reply(flask_app, '   ') is None


def test_poller_disabled_and_errors(admin, flask_app, bot, monkeypatch):
    r = admin.put('/api/admin/notify-settings', json={'telegram': {'commands': False}})
    assert r.get_json()['telegram']['commands'] is False
    assert poll(flask_app) is None and bot.state['polls'] == []
    audit = admin.get('/api/audit?page=integrations').get_json()['entries']
    assert any(a['fieldName'] == 'telegramCommands' for a in audit)
    admin.put('/api/admin/notify-settings', json={'telegram': {'commands': True}})
    link(flask_app, admin, bot, 'blocked')  # бот заблокирован пользователем — ошибка отправки не роняет опрос
    bot.user_says('/status', chat_id='blocked')
    assert poll(flask_app) == 1
    original = app_module.telegram_reply
    monkeypatch.setattr(app_module, 'telegram_reply', lambda *a: 1 / 0)
    bot.user_says('/status', chat_id='blocked')
    assert poll(flask_app) == 1  # сбой команды записан в журнал, сообщение подтверждено
    assert poll(flask_app) == 0
    monkeypatch.setattr(app_module, 'telegram_reply', original)
    bot.user_says('без чата', chat_id='')
    bot.state['updates'][-1]['message'].pop('chat')
    assert poll(flask_app) == 1


def test_poll_tick_and_menu_retry(admin, flask_app, bot, monkeypatch):
    with flask_app.app_context():
        db = app_module.get_db()
        assert not app_module.telegram_poller_alive(db)
    poll(flask_app)
    with flask_app.app_context():
        assert app_module.telegram_poller_alive(app_module.get_db())
    assert admin.get('/api/admin/notify-settings').get_json()['telegram']['pollTick']
    with flask_app.app_context():
        db = app_module.get_db()
        app_module._set_setting(db, 'telegram_poll_tick', '2020-01-01 00:00:00')
        db.commit()
        assert not app_module.telegram_poller_alive(db)
    bot.state['token'] = 'другой'  # токен отозван: меню не отправилось, опрос сообщает об ошибке
    with flask_app.app_context():
        db = app_module.get_db()
        app_module._set_setting(db, 'telegram_menu', None)
        db.commit()
    with pytest.raises(app_module.notify.NotifyError):
        poll(flask_app)


def test_poll_lock_single_process(flask_app, monkeypatch, tmp_path):
    monkeypatch.setattr(app_module, '_tg_lock', None)
    monkeypatch.setattr(app_module, 'DB_PATH', str(tmp_path / 'x.db'))
    assert app_module._telegram_poll_lock() is True
    holder = app_module._tg_lock
    assert app_module._telegram_poll_lock() is True  # повторно — тот же процесс
    monkeypatch.setattr(app_module, '_tg_lock', None)  # «другой процесс»: блокировка уже занята
    assert app_module._telegram_poll_lock() is False
    holder.close()
