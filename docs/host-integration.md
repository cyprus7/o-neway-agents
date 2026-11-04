# Встраивание в host-продукт

Точка входа — [`examples.host_integration.create_host`](../examples/host_integration.py). Она создаёт одно приложение агента, монтирует его под `/travel` и явно входит в lifespan дочернего FastAPI. Lifespan смонтированного приложения автоматически не запускается. Standalone React использует корневые `/api/...`; host-фронтенд должен использовать `/travel/api/...`.

Это прикладной модуль с конкретным контрактом хранения и lifecycle, не универсальный storage SDK. Host отвечает за проверку своей учётной записи, порядок middleware, внешний доступ, CSRF своего канала и отзыв доступа. Агент проверяет внутреннего владельца поездки, задания, SSE, памяти и квитанций.

## Доверенная личность

Ниже граница из [`tests/test_host_productization.py`](../tests/test_host_productization.py): подтверждённый subject приходит из серверного middleware host-продукта и отображается в устойчивого внутреннего пользователя. Код не проверяет host-login вместо вашего middleware.

```python
from backend.app.application.dependencies import AgentDependencies
from backend.app.domain.identity import Principal
from examples.host_integration import create_host

app = create_host(dependencies=AgentDependencies(
    identity_resolver=lambda request: getattr(request.state, "verified_principal", None)
))

@app.middleware("http")
async def map_verified_host_identity(request, call_next):
    # An OUTER, verified host authentication middleware sets this server-side.
    # Never derive it from an unverified public header, query or form field.
    subject = getattr(request.state, "verified_subject", None)
    request.state.verified_principal = None
    if subject is not None:
        issuer = "host:example-product"
        store = app.state.travel_agent.state.store
        try:
            user = store.resolve_identity(issuer, str(subject))
        except KeyError:
            # A deleted internal account must not be recreated or become a 500.
            # Leave no principal; the agent returns 401 for private API routes.
            pass
        else:
            request.state.verified_principal = Principal(user.user_id, issuer, str(subject))
    return await call_next(request)
```

Host обязан обеспечить выполнение проверяющего middleware **до** mapper. Subject — стабильный ID учётной записи, не отображаемое имя. `identity_resolver(request)` может вернуть `Principal`, `None` либо awaitable с таким результатом. Нет допустимого пользователя — `401`. Заголовок `X-User-ID` не выбирает владельца. Настроенный resolver заменяет стандартную cookie-границу: host обеспечивает защиту своих browser mutations и не принимает недоверенный principal.

Для standalone Web resolver не задаётся: `POST /api/auth/guest` выдаёт 12-часовую opaque HttpOnly cookie и отдельный CSRF token. HTTPS cookie Secure; loopback HTTP — явное исключение. Mutations требуют точный разрешённый Origin и `X-CSRF-Token`. Telegram проверяет подписанные `initData` сервером и выдаёт тот же механизм доступа; автоматического объединения с гостем нет.

## Фабрики и владение ресурсами

Фабрики `AgentDependencies` синхронно создают ресурс один раз при входе в lifespan.

| Параметр | Вызывается как | Контракт |
| --- | --- | --- |
| `repository_factory` | `factory(path, on_change=notifications.notify)` | Store-совместимые транзакции, ownership, migrations, команды, память и outbox; `.close()` |
| `model_factory` | `factory(shared_http_client, store)` | `IntentModelPort.extract(session)`; необязательный `.aclose()` |
| `travel_data_factory` | `factory(shared_http_client, singleton_model)` | `TravelDataPort`; необязательный `.aclose()` |
| `identity_resolver` | `resolver(request)` | Только доверенная проверенная личность host-продукта |

`database_path` задаётся в `create_host(...)`/`create_app(...)`. Сохранён прежний аргумент `travel_data_factory(client)` у этих функций; его нельзя задавать одновременно с `AgentDependencies.travel_data_factory(client, model)`.

По умолчанию создаются один `LLMClient(client, store)` и `KnowledgeTravelDataPort(HttpTravelDataPort(client, PROVIDER_GATEWAY_URL), llm)`. Планирование и опциональное извлечение цен используют **тот же** model instance, сериализацию, rolling limiter 10/60 секунд и backoff. Шлюз не содержит inference, сессий или checkout. Можно явно заменить data port корпоративным адаптером либо чистым `HttpTravelDataPort`; скрытого embedded-provider fallback нет.

Если custom model используется с enrichment, совместимость price-extraction интерфейса должна быть явной. Не создавайте второй inference client внутри data factory. Host, заменяющий model factory, отвечает за квоты, сериализацию, ошибки и освобождение своего адаптера: `IntentModelPort` сам по себе не добавляет Hetzner limiter. Обычный `LLMClient` сохраняет общие ограничения. Отдельная модель на запрос, пользователя, канал или mount не поддерживается.

Lifespan владеет SQLite, общим HTTP client, моделью, data port, одним bounded command worker, Telegram/reminder lifecycle и retention. Фоновые задачи закрываются до зависимых ресурсов. Один runtime не следует запускать повторно или независимо на нескольких mounts. Тесты подтверждают одну модель для двух владельцев, использование её планировщиком, закрытие ресурсов ровно один раз и остановку SSE после отзыва пользователя. Это fixture-доказательство lifecycle, не сетевой SLO.

## Команды и обновления

Используется тот же [API](API_CONTRACT.md), с префиксом mount:

```http
POST /travel/api/sessions
Content-Type: application/json

{"apply_memory": false}
```

```http
POST /travel/api/sessions/<trip-id>/messages
Content-Type: application/json
Idempotency-Key: host-command-0001

{"content":"Из Варшавы в Ларнаку", "expected_revision":0}
```

Здесь личность уже проверена host middleware. Cookie-каналу дополнительно нужны его cookie/Origin/CSRF. `202` — приём команды, не готовый маршрут. Следите за `/travel/api/jobs/{job_id}` и аутентифицированным SSE `/travel/api/sessions/{id}/events`; cursor не даёт доступа. Сервер повторно проверяет доступ перед приватным snapshot/heartbeat.

Одинаковый владелец, ключ и тело возвращают прежний результат; другое тело под тем же ключом — `409`. Для нового действия создаётся новый ключ, при повторе сетевого запроса сохраняется старый. Устаревшая ревизия — `409`; нет входа — `401`; чужая или отсутствующая сущность — одинаковый `404`; переполнение admission — `429`. Выбор и демо-checkout дополнительно привязаны к варианту, ревизии и счёту.

`POST /travel/api/jobs/{id}/cancel` отменяет работу с commit fence, но не возвращает потраченную model quota. Только не начатые queued-команды возобновляются после рестарта. Running-at-restart становится `interrupted`; повтор требует нового ключа и текущей ревизии. Публичные статусы сохраняют `completed`, а также `queued/running/failed/superseded/canceled/interrupted`.

Сравнение/объяснение идут в `/messages` с `action: "compare" | "explain"` и текущими `option_ids` (до трёх). Они читают сохранённые доказательства без новой модели/источников и смены ревизии. Изменения маршрута инвалидируют selection/quote. Историческая демо-квитанция неизменна; amendment создаёт отдельную связанную поездку.

Проверка: `python -m pytest tests/test_host_productization.py backend/tests/test_host_integration.py -q`. Для собственного внешнего доступа host-продукт настраивает аутентификацию, TLS и сетевую политику. Пример показывает только интеграцию приложения и его lifecycle.
