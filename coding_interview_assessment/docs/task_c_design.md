# Task C – Xero Integration Review

## C1 – Connection verification

Smallest sequence that proves OAuth and tenant selection:

1. Authorize + token exchange: `POST https://identity.xero.com/connect/token` (`grant_type=authorization_code`, `code`, `redirect_uri`, PKCE `code_verifier`). Requested scopes must include `offline_access` (refresh token) plus the invoice scopes.
   Proves: client credentials, redirect URI and `state` check are right; the response contains `access_token` (≈30 min), `refresh_token`, `expires_in`, `scope`. Check the returned `scope` actually contains what was requested (users can grant less).
2. List connections: `GET https://api.xero.com/connections` with `Authorization: Bearer <access_token>`.
   Proves: the token is accepted by Xero and shows which organisations the user authorised. Each entry has `tenantId`, `tenantType` (`ORGANISATION`), `tenantName`, `id` (connection id). Persist the `tenantId` the user selected; fail if it is not in the list (revoked / wrong org).
3. Tenant-scoped read: `GET https://api.xero.com/api.xro/2.0/Organisation` with `Authorization` + `xero-tenant-id: <tenantId>`.
   Proves: the header-based tenant routing and the scopes work end to end (cheap, single-record call). Compare `OrganisationID`/name with what is stored.
4. Refresh once (in a non-production verification or on first save): `POST /connect/token` with `grant_type=refresh_token`; store the new refresh token atomically (Xero rotates it – the old one is single use, with a short grace window).
   Proves: background workers will keep working after the first access token expires.

Only after 1–4 pass mark the connection `healthy`; then read invoices.

---

## C2 – Failure diagnosis (`/connections` OK, `/Invoices` fails)

```text
Invoices call fails
├─ 401 Unauthorized
│   ├─ WWW-Authenticate: ... token_expired / expires_at in the past → refresh (once), retry once
│   ├─ refresh returns invalid_grant → refresh token expired (60 days unused), already used
│   │   (rotation race: two workers refreshed), or user revoked → mark connection "needs reauth"
│   ├─ token issued for a different client_id / environment (e.g. staging app creds with prod token)
│   └─ malformed header (missing "Bearer ", whitespace, truncated token in config)
├─ 403 Forbidden
│   ├─ scope missing: compare token `scope` with required (accounting.transactions or granular
│   │   accounting.invoices[.read]; for newer apps broad scopes may be unavailable) → re-consent
│   ├─ xero-tenant-id missing, or tenant not in this token's /connections list → re-select tenant
│   ├─ user lacks permission in that org (e.g. no access to sales invoices / role removed)
│   ├─ org subscription/app-limit issues (uncertified app connection limit) → check app dashboard
│   └─ IP/gateway/proxy stripping the xero-tenant-id header (compare direct curl vs service)
└─ 404 Not Found
    ├─ wrong base URL/version path (must be /api.xro/2.0/Invoices, case-sensitive resource)
    ├─ requesting a specific InvoiceID that does not exist in *this* tenant
    │   (id from another environment/org – demo vs prod data)
    └─ wrong tenant selected (id valid, but record lives in another org)
```

Environment configuration checks (differs between dev/stage/prod): client id/secret, redirect URI, scopes list in config, stored `tenantId` per environment, secret manager version, clock skew (token expiry computed locally), and whether the Xero *Demo Company* is used in non-prod (demo data resets every 28 days, so stored IDs go stale → 404). Log `xero-correlation-id` from the failing response to quote to Xero support.

---

## C3 – Incremental synchronisation

 Per-tenant state (DB row): `tenant_id`, `cursor_modified_since` (UTC timestamp), `last_successful_run`, `status`, `current_run_id`, `page_checkpoint`.

Algorithm (resumable):

1. Acquire a per-tenant lock/lease (DB advisory lock or row `lease_until`) so only one worker syncs a tenant.
2. `run_started = now()` (UTC). Window start = `cursor_modified_since − overlap` (e.g. 5 min overlap to cover clock skew/in-flight commits).
3. Request `GET /Invoices?page=N&pageSize=100&order=UpdatedDateUTC ASC` with header `If-Modified-Since: <window start>` (Xero returns records modified after that time). Use `Statuses`/`where` only if the business needs them; include archived/voided/deleted statuses so deletions/voids propagate.
4. Pagination: loop `page=1,2,…` until a page returns fewer than `pageSize` (or the `pagination` object says no more). After each page: upsert in one DB transaction and store `page_checkpoint = N`. A crash resumes at page N (replay of page N is safe because writes are idempotent upserts).
5. Idempotent upserts keyed on `(tenant_id, InvoiceID)`; ignore a record whose `UpdatedDateUTC` is not newer than the stored one (handles overlap duplicates and out-of-order replays).
6. Cursor persistence: only when all pages succeed, set `cursor_modified_since = max(UpdatedDateUTC seen)` (or `run_started` minus overlap) and clear the checkpoint. Never advance the cursor on partial failure.
7. Partial failure: keep cursor, keep checkpoint, release lease, schedule retry with backoff (see C4). Poison records (validation failure mapping to internal model) go to a dead-letter table with `InvoiceID`, error, `xero-correlation-id` rather than blocking the whole run.
8. Large organisations / first sync: initial backfill uses a fixed `modified-since = epoch` split by date ranges (`where=Date>=DateTime(...)&&Date<DateTime(...)`) so each window is independently resumable; process windows in parallel only within the concurrency limit (C4).
9. Webhooks (optional optimisation): Xero invoice webhooks only notify of changes; they trigger a sync run, they do not replace the polling cursor (webhooks can be missed). Verify the `x-xero-signature` HMAC before trusting them.
10. Safe replay: because of overlap + upsert + monotonic `UpdatedDateUTC` check, re-running any window gives the same final state.

---

## C4 – Rate limits (HTTP 429)

Published limits (verify): 60 calls/min and 5,000/day per tenant, 5 concurrent calls per tenant, plus an app-wide per-minute cap**.

On 429:

 Read response headers: `Retry-After` (seconds to wait), `X-Rate-Limit-Problem` (`minute`, `day`, `concurrent`, `appminute`), and the `X-MinLimit-Remaining`, `X-DayLimit-Remaining`, `X-AppMinLimit-Remaining` counters (also on successful responses – use them proactively).
  Back-off:** wait `max(Retry-After, backoff)` where `backoff = min(cap, base·2^attempt)` with full jitter (`sleep = random(0, backoff)` but never less than `Retry-After`) so workers don't wake together.
  Concurrency limit: a per-tenant semaphore (max 5, better 2–3 to leave room for user-facing calls) plus a token bucket of ~50 req/min per tenant, shared across workers via Redis.
  Retry budget: e.g. max 5 attempts and a per-job wall-clock budget; per-tenant circuit breaker – if `X-Rate-Limit-Problem: day`, **stop the tenant until the next UTC day reset** and reschedule the job instead of sleeping in a worker.
  Scheduling: queue jobs per tenant (one active sync per tenant), spread tenants' start times, prioritise user-triggered over backfill, and release the worker slot while waiting (re-enqueue with delay, don't `sleep` for minutes).

 Retry: 429, 500, 502, 503, 504, network timeouts/connection resets (with idempotency protection for writes, see C5).
 Do not retry: 400 (validation – fix payload), 401 after one refresh attempt fails, 403 (scope/permission), 404, 409/412-type conflicts, and any 4xx other than 408/429; also don't retry `invalid_grant`. Retrying these only burns the limit.

---

## C5 – Data integrity (duplicate invoices after timeouts)

  Idempotency key:** generate a deterministic key per internal order/invoice intent (e.g. `sha256(tenant_id + order_id + invoice_version)`, ≤128 chars) and send it in the `Idempotency-Key` header on `PUT/POST /Invoices`. A retry with the same key returns the original result instead of creating a second invoice. (Check the key-retention window in the docs.)
  Outbox pattern:** persist `invoice_sync` row (`order_id`, `idempotency_key`, `status=PENDING`, `xero_invoice_id=NULL`) before the call; update to `CREATED` with the returned `InvoiceID` in the same transaction as the response handling. A unique index on `(tenant_id, order_id)` blocks local duplicates.
  Business-level guard:** set a unique, deterministic `InvoiceNumber` or `Reference` (= internal order id). Xero rejects a duplicate `InvoiceNumber` for the same type, which is a second safety net.
  Lost response reconciliation** (request succeeded, response lost): rows stuck in `PENDING` older than a threshold are reconciled by querying before retrying: `GET /Invoices?where=Reference=="ORD-123"` (or by `InvoiceNumber`/ `InvoiceID` if known). Found → mark `CREATED` with that ID. Not found → retry with the **same** idempotency key. Periodically run a sweep comparing internal orders vs Xero invoices by reference to catch any drift and alert on mismatches.
  Never regenerate a new key/number on retry.



## C6 – Observability and security

 Logs (structured JSON): `correlation_id` (ours, propagated from the job/request), `xero-correlation-id` (from every response – essential for Xero support), `tenant_id` (internal org id; Xero tenant id is not secret but treat as customer identifier), `job_id/run_id`, endpoint + method (path template, not full query), status code, latency, attempt number, page, record counts, cursor before/after, rate-limit headers.

 Metrics: request count/latency by endpoint and status class; 401/403/404/429 rates; remaining-limit gauges per tenant; sync lag (`now − last_successful_run`); records upserted; DLQ size; token refresh success/failure; duplicate-prevented counter; pending-reconciliation age.

 Alerts:  refresh failures / `invalid_grant` spike or connection moved to `needs_reauth`; sync lag above SLO per tenant; sustained 429 or daily limit hit; 5xx rate > threshold; DLQ growth; reconciliation mismatch > 0; webhook signature failures.

 Never log: access/refresh tokens, client secret, authorization codes, `Authorization` headers, PKCE verifier, webhook signing key, full invoice bodies with customer PII/bank details (log IDs, not payloads); scrub with a log-filter allow-list and redact `token`-like fields in error objects from the SDK.

Secrets / token storage:  client secret in a secrets manager (not env files in git); refresh/access tokens encrypted at rest (envelope encryption, KMS key per environment), per-tenant rows, access restricted to the sync service role; refresh under a per-tenant lock and **write the rotated refresh token transactionally** before using the new access token; rotate the client secret on a schedule (Xero supports multiple secrets for zero-downtime rotation) and on suspected leak; on disconnect call `DELETE /connections/{id}` and purge tokens; least-privilege scopes.



## References

 OAuth 2.0 flow & token lifetimes – <https://developer.xero.com/documentation/guides/oauth2/auth-flow>
 Tenants / connections – <https://developer.xero.com/documentation/guides/oauth2/tenants>
 Scopes – <https://developer.xero.com/documentation/guides/oauth2/scopes>
 Rate limits – <https://developer.xero.com/documentation/guides/oauth2/limits>
 Accounting API: Invoices (paging, `If-Modified-Since`, `where`) – <https://developer.xero.com/documentation/api/accounting/invoices>
 Requests & responses (headers, idempotency, errors) – <https://developer.xero.com/documentation/api/accounting/requests-and-responses>
 Webhooks – <https://developer.xero.com/documentation/guides/webhooks/overview>
 Status/error codes – <https://developer.xero.com/documentation/api/accounting/responsecodes>

