# Один запрос — travel agent

Пользователь описывает поездку обычным языком. Агент уточняет условия, сравнивает варианты, сохраняет выбранный маршрут и пересобирает план при изменении пожеланий. Демо-оплата создаёт вымышленный счёт и квитанцию. Web-приложение поддерживает библиотеку поездок, память и необязательную интеграцию с Telegram.

Это самостоятельная публичная копия проекта хакатона. Начальный коммит `fork` содержит очищенный снимок исходников. Прежняя история, рабочие окружения, материалы жюри, операторские скрипты, настройки развёртывания и исторические отчёты в копию не перенесены.

## Архитектура

- **Agent:** FastAPI/Pydantic, SQLite, команды, авторизация, SSE, планирование и simulated checkout.
- **Provider gateway:** отдельный сервис данных, локальный OSM/Wikidata-каталог, поиск, погода, события и источники цен.
- **Workers:** Chromium для гостиниц и авиаперелётов, SearXNG, локальный CPU encoder Ollama/Qwen.
- **Frontend:** React, TypeScript и Vite.

Планировщик зависит от `TravelDataPort`, `IntentModelPort` и `PlanningRepository`. Секреты inference находятся только в agent, настройки провайдеров — в gateway. Один agent worker разделяет очередь и лимитер inference: не более 10 запросов за скользящие 60 секунд, включая повторы и извлечение цен.

[Архитектура](docs/architecture.md) · [API](docs/API_CONTRACT.md) · [контракт данных](docs/TRAVEL_DATA_CONTRACT.md) · [встраивание в host-приложение](docs/host-integration.md).

## Локальный запуск

Нужен Docker с Linux containers и Docker Compose. Запуск не требует Kubernetes.

```sh
git clone https://github.com/cyprus7/o-neway-agents.git
cd o-neway-agents
cp .env.example .env
```

В PowerShell вместо `cp` можно использовать `Copy-Item .env.example .env`. Создавайте `.env`, только если собственного файла ещё нет. Заполните `LLM_API_KEY` своим ключом совместимого inference API; при необходимости задайте `LLM_BASE_URL` и `LLM_MODEL`. Ключи и готовые учётные записи не поставляются.

```sh
docker compose up --build
```

[Приложение](http://localhost:8080) · [health](http://localhost:8080/health) · [OpenAPI](http://localhost:8080/docs). Только app публикует локальный порт. Первое создание encoder volume требует загрузки закреплённой модели; следующие запуски используют её сохранённую копию.

Без ключа inference интерфейс и health доступны, но планирование сообщает о недоступности модели. Fixtures не заменяют inference в штатном запуске.

| Переменная | Назначение |
|---|---|
| `LLM_API_KEY`, `LLM_BASE_URL`, `LLM_MODEL` | Собственная конфигурация inference |
| `LLM_ENABLED=0` | Явная пауза inference |
| `WEB_FALLBACK_ENABLED=0` | Запрет внешних travel-data HTTP; inference настраивается отдельно |
| `LOCAL_CATALOG_ENABLED=0` | Отключение локального каталога |
| `OLLAMA_API_KEY`, `SERPAPI_API_KEY` | Необязательные ключи источников gateway |
| `APP_PORT` | Локальный порт app, по умолчанию 8080 |
| `TELEGRAM_MODE` | Необязательный канал, по умолчанию disabled |

Для Telegram используются только собственные bot token и webhook secret из environment. Для публичного доступа отдельно настраиваются origin, защищённый ingress, HTTPS и подтверждение личности; готового внешнего окружения в репозитории нет. Host-продукт отвечает за авторизацию своих пользователей.

## Проверки

Для Python используйте отдельное виртуальное окружение, Python 3.12 и зависимости разработки. Для frontend нужен Node.js 22.

```sh
python -m venv .venv
# Activate .venv with the command appropriate for your shell.
python -m pip install -r backend/requirements-dev.txt
python -m pytest backend/tests tests -q
npm --prefix frontend ci
npm --prefix frontend run build
npm --prefix frontend test
```

Дополнительные browser checks: `npm --prefix frontend run test:e2e`. Для запущенного локального приложения: `python scripts/smoke_test.py`; этот smoke не вызывает модель. Live evaluation через `scripts/evaluate_wishes.py --live` создаёт синтетические поездки и расходует общую квоту inference.

## Границы данных

Неизвестные реальные цены, наличие мест и специальные условия остаются неизвестными. Публичные тарифные ссылки не подтверждают доступность или итоговую цену поездки. Иллюстративные диапазоны `demo_pricing` и суммы simulated checkout — отдельные вымышленные примеры. Приложение не бронирует и не проводит реальные платежи.

Каталог содержит 1300 именованных OSM lodging source POI, включая 682 hotel-kind, и 20 exact-ID записей Wikidata. Уникальность source ID не означает уникальность работающих предприятий. Сохранены исходные timestamps, field lineage и условия лицензирования: [текущий каталог](backend/app/catalog/assets/cyprus/LICENSE.md), [архивный snapshot](backend/app/catalog/assets/cyprus/archive/cyprus-hotels-20261025-v1/LICENSE.md). Данные OSM распространяются по ODbL; Wikidata — CC0. Эти условия не задают лицензию остальных исходников.
