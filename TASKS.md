# FastAPI Production Backlog

## Durable objective

Ship the existing FastAPI backend as quickly as possible as the production
backend for the current storefront. Preserve the actual `/api/v1` contract of
the current frontend. Keep Robokassa and all online billing disabled for the
first release. Use the existing CMS/S3 and read-only MoySklad implementation
instead of rebuilding commerce functionality on Medusa.

The experimental `medusa/` implementation is frozen. Do not continue Medusa
feature work unless the owner explicitly changes direction again.

## Current checkpoint

Last audited: 2026-09-20.

- The current modular FastAPI worktree runs all 51 tests successfully with the
  isolated PostgreSQL test database enabled; Ruff and format checks pass for
  `src` and `tests`.
- The quote UUID compatibility fix, authenticated quote ownership and cart
  cleanup now have PostgreSQL regression coverage.
- The current frontend already has a CSRF manager and accepts both wrapped and
  direct customer responses; the older E2E report is stale on those points.
- Ruff is blocked only by two untracked one-off refactor helper scripts and
  mypy reports service-mixin typing debt. By owner decision on 2026-09-20,
  refactor cleanup is deferred and is not on the fastest launch path.
- Checkout previously returned `503 FEATURE_DISABLED` whenever Robokassa was
  disabled. Resolved in `FP-02`: checkout now creates a manager-confirmed unpaid
  request without requiring a delivery price.
- CMS/S3 and read-only MoySklad code already exist. Their remaining work is
  production configuration, end-to-end verification, reconciliation, and
  operational hardening rather than replacement.

## Release gates

- [x] Runtime imports, OpenAPI, tests and PostgreSQL integration pass. Existing
  refactor-only Ruff/mypy debt is recorded but does not block the first launch.
- [ ] Current storefront contract and browser journeys pass against FastAPI.
- [x] Quote and checkout work without Robokassa; retries cannot duplicate orders.
- [ ] Real S3 upload/process/attach/read/delete lifecycle passes.
- [ ] Initial catalog import and read-only MoySklad catalog/stock sync reconcile.
- [ ] API, worker, scheduler, PostgreSQL, proxy, backup/restore, logs and alerts
  pass a production-like rehearsal.
- [ ] Secrets and production origins are configured; no demo secrets/data remain.

## Ordered work packages

### [~] FP-01 — Stabilize the modular refactor (deferred)

1. Review and preserve the semantic diff from the former `ApplicationService`
   monolith; remove or intentionally retain the one-off refactor helpers.
2. Define a typed service protocol/base so mixins expose `settings`, provider
   clients and cross-mixin helpers without `attr-defined` suppressions.
3. Fix all mypy errors and keep Ruff/format clean.
4. Run the complete quality gate and PostgreSQL integration suite.

Owner decision: do not spend launch time on this package now. Preserve the
working tree and return to it after the revenue path and integrations work.

### [x] FP-02 — Close order-path P0s without billing

1. Add PostgreSQL/API regression coverage proving JSON product IDs work in B2B
   quote and unavailable IDs return the compatibility error rather than `500`.
2. Change checkout so disabled Robokassa creates a valid manual/no-online-payment
   request for manager confirmation instead of returning `FEATURE_DISABLED`.
   Delivery price is currently unknown and must not block request creation; the
   manager confirms delivery and the final amount out of band.
3. Freeze the resulting order/payment statuses and response shape in contract
   tests; keep the payment-attempt operation explicitly disabled and stable.
4. Verify stock locking, reservation expiry, price changes and idempotent retry.

Acceptance: quote and checkout journeys pass with `ROBOKASSA_MODE=disabled`, and
duplicate requests create exactly one order.

Verified: 50/50 tests pass with PostgreSQL enabled; a live HTTP checkout returned
`201 new`, no payment confirmation, pending delivery and required manager
confirmation. Replaying the idempotency key returned the same public order ID.

### [~] FP-03 — Re-run contract and storefront acceptance

1. Re-inventory the current frontend commit; do not rely on the stale E2E report.
2. Run API contract tests for all storefront operations and exact error shapes.
3. Run browser journeys for catalog/search, register/login/refresh/logout,
   profile, addresses, favorites, cart, quote, checkout and order history.
4. Record only current defects and fix backend-owned compatibility gaps.

Acceptance: the current frontend needs no backend-specific workaround or mock
fallback to complete every supported stage-1 journey.

Current: the production frontend builds and its API smoke passes 8/8 against
the local FastAPI container. Browser E2E passed catalog (195 products), product
detail, out-of-stock subscription, lead request, registration, persisted auth,
favorites, cart and authenticated quote/order creation without payment. The
created order appears in order history and purchased cart lines are removed.
E2E exposed and fixed three backend defects: missing `SessionBundle`
construction after the service refactor, missing `127.0.0.1:5173` local CORS,
and anonymous ownership/no cart cleanup on the authenticated quote route. Test
records were removed and the temporary stock change was restored to zero.
Remaining browser coverage: explicit login/logout, profile/address mutations,
search and negative/expired-session cases. The frontend still displays a local
500 RUB delivery estimate even though the server correctly creates the request
with delivery pending and a 720 RUB merchandise total; that display is frontend
logic and was not changed. Two pre-existing frontend unit tests also fail on
CSS-only expectations (`--drawer-width` and document padding).

### [~] FP-04 — Productionize CMS and S3

1. Configure the existing two-bucket model: private originals and public
   variants/CDN; verify exact Admin CORS with `kosto-vet verify-s3`.
2. Exercise upload intent -> direct upload -> complete -> worker processing ->
   public variants -> article/product attach -> detach -> delayed purge.
3. Verify MIME/magic/size/dimension rejection, stored-XSS protections, retry and
   orphan cleanup behavior.
4. Confirm the operator surface required for CMS. The backend already provides
   the APIs; a separate editor UI is a distinct deliverable and must not be
   silently assumed.

Acceptance: a real image and article can be managed end to end without API
credentials reaching the browser and without serving private originals.

Current: the REG.RU endpoint, private originals bucket, public variants bucket,
public base URL and local Admin origin are configured and pass `verify-s3`.
The originals bucket CORS preserves the REG.RU console origin and includes the
exact configured Admin origin. A real background lifecycle smoke completed:
presigned upload, completion, worker processing, four WebP variants, public
reads, product attach/detach, scheduler enqueue and worker purge. The smoke
also exposed and fixed a libvips repeated-read defect by decoding in random
access mode. Final checks found zero smoke objects in either bucket and zero
temporary DB records. Validation rejection cases and the production Admin
origin/operator surface remain.

### [~] FP-05 — Productionize initial catalog and MoySklad

1. Dry-run and review the explicit catalog manifest and allowlist; import only
   approved products and publish only after owner approval.
2. Configure token, warehouse ID and price type ID, then run a manual full sync.
3. Reconcile mapped product IDs/articles, prices and stock against MoySklad;
   unknown goods remain skipped and are never auto-published.
4. Verify incremental cursors, advisory locking, retries/429 handling,
   scheduler cadence and stale-stock behavior.
5. Produce a machine-readable reconciliation report suitable for release signoff.

Acceptance: repeated syncs are idempotent and an approved sample matches
MoySklad exactly for product identity, price and available quantity.

Current: token access, production mode, the single active warehouse and the
single sale price type are configured and verified through the real read-only
adapter. The owner approved all 195 products for publication and delegated
stock verification to stakeholders. The repeatable exporter supports an
explicit `--published` switch and generated a 195-product, four-category
manifest plus matching allowlist. The manifest was applied to the local
production-like Compose database: 195 created, then 195 updated on the repeated
idempotency run, with zero skips or duplicates. HTTP smoke reports 195 public
products and four public categories. The live local scheduler/worker then
completed an incremental catalog job for all 195 products and a zero-row stock
job, both on their first attempt. The provider stock report still contains zero
rows, so all imported availability remains zero. Remote production apply and
release reconciliation remain.

### [ ] FP-06 — External integrations still in stage 1

1. Validate Yandex OAuth start/callback/unlink with real credentials and the
   registered production callback URL.
2. Keep password-reset delivery explicitly disabled unless the storefront makes
   it a launch requirement.
3. Keep Robokassa, fiscalization, refunds and payment callbacks disabled.

Acceptance: Yandex account collision/link/unlink cases are verified and every
deferred feature returns its documented stable behavior.

### [ ] FP-07 — Production artifact and operations

1. Build immutable API/worker/scheduler/frontend images and validate production
   Compose configuration with non-demo secrets.
2. Run migrations and smoke tests from built artifacts, not editable source.
3. Add CI for static checks, tests, OpenAPI drift, image/dependency scanning and
   migration safety.
4. Configure structured logs, request IDs, error reporting, health monitoring,
   worker/job alerts and resource limits.
5. Perform database backup/restore and document RPO/RTO.

Acceptance: a clean production-like host can deploy, monitor, restart and
restore the complete stack using only documented artifacts and secrets.

### [ ] FP-08 — Release rehearsal and cutover

1. Import a production-like snapshot and reconcile counts and sampled records.
2. Rehearse deploy, migration, storefront acceptance, rollback and restore.
3. Execute monitored cutover with an observation window and rollback threshold.
4. Remove demo data and retire obsolete deployment paths only after acceptance.

Acceptance: owner signs off the current storefront journeys and operations can
both roll forward and roll back without losing orders or media.

## Inputs required from the owner

Required for `FP-02`:

- Resolved: stage-1 orders are requests for manager confirmation, without online
  payment. Delivery price is unknown at request time and is not a launch input;
- manager name, phone and email if current defaults are not final.

Required for `FP-04`:

- S3 endpoint, region, private-originals bucket, public-variants bucket, public
  base URL/CDN, access key and secret key;
- production Admin origin;
- confirmation whether a CMS editor UI is required at launch or the backend API
  is the deliverable.

Required for `FP-05`:

- initial catalog manifest/source and reviewed MoySklad allowlist;
- MoySklad token, warehouse ID and price type ID;
- a small approved reconciliation sample.

Required later:

- Yandex client ID/secret and exact callback URL;
- production API/frontend domains, cookie policy, deployment target, monitoring
  destination, backup retention and acceptable maintenance/rollback window.

Robokassa credentials are intentionally not required.

## Next executable task

Finish the remaining `FP-03` browser cases, then perform `FP-07`: production
origins/secrets, immutable images, reverse proxy, backup/restore, monitoring and
a clean-host deployment rehearsal. Yandex OAuth remains `FP-06` and requires
the real client credentials/callback URL.
