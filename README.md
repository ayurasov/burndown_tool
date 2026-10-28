# Burndown Tracker — трекер сгорания работ

Веб-приложение для отслеживания сгорания задач по эпикам и этапам проектов. Поддерживает мультипроектность, ролевую модель и авторизацию.

## Возможности

- Многопроектная структура (каждый проект — отдельные эпики, этапы, замеры)
- Графики сгорания с прогнозом, средней динамикой и целевыми линиями
- Расширенные тултипы: отклонения факта от средней динамики и прогноза
- Свод по этапам (группировка эпиков по этапу или явной группе), признак «сдано» и быстрые фильтры
- Ввод замера на одну дату сразу по всем эпикам; рост оценки (Δ оценки) и подсветка роста остатка
- Выгрузка в PDF (таблицы) и печать разделов с графиками; экспорт проекта в JSON
- Реестр изменений с фильтрами
- Связь с Jira: кнопки «срез из Structure» заполняют замеры остатком, затраченным временем и оценкой задач (поля Jira настраиваются)
- Автозаполнение замеров из Jira по расписанию
- Состав эпиков по задачам Jira: статусы, burn-up, разбор изменений объёма и проверка качества данных
- Восстановление истории замеров по журналу изменений Jira
- Прогноз диапазоном: шанс успеть к цели и диапазон 70% (Монте-Карло)
- Ролевая модель: администратор, руководитель проекта, пользователь
- Авторизация по логину/паролю
- Хранение данных в SQLite

## Технологии

- Python 3.10+ / Flask
- SQLite (встроена в Python)
- Chart.js и шрифты — локальные копии в `static/vendor` (работает без доступа в интернет)
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

> **Секретный ключ сессий.** Если `SECRET_KEY` не задан, при первом запуске генерируется случайный ключ
> и сохраняется в файл `.secret_key` рядом с базой данных (права 600). Ключ из переменной окружения
> имеет приоритет. При работе через HTTPS добавьте `Environment="SESSION_COOKIE_SECURE=1"` —
> cookie сессии будет передаваться только по защищённому соединению.

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

## Связь с Jira (срез из Structure)

Раздел **«Связь с Jira»** (руководитель проекта и администратор) настраивается для каждого проекта.

1. **Подключение** — устроено как в jira_mass_logger:
   - Jira Server/DC — Personal Access Token (Bearer), REST API v2;
   - Jira Server — логин + пароль (Basic), REST API v2;
   - Jira Cloud — email + API-токен, REST API v3;
   - опция «Не проверять TLS-сертификат» для самоподписанных/корпоративных сертификатов.

   Токен хранится в БД зашифрованным (ключ — производный от `SECRET_KEY`; при смене ключа токен нужно ввести заново)
   и никогда не возвращается в интерфейс. Прокси для запросов в Jira берётся из окружения сервера
   (`HTTPS_PROXY`, `NO_PROXY`).
2. **Кнопки среза** — у кнопки задаются название, структура Structure (список подгружается из Jira, либо ID вручную)
   и ассоциации «эпик ← задачи Jira» с флажком «С вложенными».
3. **Поля Jira** — у кнопки выбирается, из каких полей брать остаток, затраченное время и оценку: по умолчанию
   стандартный учёт времени (`timeestimate`, `timespent`, `timeoriginalestimate`, в секундах), либо любые числовые
   поля, в том числе пользовательские (в часах или секундах); показатель можно не заполнять.
4. **Срез** — кнопка появляется в разделе «Данные по датам». По нажатию для каждого эпика суммируются выбранные поля:
   - со структурой и «С вложенными» — все задачи поддерева указанных задач в структуре (каждая один раз);
   - без структуры и «С вложенными» — задача вместе с подзадачами (поиск `parent in (...)`);
   - без «С вложенными» — только сами указанные задачи.

   Значения показываются в окне замера: их можно проверить и поправить перед сохранением.
5. **Автозаполнение** — после первого успешного среза по кнопке в разделе «Автозаполнение» включается расписание:
   дни недели, время, часовой пояс, перезапись уже внесённого замера. Сервер сам забирает срез и сохраняет замер
   на текущую дату; результат виден в карточке кнопки и в реестре изменений (автор «Автозаполнение»).
6. **Задачи Jira на дашборде** — каждый срез сохраняет состав эпиков по задачам на день среза
   (таблица `jira_issue_snapshots`). На дашборде: «Задачи Jira по эпикам» (статусы задач, доля выполненного объёма,
   проблемы данных), burn-up выбранного эпика (объём и выполнено по датам срезов) и список задач со ссылками в Jira
   и фильтром проблем: не закрыта без оценки, закрыта с остатком, в работе с нулевым остатком, перерасход.
   Таблица состава попадает и в PDF-отчёт дашборда.
7. **Что изменилось** — разбор изменения объёма эпика между двумя срезами: новые, убранные и переоценённые задачи
   (каскадная диаграмма и списки), закрытые и переоткрытые за период; в списке задач — изменения с прошлого среза.
8. **Восстановление истории** — замеры и состав задач на прошлые даты по журналу изменений Jira (фоновая задача
   с прогрессом, таблица `jira_history_jobs`); ручные замеры по умолчанию не изменяются.

**Прогноз диапазоном.** Для эпиков и этапов считается «шанс успеть» к цели и диапазон 70% (P15–P85) методом
Монте-Карло по интервалам между замерами; диапазон рисуется на графике эпика.

Для кнопок со структурой нужен плагин Structure (Tempo, ранее ALM Works) с REST API `/rest/structure/2.0`
(Jira Server/DC). Работа с Structure в Jira Cloud не проверялась: если API недоступен, приложение сообщит об этом —
в таком случае используйте кнопки без структуры.

**Планировщик автозаполнения** — фоновый поток в процессе приложения (проверка раз в 30 секунд), отдельная служба
или cron не нужны. Каждая кнопка запускается не чаще раза в день: день запуска захватывается в БД атомарно, поэтому
несколько воркеров gunicorn не дублируют срез; если сервер был остановлен во время запуска, срез выполнится позже
в тот же день. Отключить планировщик в процессе — `BURNDOWN_SCHEDULER=0`.

## Руководства

Готовые руководства пользователя и администратора (PDF и DOCX) — в [`docs/manuals`](docs/manuals).
Источники — Markdown рядом с ними. Пересборка после изменений интерфейса (нужны pandoc 3, LibreOffice,
python-docx, pypdf и Playwright с Chromium):

```bash
pip install -r requirements-dev.txt python-docx pillow
python tools/docs/shots.py           # скриншоты на демо-стенде (временная БД и учебная Jira)
python tools/docs/build_manuals.py   # PDF и DOCX
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
├── requirements-dev.txt # Зависимости для тестов
├── tests/              # Автотесты (pytest + Playwright)
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

## Автотесты

```bash
pip install -r requirements-dev.txt
playwright install chromium   # браузер для e2e-тестов (один раз)
pytest                        # все тесты
pytest tests/test_api.py tests/test_pdf.py   # только API и PDF, без браузера
```

- `tests/test_api.py` — API: авторизация, права доступа, CRUD, валидация, реестр изменений.
- `tests/test_pdf.py` — PDF-выгрузка: итоги, burn rate, фильтры, вспомогательные функции.
- `tests/test_e2e.py` — интерфейс в браузере (Playwright): расчёты на дашборде и своде, сортировка,
  режим просмотра, модальные окна, профиль, администрирование, мобильная вёрстка.
  Если браузер не установлен, тесты пропускаются; путь к уже установленному Chromium
  можно задать переменной `CHROMIUM_PATH`.

Тесты автоматически запускаются в GitHub Actions на каждый push и pull request (`.github/workflows/tests.yml`).

Тесты используют временную копию демо-базы из `init_db.py` и не затрагивают рабочую `burndown.db`.
