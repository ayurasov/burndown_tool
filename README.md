# Burndown Tracker — трекер сгорания работ

Веб-приложение для отслеживания сгорания задач по эпикам и этапам проектов. Поддерживает мультипроектность, ролевую модель и авторизацию.

## Возможности

- Многопроектная структура (каждый проект — отдельные эпики, этапы, замеры)
- Графики сгорания с прогнозом, средней динамикой и целевыми линиями
- Расширенные тултипы: отклонения факта от средней динамики и прогноза
- Ролевая модель: администратор, руководитель проекта, пользователь
- Авторизация по логину/паролю
- Хранение данных в SQLite

## Технологии

- Python 3.10+ / Flask
- SQLite (встроена в Python)
- Chart.js (через CDN)
- Gunicorn (продакшен)

## Развёртывание на Ubuntu 24.04

### 1. Установка системных пакетов

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip nginx
```

### 2. Создание пользователя и директории

```bash
sudo useradd -m -s /bin/bash burndown
sudo mkdir -p /opt/burndown
sudo chown burndown:burndown /opt/burndown
```

### 3. Копирование файлов

Скопируйте все файлы приложения в `/opt/burndown`:

```bash
sudo -u burndown cp -r /path/to/burndown-app/* /opt/burndown/
```

### 4. Создание виртуального окружения и установка зависимостей

```bash
sudo -u burndown bash -c '
cd /opt/burndown
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
'
```

### 5. Инициализация базы данных

```bash
sudo -u burndown bash -c '
cd /opt/burndown
source venv/bin/activate
python init_db.py
'
```

База данных `burndown.db` будет создана в директории приложения.

Будет создан пользователь по умолчанию:
- Логин: `admin`
- Пароль: `Test198!`

Созданы проекты:
- **АИСУЗ** — с предзаполненными данными
- **ИСУП** — пустой проект

### 6. Настройка Gunicorn через systemd

Создайте файл `/etc/systemd/system/burndown.service`:

```ini
[Unit]
Description=Burndown Tracker (Gunicorn)
After=network.target

[Service]
User=burndown
Group=burndown
WorkingDirectory=/opt/burndown
Environment="PATH=/opt/burndown/venv/bin"
Environment="SECRET_KEY=ваш-секретный-ключ-замените-это"
Environment="DB_PATH=/opt/burndown/burndown.db"
ExecStart=/opt/burndown/venv/bin/gunicorn -w 4 -b 127.0.0.1:5000 app:app
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Запуск:

```bash
sudo systemctl daemon-reload
sudo systemctl enable burndown
sudo systemctl start burndown
sudo systemctl status burndown
```

### 7. Настройка Nginx (обратный прокси)

Создайте файл `/etc/nginx/sites-available/burndown`:

```nginx
server {
    listen 80;
    server_name ваш-домен-или-ip;

    location / {
        proxy_pass http://127.0.0.1:5000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    location /static/ {
        alias /opt/burndown/static/;
        expires 30d;
    }
}
```

Активация:

```bash
sudo ln -s /etc/nginx/sites-available/burndown /etc/nginx/sites-enabled/
sudo nginx -t
sudo systemctl restart nginx
```

### 8. Настройка HTTPS (рекомендуется)

```bash
sudo apt install -y certbot python3-certbot-nginx
sudo certbot --nginx -d ваш-домен-или-ip
```

## Управление

### Перезапуск приложения

```bash
sudo systemctl restart burndown
```

### Просмотр логов

```bash
sudo journalctl -u burndown -f
```

### Резервное копирование базы данных

```bash
sudo cp /opt/burndown/burndown.db /opt/burndown/backup/burndown-$(date +%Y%m%d).db
```

### Сброс базы данных

```bash
sudo -u burndown bash -c '
cd /opt/burndown
source venv/bin/activate
python init_db.py
'
sudo systemctl restart burndown
```

## Ролевая модель

| Роль | Возможности |
|------|-------------|
| Администратор | Полный доступ: создание/удаление проектов, управление пользователями, редактирование всех данных |
| Руководитель проекта | Редактирование данных в назначенных проектах (эпики, замеры, настройки, целевые даты) |
| Пользователь | Только просмотр назначенных проектов |

## Структура проекта

```
burndown-app/
├── app.py              # Flask-приложение (API + маршруты)
├── init_db.py          # Инициализация БД и начальных данных
├── requirements.txt    # Python-зависимости
├── burndown.db         # База данных SQLite (создаётся при инициализации)
├── templates/
│   ├── login.html      # Страница авторизации
│   └── index.html      # Основное приложение
└── static/
    ├── css/style.css   # Стили
    └── js/app.js       # Логика приложения
```

## Быстрый запуск (временный, без systemd/Nginx)

Для временного запуска на сервере без настройки systemd и Nginx — одной командой через gunicorn:

```bash
cd /opt/burndown
source venv/bin/activate
python init_db.py

gunicorn app:app --bind 0.0.0.0:39661
```

Приложение будет доступно по адресу `http://<IP-сервера>:39661`.

Опциональные параметры:

```bash
# С указанием ключа и путей к БД
gunicorn app:app --bind 0.0.0.0:39661 \
  --env SECRET_KEY=ваш-секретный-ключ \
  --env DB_PATH=/opt/burndown/burndown.db

# С 4 воркерами для боевой нагрузки
gunicorn app:app --bind 0.0.0.0:39661 -w 4

# С логированием в файл
gunicorn app:app --bind 0.0.0.0:39661 --access-logfile - --error-logfile -
```

Остановка — `Ctrl+C` в терминале. Для постоянной работы используйте systemd (раздел 6 выше) или `nohup`:

```bash
nohup gunicorn app:app --bind 0.0.0.0:39661 > /tmp/burndown.log 2>&1 &
```

## Локальный запуск (для разработки)

```bash
cd burndown-app
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python init_db.py
python app.py
```

Приложение будет доступно по адресу `http://localhost:5000`.
