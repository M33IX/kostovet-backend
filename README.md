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

Задайте immutable `BACKEND_IMAGE`, `FRONTEND_IMAGE`, точные `API_HOST`/`FRONTEND_HOST`, `ADMIN_ALLOWED_CIDRS`, независимые secrets и production PostgreSQL credentials:

```sh
docker compose -f docker-compose.yml -f docker-compose.prod.yml config
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d
```

Demo seed в production автоматически не запускается. Production Robokassa всегда заблокирована в demo-сборке; юридический запуск, fiscalization, refunds и реальные платежи требуют отдельной реализации и приёмки. Backup-инструкция: `deploy/backup/RESTORE.md`.

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

Интеграция строго read-only: она обновляет только уже импортированные позиции, сопоставленные по external ID или артикулу; новые товары из МойСклад не публикует и не создаёт. Для включения укажите `MOYSKLAD_MODE=sandbox` или `production`, token, warehouse ID и price type ID. Scheduler ставит incremental stock-sync раз в минуту и catalog-sync раз в 15 минут; полный sync доступен только через защищённый staff admin API. До включения в production выполните dry-run первичного импорта, затем manual full sync и сверку цен/остатков на утверждённой выборке.

## Проверки

```powershell
uv lock --check
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run openapi-spec-validator contracts/openapi.v1.yaml
uv run pytest --cov
```
