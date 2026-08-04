# Kosto-Vet backend

Demo-backend на FastAPI, SQLAlchemy async и PostgreSQL. Канонический контракт — `contracts/openapi.v1.yaml` (57 операций); runtime `/openapi.json` возвращает именно этот golden-файл.

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

## Проверки

```powershell
uv lock --check
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run openapi-spec-validator contracts/openapi.v1.yaml
uv run pytest --cov
```
