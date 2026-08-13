# ParserPro — парсеры интернет-магазинов

Сбор каталогов, характеристик, цен и изображений с российских маркетплейсов с сохранением в SQLite и checkpoint-based resume.

**Репозиторий:** [github.com/MaxPlaksin/ParserPro](https://github.com/MaxPlaksin/ParserPro)

## Поддерживаемые магазины

| Магазин | Модуль | Статус |
|---------|--------|--------|
| DNS | `dns_parser.py` | production |
| M.Video | `mvideo_parser.py` | production |
| Regard | `regard_parser.py` | production |
| Citilink | `citilink_parser.py` | priority run, resume |

## Что внутри

- **~44 000 товаров** в `data/products.db` (DNS, M.Video, Regard, Citilink)
- **Checkpoint/resume** — прерванный прогон продолжается без дубликатов (idempotent upsert)
- **Нормализация** — единая схема `ProductRecord` → SQLite
- **Изображения** — `storage/images/{source}/{source_id}/`
- **Excel-экспорт** — `output/{shop}/{категория}/products.xlsx` (промежуточный)

---

## Быстрый старт

### 1. Клонировать репозиторий

```powershell
git clone https://github.com/MaxPlaksin/ParserPro.git
cd ParserPro
git lfs install
git lfs pull
```

> **Важно:** база `data/products.db` (~640 MB) хранится через **Git LFS**.  
> Без `git lfs pull` скачается pointer-файл, а не сама база.

### 2. Окружение

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Playwright/CloakBrowser подтянутся через `cloakbrowser` при первом запуске браузерных парсеров.

### 3. Проверить базу

```powershell
.\.venv\Scripts\python.exe scripts\db_analytics.py
.\.venv\Scripts\python.exe scripts\show_db.py
```

---

## База данных

**Файл:** `data/products.db`  
**Схема:** `db/schema.sql`

Основные таблицы:

| Таблица | Назначение |
|---------|------------|
| `products` | товары (source, source_id, name, price, …) |
| `product_characteristics` | характеристики (JSON) |
| `product_images` | пути к локальным изображениям |
| `price_history` | история цен |
| `categories` | категории по источникам |

Уникальный ключ: `(source, source_id)` — повторный парсинг обновляет запись.

### Текущий объём (snapshot)

| source | товаров |
|--------|---------|
| mvideo | ~31 000 |
| regard | ~7 700 |
| dns | ~4 900 |
| citilink | ~450 |
| **всего** | **~44 000** |

Citilink priority run (~8 300 expected) ещё не завершён — см. раздел Citilink.

---

## Запуск парсеров

Все команды — из корня проекта, с активированным `.venv`.

### DNS

```powershell
# smoke: одна категория, 3 товара
.\.venv\Scripts\python.exe dns_parser.py --category 17a8a01d16404e77 --limit 3 -o output/dns

# полный прогон (каталог из checkpoint/manifest)
.\.venv\Scripts\python.exe dns_parser.py -o output/dns
```

DNS всегда запускается в **видимом** браузере (политика в `parser_policy.py`).

### M.Video

```powershell
# smoke
.\.venv\Scripts\python.exe mvideo_parser.py --max-categories 1 --limit 5 -o output/mvideo

# полный прогон
.\.venv\Scripts\python.exe mvideo_parser.py -o output/mvideo
```

### Regard

```powershell
.\.venv\Scripts\python.exe regard_parser.py -o output/regard_full
```

### Citilink

**Одна категория (smoke / resume):**

```powershell
.\.venv\Scripts\python.exe citilink_parser.py `
  -o output/citilink `
  --category-slug processory `
  --category 26 `
  --category-name "Процессоры" `
  --category-path "Комплектующие для ПК > Процессоры" `
  --expected-products 136 `
  --delay 4.0 `
  --profile-suffix _run
```

**Priority run — 12 категорий из manifest:**

```powershell
.\.venv\Scripts\python.exe scripts\run_citilink_priority.py `
  --manifest output\citilink\priority_manifest.json `
  --profile-suffix _run
```

При блокировке (403/429) run **останавливается** с exit code 1. Перезапуск той же команды продолжит с checkpoint.

**Диагностика Citilink:**

```powershell
.\.venv\Scripts\python.exe scripts\citilink_priority_diag.py
.\.venv\Scripts\python.exe scripts\citilink_smoke_report.py --slug processory
```

### DNS + M.Video вместе

```powershell
.\.venv\Scripts\python.exe run_parsers.py --test    # smoke
.\.venv\Scripts\python.exe run_parsers.py --full    # полный
```

### Production supervisor (долгие прогоны)

Автоперезапуск, логи, Telegram-алерты:

```powershell
# .env: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
.\.venv\Scripts\python.exe scripts\parser_supervisor.py --name dns -- `
  .\.venv\Scripts\python.exe dns_parser.py -o output/dns

.\.venv\Scripts\python.exe scripts\parser_supervisor.py --name mvideo -- `
  .\.venv\Scripts\python.exe mvideo_parser.py -o output/mvideo
```

Логи: `logs/supervisor_{name}.log`

---

## Структура проекта

```
Parser/
├── dns_parser.py          # парсер DNS
├── mvideo_parser.py       # парсер M.Video
├── regard_parser.py       # парсер Regard
├── citilink_parser.py     # парсер Citilink
├── common.py              # checkpoint, browser session, export
├── parser_policy.py       # throttle/headless policy per shop
├── run_parsers.py         # DNS + M.Video launcher
├── data/
│   └── products.db        # SQLite (Git LFS)
├── db/
│   ├── schema.sql
│   ├── repository.py      # upsert API
│   └── hooks.py           # persist_*_row
├── normalizer/            # row → ProductRecord
├── models/                # ProductRecord dataclass
├── output/                # checkpoints, xlsx (не в git)
├── storage/images/        # локальные фото (не в git)
├── scripts/               # утилиты, supervisor, analytics
└── logs/                  # логи прогонов (не в git)
```

---

## Checkpoint и resume

Каждый парсер пишет прогресс в `output/{shop}/`:

```
output/citilink/{категория}/checkpoint.json      # состояние категории
output/citilink/{категория}/checkpoint.rows.json # собранные строки
output/citilink/checkpoint_global.json           # completed categories
```

При перезапуске:
- listing продолжается с `list_page`
- detail queue (`pending`) дообрабатывается
- upsert в БД идемпотентен по `(source, source_id)`

---

## Аналитика и отчёты

```powershell
# сводка по БД
.\.venv\Scripts\python.exe scripts\db_analytics.py

# success rate, coverage, pending по всем магазинам
.\.venv\Scripts\python.exe scripts\parse_success_analytics.py
# → output/parse_success_analytics.json

# HTML-обзор магазинов
.\.venv\Scripts\python.exe scripts\build_stores_overview.py
# → output/stores_overview.html

# QA DNS
.\.venv\Scripts\python.exe scripts\build_dns_quality_review.py
# → output/dns_quality_review.html
```

---

## Политики и ограничения

Правила throttle, headless, workers — в `parser_policy.py`:

| Магазин | delay | category-delay | workers | headless |
|---------|-------|----------------|---------|----------|
| DNS | ≥4s | ≥20s | 1 | always visible |
| M.Video | policy | policy | policy | optional |
| Citilink | ≥4s | ≥20s | 1 | optional |

Citilink при defer/block **не переходит** к следующей категории — run останавливается для безопасного resume.

---

## Что не в git

`.gitignore` исключает:

- `output/` — checkpoints и xlsx
- `storage/` — изображения
- `logs/` — логи
- `probe_*.py` — разовые исследовательские скрипты
- `.venv/`, `.env`

В git **есть:** код, `db/schema.sql`, `data/products.db` (LFS).

---

## Разработка

```powershell
# тесты
.\.venv\Scripts\python.exe -m pytest

# миграции БД (добавление колонок)
.\.venv\Scripts\python.exe -c "import sqlite3; from db.migrate import migrate; c=sqlite3.connect('data/products.db'); migrate(c); c.commit()"
```

---

## Лицензия и данные

Парсинг выполняется в исследовательских/внутренних целях. Соблюдайте robots.txt и rate limits магазинов. Не публикуйте `.env` и Telegram-токены.
