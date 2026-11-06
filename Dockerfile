# syntax=docker/dockerfile:1
# Burndown Tracker: Flask + Gunicorn, SQLite в томе /data.
# Сборка и запуск — docker compose up -d --build (см. docker-compose.yml).
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Europe/Moscow

WORKDIR /app

# Сначала только зависимости — слой переиспользуется, пока requirements.txt не меняется
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Код приложения; база и ключ сессий живут в томе /data (в образ их не собираем)
COPY app.py init_db.py forecast.py holidays_ru.py insights.py confluence.py jira_integration.py notify.py ./
COPY static ./static
COPY templates ./templates
COPY docs ./docs

ENV DB_PATH=/data/burndown.db
VOLUME /data

# python:3.12-slim без wget/curl — healthcheck на стандартной библиотеке
HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=5 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:5000/healthz', timeout=4)"

EXPOSE 5000

# Первая база создаётся автоматически (init_db идемпотентен на пустой базе)
CMD ["sh", "-c", "[ -s \"$DB_PATH\" ] || python init_db.py; exec gunicorn -w 4 -k gthread --threads 8 --timeout 120 -b 0.0.0.0:5000 app:app"]
