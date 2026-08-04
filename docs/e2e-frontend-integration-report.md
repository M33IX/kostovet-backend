# E2E-отчёт: интеграция Kosto-Vet frontend и backend

**Дата:** 2 августа 2026  
**Статус:** `FAIL — к релизу не готово`  
**Frontend:** `C:\repos\work\websites\kostovet-frontend`, origin `https://github.com/pipapaparapo2024/kosto-vet`, commit `8b632eb`  
**Backend:** `C:\repos\work\websites\kostovet-backend`  
**Средства:** Docker Compose, PostgreSQL, Vite development server в Docker, Playwright (Chrome), npm test/build/audit и API smoke.

## Краткий вывод

Backend и frontend можно поднять вместе, а публичный каталог действительно способен получать данные из backend. Однако связка **не готова к приёмочному или production-релизу**: критические пользовательские сценарии авторизации, выхода, избранного, корзины и оформления заказа не проходят или не могут быть пройдены через UI.

Главные причины — одновременно несовместимый HTTP-контракт, устаревшие slug'и и mock fallback на стороне frontend, а также ошибка в backend при B2B-оформлении. Поэтому ответственность распределена между обоими репозиториями; это не одна локальная проблема окружения.

## Как проводилось тестирование

1. Frontend был клонирован в требуемую директорию `C:\repos\work\websites\kostovet-frontend`.
2. Backend был поднят в Docker Compose с PostgreSQL, API, worker, scheduler, Caddy и seed-данными. Для браузерного E2E-взаимодействия Vite frontend работал отдельным временным контейнером и проксировал `/api` на API backend.
3. Для тестового браузерного origin временно был разрешён точный origin `http://localhost:5173`. Это изменение не сохранено: после теста временный env-файл, контейнеры, volumes и demo-база удалены.
4. Сценарии выполнялись в настоящем браузере Chrome через Playwright и подтверждались сетевыми ответами, а не только визуальным состоянием UI.
5. После завершения стенд остановлен, тестовые данные удалены. Frontend repository остался чистым, без изменений исходников.

## Итоги автоматических проверок

| Проверка | Результат | Комментарий |
|---|---:|---|
| Frontend unit tests | PASS | 70 тестов прошли |
| Frontend production build | PASS | сборка завершилась; Vite предупредил о крупном JS bundle (~512 kB до gzip) |
| Frontend API smoke | PASS | 8 из 8: liveness, readiness, settings, categories, products, search, anonymous `me`, validation lead |
| Backend local tests | PASS | 37 passed, 3 skipped (запускался до E2E) |
| Backend PostgreSQL integration | PASS | 3 из 3 (запускался до E2E) |
| Backend OpenAPI route inventory | PASS | 57 operation IDs соответствуют backend routes |
| Browser public catalog | PASS с оговорками | реальные товары загружаются, но fallback может заменить их mock-данными |
| Browser registration | PARTIAL PASS | регистрация и выдача cookies успешны; восстановление сессии сломано |
| Browser B2B quote checkout | FAIL | backend отвечает `500` |
| Browser favorites/logout | FAIL | frontend не передаёт CSRF, backend корректно отвечает `403` |
| Реальный B2C checkout | BLOCKED | в UI отсутствует рабочий путь добавления товара в корзину; B2B checkout также падает |
| Dependency audit runtime | FAIL | 2 high vulnerabilities: `react-router`, `react-router-dom` |

## Подтверждённые рабочие сценарии

### Каталог использует backend, когда запросы успешны

В Playwright были проверены:

- `/catalog`: `GET /api/v1/catalog/categories` и `GET /api/v1/catalog/products?...` вернули `200`;
- в интерфейсе отобразились **11 seed-товаров** backend, включая реальные article, цены и остатки;
- `/catalog/plates`: backend вернул 8 товаров категории `plates`;
- поиск `винт` отправил запрос в backend и показал один реальный товар: «Винт кортикальный 2,0 мм», 408 ₽, остаток 48;
- `/catalog/plates/plate-t-58-6`: карточка загрузила backend-товар «Пластина Т-образная», article `KV-TP-058-06`, цену 1 143 ₽ и остаток 12;
- публичная форма на `/contacts` успешно отправила `POST /api/v1/leads` и получила `202 Accepted`; UI показал «Заявка получена».

### Базовая регистрация создаёт серверную сессию

Регистрация customer через UI вернула `201 Created`. Backend выдал отдельные cookies `kv_customer_access` и `kv_customer_refresh` с ожидаемыми путями. Это подтверждает, что создание customer и первичный login-response совместимы.

## Блокеры и дефекты

Приоритеты: **P0** — немедленно блокирует основной поток/релиз, **P1** — блокирует значимую функцию и требует исправления до релиза, **P2** — существенная функциональная или data-quality проблема.

### P0 — B2B-оформление падает с HTTP 500

**Сценарий:** открыть реальную карточку товара, нажать «Заказать», заполнить валидные B2B-реквизиты и отправить заявку.  
**Фактический результат:** `POST /api/v1/orders/quote` → `500 Internal Server Error`.  
**Ожидаемый результат:** создание B2B quote/order согласно OpenAPI, либо объявленная business-ошибка, но не `500`.

**Где дефект:** backend. В `src/kosto_vet/services/application.py` в `_order_products` словарь результатов индексируется UUID-значением, а значение `item["product_id"]` после сериализации HTTP-модели является строкой. Это приводит к `KeyError` для существующего товара.

**Владелец исправления:** backend team. Frontend передавал ID реального товара, полученный из backend, и валидные данные формы.

**Что исправить:**

1. Нормализовать `product_id` к `UUID` до обращения к словарю либо строить словарь с ключами одного типа.
2. Добавить unit и PostgreSQL/API integration-тесты для B2B quote с product ID из JSON HTTP request.
3. Проверить той же функцией guest/customer B2C checkout: она использует общий путь загрузки позиций и может иметь тот же дефект.
4. Преобразовать неожиданную ошибку в контролируемую domain/API-ошибку, чтобы 500 не скрывал причину при будущем регрессе.

### P0 — frontend показывает mock-каталог вместо ошибки backend

**Сценарий:** открыть `/catalog/plastiny`.  
**Фактический результат:** backend корректно возвращает `404` для старого slug, после чего frontend подставляет локальные товары, изображения и искусственные счётчики. В браузере были видны mock-счётчики 46, 13, 59, 46, 46, 46, 134.  
**Ожидаемый результат:** frontend использует только данные backend; ошибка API явно показывается пользователю или логируется, но не заменяется скрытно mock-данными.

**Где дефект:** frontend. В `src/lib/api/catalog.js` функция `withLocalFallback` применяется ко всем основным catalog-вызовам и подменяет результат при любой ошибке.

**Владелец исправления:** frontend team.

**Что исправить:**

1. Удалить production fallback на `src/lib/localCatalog.js` и локальные catalog data из runtime-пути.
2. При ошибке API показывать явный error/loading state с retry, не вымышленные товары.
3. Если demo fallback всё же нужен для Storybook/визуальной разработки, включать его только явным build-time флагом, выключенным по умолчанию, и не включать в production bundle.
4. Добавить Playwright test: при `404` catalog endpoint UI не должен показывать локальный товар.

### P0 — старые category slug'и вызывают 404 и запускают mock fallback

**Фактические slug'и backend:** `plates`, `screws`, `tools`, `sutures`; наборов (`sets`) в demo seed и модели нет.  
**Зашитые slug'и frontend:** `plastiny`, `vinty`, `instrumenty`, `shvovny`, `nabory`.

Затронуты, в частности:

- `src/pages/CatalogPage.jsx` — ссылки на plate-подкатегории;
- `src/pages/ProductPage.jsx` — ссылка «Смотреть всё» для винтов;
- `src/components/layout/Footer/Footer.jsx` — все footer-ссылки каталога.

**Владелец исправления:** frontend team, с обязательной contract-сверкой со стороны обеих команд.

**Что исправить:**

1. Не хранить category navigation как захардкоженный список; строить его по `GET /api/v1/catalog/categories`.
2. Если SEO URL должны остаться старыми, согласовать и реализовать backend alias/redirect как отдельное контрактное решение. Нельзя молча заменить 404 mock-данными.
3. Убрать ссылку на `nabory`, если такой категории нет в договорённой модели.
4. Добавить contract/E2E test, проходящий все ссылки категорий в header/footer и требующий `200` от API.

### P0 — сессия есть на сервере, но пропадает в UI после reload

**Сценарий:** зарегистрировать customer, затем обновить страницу.  
**Фактический результат:** `GET /api/v1/account/me` возвращает `200` и объект customer, но UI отображает гостевой «Личный кабинет».  
**Причина:** `src/context/AuthContext.jsx` в `applySession` ожидает `data.customer`, а `GET /api/v1/account/me` backend возвращает customer напрямую. Login/register response использует wrapper, поэтому первоначальный экран выглядит рабочим, а reload ломается.

**Владелец исправления:** shared contract defect. Backend и frontend используют разные response shape для одной сущности без адаптера. Практическое исправление должно быть согласовано владельцем канонического OpenAPI; frontend обязан перестать предполагать незадокументированный wrapper.

**Рекомендуемое решение:**

1. Зафиксировать в canonical OpenAPI единственный формат (`Customer` напрямую или `{ customer: Customer }`) для `register`, `login`, `refresh`, `me`, `update`.
2. Предпочтительно сделать frontend tolerant на переходный период: `const customer = data.customer ?? data` с проверкой shape.
3. Либо привести backend endpoint к формату, закреплённому в OpenAPI. Не менять один endpoint без обновления остальных.
4. Добавить Playwright сценарий: register/login → hard reload → account UI остаётся authenticated.

### P0 — клиент не реализует CSRF для cookie-auth unsafe запросов

**Сценарии:** добавить товар в избранное; выполнить logout.  
**Фактический результат:**

- `POST /api/v1/account/favorites` → `403`;
- `POST /api/v1/customer/auth/logout` → `403`;
- интерфейс всё равно локально «разлогинивает» пользователя, хотя refresh/access cookies остаются; после reload backend по-прежнему подтверждает сессию (`GET /account/me` → `200`).

**Причина:** backend ожидает session-bound CSRF для unsafe cookie-auth операций, что соответствует security design. Frontend не получает `GET /api/v1/customer/auth/csrf` и не передаёт `X-CSRF-Token`.

**Владелец исправления:** frontend team. Дополнительно имеется contract/documentation debt: копия `docs/openapi.v1.yaml` frontend устарела (0.4.0 против backend 0.5.0) и не содержит CSRF endpoint/требования.

**Что исправить:**

1. Ввести один CSRF manager в API client: получать token после создания/восстановления сессии, держать его только в памяти и добавлять к POST/PATCH/PUT/DELETE cookie-auth requests.
2. На `403 CSRF` получать новый token и повторять только безопасно идемпотентный запрос; не выполнять безусловный retry для заказа или платежа.
3. Не очищать локальную auth-state после неуспешного logout; показать ошибку и сохранить состояние, пока сервер не подтвердит выход.
4. Синхронизировать frontend OpenAPI с backend golden contract; запретить stale-copy в CI.
5. Добавить E2E: favorite add/remove, cart mutation, profile patch, logout и refresh replay/CSRF rejection.

### P1 — customer cart/checkout не может быть пройден через реальный UI

**Факт:** в frontend есть `addItem` в `src/context/CartContext.jsx`, но в каталогах и карточке товара нет доступного пользовательского CTA, который его вызывает. Основной CTA «Заказать» открывает B2B quote drawer, а не кладёт реальный товар в B2C корзину.

**Следствие:** невозможно честно проверить guest или customer B2C checkout через обычный пользовательский путь. Вдобавок B2B-ветка падает с P0 выше.

**Владелец исправления:** frontend team за отсутствие UI-связки; backend team после P0 должен подтвердить B2C checkout integration tests.

**Что исправить:**

1. Явно согласовать продуктовый UX: «В корзину» для B2C, B2B quote либо selector customer type.
2. Добавить CTA в ProductCard/ProductPage, передающий backend `id`, доступный остаток и quantity в `CartContext.addItem`.
3. После CSRF-реализации провести E2E guest cart → checkout и customer cart → checkout.
4. Добавить проверку stale stock, idempotency и response errors (`DELIVERY_PRICE_NOT_CONFIGURED`) на реальном UI.

### P1 — карточка частично фабрикует данные, отсутствующие в backend response

**Подтверждено:** API карточки возвращает `category_slug`, `category_path`, `specs`, `images: []` и `related`, но не возвращает category object и variants. UI показал «Категория» в breadcrumb, заранее заданные варианты длины/отверстий/ширины и default характеристики как будто это данные товара.

**Владелец исправления:** frontend team, если UI не должен отображать данные без источника; backend team — только если эти поля действительно обязательны продуктовой спецификацией и должны быть в contract.

**Что исправить:**

1. Рендерить `category_path` и `specs`, которые реально пришли из API.
2. Не показывать variants/default specs, пока backend их не предоставил.
3. Для изображений показать честный placeholder «изображение пока недоступно», но не выдавать контент за реальное фото товара.
4. Если variants являются обязательной функцией, расширить domain/OpenAPI/backend seed и покрыть contract test, затем подключить UI.

### P1 — frontend использует устаревшую копию OpenAPI

**Факт:** `kostovet-frontend/docs/openapi.v1.yaml` имеет версию 0.4.0, backend golden contract — 0.5.0. Diff содержит 143 строки; в frontend отсутствует CSRF endpoint и часть требований безопасности.

**Владелец исправления:** владелец интеграционного контракта/релизного процесса, с участием обеих команд.

**Что исправить:**

1. Не копировать спецификацию вручную. Подключить frontend к backend `contracts/openapi.v1.yaml` через git submodule/package/release artifact или CI download по pinned version.
2. В CI сравнивать checksum/operation inventory с golden contract и падать при расхождении.
3. Генерировать typed API client или минимум contract tests из одной схемы.

### P2 — runtime dependency security audit

`npm audit --omit=dev --json` обнаружил 2 high severity advisory: `react-router` и `react-router-dom`.

**Владелец исправления:** frontend team / dependency maintenance.

**Что исправить:** обновить затронутые пакеты до исправленной версии после проверки changelog и прогнать unit, build и E2E. Не выполнять автоматический `npm audit fix` без review lockfile и тестов.

## Непройденные или ограниченно проверенные области

Это не «pass»: ниже перечислены области, которые нельзя было подтвердить из-за блокеров или отсутствия разрешённых внешних credentials.

- Guest и customer B2C checkout — заблокированы отсутствием рабочего add-to-cart UI и P0 в общем order path.
- Cart merge, quantity changes, profile update, favorites persist — заблокированы отсутствием CSRF в frontend.
- Password login после registration — endpoint/primary auth-response проверены частично через registration; полноценный login/reload journey требует исправления `me` shape.
- Yandex ID OAuth — не тестировался с реальными provider credentials; frontend integration отдельно не подтверждена.
- Robokassa sandbox callback и оплатный state machine — не тестировались с credentials; B2B quote и basket flows блокируют UI journey раньше оплаты.
- Admin API — backend API имеет demo admin operations, но в clone frontend не обнаружена отдельная admin UI surface; browser тестировать там нечего.
- Media/S3 — backend seed содержит пустые `images`; реальная CDN/media import интеграция не могла быть подтверждена.

## План исправлений и повторной приёмки

### Этап 1 — немедленные P0

1. **Backend:** исправить UUID normalization в order product lookup и добавить integration test B2B quote.
2. **Frontend:** отключить runtime local catalog fallback, заменить зашитые slug'и данными API.
3. **Contract:** синхронизировать OpenAPI 0.5.0; согласовать единый response shape customer session.
4. **Frontend:** реализовать CSRF manager и корректный logout failure UX.

### Этап 2 — завершить коммерческий путь

1. Добавить согласованный B2C «В корзину» flow в UI.
2. Исправить отображение только реальных product fields.
3. Повторить browser E2E: guest/customer cart, B2C checkout, B2B quote, favorites, profile, logout/login/reload.
4. Включить negative E2E: invalid CSRF, stale stock, duplicate idempotency key, late/duplicate payment callbacks, IDOR role matrix.

### Критерии повторного зачёта

- Ни одна catalog API ошибка не подменяется mock-товарами.
- Все header/footer/category URLs резолвятся в backend category или имеют согласованный backend redirect.
- Customer остаётся logged-in после hard reload; logout завершает серверную сессию.
- Favorite/cart/profile mutations проходят с CSRF и получают корректные ошибки при недействительном токене.
- B2B quote и guest/customer B2C checkout проходят с real product ID, реальной ценой/остатком и idempotency behavior.
- Product page не показывает variants/specs/category как реальные, если их нет в API.
- Frontend OpenAPI и backend golden contract совпадают; проверка встроена в CI.
- Dependency audit не содержит high/critical runtime vulnerabilities либо есть одобренное исключение с датой устранения.

## Вывод об ответственности

| Сторона | Ответственность |
|---|---|
| Backend | Исправить 500 при order/quote UUID lookup; подтвердить B2C после фикса; сохранить строгую CSRF защиту и не ослаблять её ради текущего клиента. |
| Frontend | Убрать mock fallback, перейти на backend category data/slug'и, реализовать CSRF, исправить session shape handling, добавить cart CTA и не показывать вымышленные свойства товара. |
| Совместно / release owner | Сделать canonical OpenAPI единым источником правды, убрать ручную копию схемы, ввести contract-gate и E2E gate в CI. |

Формулировка «вина» в данном случае означает зону исправления, а не персональную оценку: тесты показали отдельный backend defect и несколько frontend/contract defects. Без исправлений обеих сторон успешный backend сам по себе не делает пользовательскую интеграцию работоспособной.
