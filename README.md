# Kosto-Vet backend

Demo-backend на FastAPI, SQLAlchemy async и PostgreSQL. Канонический контракт — `contracts/openapi.v1.yaml` (77 операций); runtime `/openapi.json` возвращает именно этот golden-файл.

Подробная карта слоёв, модулей, методов, данных и runtime-потоков: [docs/backend-service-map.md](docs/backend-service-map.md).

## Локальный запуск

Нужны `uv`, CPython 3.14 и PostgreSQL 18.

```powershell
Copy-Item .env.example .env
uv sync --all-groups
uv run kosto-vet migrate
uv run kosto-vet seed-demo
uv run kosto-vet create-staff --email admin@example.test --password "change-me-now" --name Admin --role admin
uv run kosto-vet api
```

Отдельные процессы:

```powershell
uv run kosto-vet worker
uv run kosto-vet scheduler
```

## Compose demo

```powershell
Copy-Item .env.example .env
docker compose up -d --build
docker compose --profile tools run --rm seed
```

Наружу публикуются только Caddy `80/443`; API, PostgreSQL и фоновые процессы находятся во внутренних сетях. Redis включается отдельно профилем `redis` и не обязателен.

## Production template

Задайте immutable `BACKEND_IMAGE`, `FRONTEND_IMAGE`, единый адрес сайта `FRONTEND_HOST`, `ADMIN_ALLOWED_CIDRS`, независимые secrets и production PostgreSQL credentials. Caddy отправляет `/api/*`, `/health/*`, `/openapi.json`, `/docs` и `/metrics` в backend, остальные пути — во frontend. Фронтенд должен собираться с пустым `VITE_API_BASE_URL`, чтобы обращаться к `/api` на текущем адресе страницы.

Для временного HTTP по IP укажите `FRONTEND_HOST=http://SERVER_IP`. При переходе на домен в настройках Caddy достаточно заменить это на `FRONTEND_HOST=shop.example.ru`: Caddy автоматически включит HTTPS, когда DNS указывает на сервер и порты 80/443 доступны. В backend `.env` одновременно обновите `API_PUBLIC_BASE_URL=https://shop.example.ru`, `FRONTEND_ORIGINS=https://shop.example.ru` и `TRUSTED_HOSTS=shop.example.ru`; настройте точные `ADMIN_ORIGINS` и OAuth callback, если они используются. Для HTTP по IP cookie с `COOKIE_SECURE=true` не будут работать; production-режим backend требует HTTPS.

```sh
docker compose -f docker-compose.yml -f docker-compose.prod.yml config
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d
```

Demo seed в production автоматически не запускается. Production Robokassa всегда заблокирована в demo-сборке; юридический запуск, fiscalization, refunds и реальные платежи требуют отдельной реализации и приёмки. Backup-инструкция: `deploy/backup/RESTORE.md`.

При `ROBOKASSA_MODE=disabled` checkout создаёт неоплаченную заявку со
статусами `new` / `not_required`. Стоимость доставки можно не задавать:
`FIXED_DELIVERY_PRICE_MINOR` отсутствует, предварительная сумма содержит только
товары, а доставку и итог подтверждает менеджер. Платёжная попытка и складская
резервация для такой заявки не создаются.

### Первичный импорт production-каталога

Подготовьте reviewable manifest по шаблону `docs/catalog-import.example.json` и отдельный allowlist внешних ID по шаблону `docs/catalog-import-allowlist.example.txt`. Импортёр не запрашивает весь каталог у провайдера: он создаёт или обновляет только явно перечисленные позиции. В production allowlist обязателен, все товары по умолчанию остаются неопубликованными.

```sh
# Сначала сверка без записи.
uv run kosto-vet import-catalog ./catalog.json --allowlist ./catalog-allowlist.txt

# Запись только после сверки отчёта и утверждения manifest/allowlist.
uv run kosto-vet import-catalog ./catalog.json --allowlist ./catalog-allowlist.txt --apply
```

Для CMS укажите два разных бакета (private originals и public variants), точный HTTPS `ADMIN_ORIGINS` и выполните read-only проверку до запуска API/worker:

```sh
uv run kosto-vet verify-s3
```

Команда проверяет доступ к обоим бакетам и наличие exact admin origins в CORS private originals bucket; ключи и объекты она не создаёт и не изменяет.

### МойСклад

Интеграция read-only по отношению к МойСклад. При старте scheduler ставит полный catalog-sync, который создаёт и публикует новые товары и категории, а затем обновляет каталог каждые 15 минут и остатки каждую минуту. Товары без настроенного типа цены пропускаются; архивные позиции снимаются с публикации. Для включения укажите `MOYSKLAD_MODE=sandbox` или `production`, token, warehouse ID и price type ID и постоянно запускайте отдельные процессы worker и scheduler.

Полный catalog-sync запускает первоначальный импорт фотографий. Затем scheduler повторяет проверку фотографий всех импортированных товаров раз в час (`MOYSKLAD_MEDIA_SYNC_INTERVAL_SECONDS`, по умолчанию 3600). Каждая карточка обрабатывается отдельной повторяемой job: изображения скачиваются из МойСклад, преобразуются в WebP-размеры `thumb`/`card`/`detail` и складываются напрямую в `S3_PUBLIC_MEDIA_BUCKET` по публичному `S3_PUBLIC_BASE_URL`. Нужны права записи в этот бакет и публичное чтение объектов. Неизменившиеся фотографии повторно не загружаются. Если фото в МойСклад отсутствует или удалено, импортированное изображение скрывается в каталоге; вручную добавленные фото не затрагиваются. Для повторного полного запуска используйте существующий admin sync endpoint; он поставит catalog job, после которой автоматически появятся media jobs. Состояние видно в `integration_jobs` (`provider='moysklad'`, `kind='media'` / `product_media`).

## Проверки

```powershell
uv lock --check
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run openapi-spec-validator contracts/openapi.v1.yaml
uv run pytest --cov
```
