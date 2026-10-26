# Руководство пользователя — исходники

Готовый файл: [`../user-guide.pdf`](../user-guide.pdf).

| Файл | Назначение |
|------|------------|
| `manual.html` | Текст и вёрстка руководства (HTML, печатается в PDF) |
| `shots.py` | Снимает скриншоты приложения в `img/` (Playwright) |
| `build.py` | Собирает PDF: оглавление с номерами страниц, колонтитулы, закладки |

## Пересборка после изменений интерфейса

```bash
pip install -r requirements-dev.txt requests
# 1. Приложение на демо-базе (отдельный каталог, рабочая БД не затрагивается)
DB_PATH=/tmp/guide/demo.db python init_db.py
DB_PATH=/tmp/guide/demo.db SECRET_KEY=demo python -c "from app import app; app.run(port=5055)" &
# 2. Скриншоты (русская локаль браузера нужна для формата дат ДД.ММ.ГГГГ)
python docs/user-guide/shots.py
# 3. PDF
python docs/user-guide/build.py docs/user-guide.pdf
```

Скрипты используют Chromium из Playwright; путь к другому Chromium можно задать в `CHROMIUM_PATH`.
Каталог `img/` создаётся заново и в репозиторий не добавляется.
