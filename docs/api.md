# MyNews HTTP API (v2 contract, `api: 1`)

> **Status.** This is the single wire contract between the v2 backend and the v2 iOS app, written before
> the code (Phase 1, step 1). The backend owns it. A change starts here and in the JSON Schemas under
> [`docs/api/schemas/`](api/schemas/), and only then reaches code. Backend snapshot tests render real
> responses into golden fixtures, the fixtures are copied into `ios/MyNewsTests/Fixtures`, and CI fails
> when the two differ.
>
> Every route carries a phase tag. **P1** routes ship in Phase 1. **P2** and **P3** routes are specified
> here so the app, the data model and the fixtures can be designed against them, but they are **not
> registered** until their phase. `backend/tests/test_route_inventory.py` holds the registered set, so a
> route cannot appear early by accident. The storage side of every route is in
> [`docs/data-model.md`](data-model.md).

Contents: [1 Conventions](#1-conventions) · [2 Authentication](#2-authentication) ·
[3 Client behaviour](#3-client-behaviour) · [4 Versioning](#4-versioning) · [5 Route index](#5-route-index) ·
[6 Routes](#6-routes) · [7 Schemas](#7-schemas)

---

## 1. Conventions

### 1.1 Base URL

`https://<function-app>.azurewebsites.net/api/`, which is the Functions default route prefix. Every path
below is relative to `/api`. HTTPS only. There is no version segment in the path (see
[Versioning](#4-versioning)).

There is no CORS: the only clients are the native app and the owner's scripts. A web client (Later)
would add it then.

### 1.2 Casing and JSON types

- **Keys are camelCase** (`createdAt`, `briefSize`, `hiddenByFilters`).
- **Codes and enumerated values are lower snake_case**: error codes, reason codes, statuses, kinds and
  slots (`invite_required`, `follow_tag`, `awaiting_identity_removal`, `must_know`, `big_stories`). The
  one exception is the content tier, which keeps its letters `"A"`, `"B"` and `"C"`.
  *Why two cases:* keys are read by a decoder, while codes are read by `switch` statements and humans
  grep logs for them. One case per job keeps both jobs mechanical.
- **Timestamps** are RFC 3339 UTC strings ending in `Z`, at seconds precision on output
  (`"2026-10-21T05:04:00Z"`). Input timestamps may carry any offset and fractional seconds; the server
  normalises them to UTC and truncates to whole seconds. **Dates** are `"YYYY-MM-DD"` (UTC dates).
- **Nullable fields are always present.** A response never omits a documented field; it sends `null`.
  Optional *request* fields may be omitted, and omitting one means "leave it unchanged".
- **Unknown response fields are ignored** by clients, and an unknown enum value maps to an `unknown`
  case. That is what lets the server add fields and values without a version bump.
- **Request bodies are strict.** An unknown field is a `400 validation_failed` with reason
  `unknown_field`, so a typo in a client fails loudly instead of being silently dropped. Unlisted query
  parameters are ignored.
- **Storage is snake_case and is never returned as-is.** Each response is built by an explicit mapping
  function, so internal fields (`pk`, `_etag`, `ttl`, vectors, other users' ids) cannot leak into a
  response by accident. The one exception is the raw `documents` section of the data export, which is
  the user's own data on purpose.

### 1.3 Error envelope

Every error from this app has the same body:

```json
{"error": {"code": "invite_required", "message": "An invite code is required to use MyNews.", "details": {}}}
```

- `code` comes from the registry below. **Clients route on `code`**, never on the status alone and
  never on `message`.
- `message` is English text for logs and developers. It is never shown to users, and it never
  contains exception text, tokens, oids, email addresses or invite codes.
- `details` is always an object (`{}` when there is nothing to add). Its keys per code are in the
  table below.
- Schema: [`Error.schema.json`](api/schemas/Error.schema.json).

### 1.4 Status codes and the error-code registry

`backend/core/errors.py` holds this table as `ERROR_STATUS`, and `test_http_contract.py` asserts that
every raised error uses a registered code with exactly this status.

| Status | `code` | When | `details` |
|---|---|---|---|
| 400 | `validation_failed` | A query parameter or body field fails validation | `{"fields": [{"field", "reason"}]}` or `{"reason"}` for whole-body problems |
| 400 | `invite_malformed` | The invite code does not normalise to 12 Crockford symbols | `{}` |
| 401 | `unauthenticated` | No `Authorization: Bearer` header | `{}` |
| 401 | `invalid_token` | Any other token problem: signature, `iss`, `aud`, `exp`, `nbf`, `ver`, `tid`, `azp`, `scp`, unknown `kid`, wrong `alg` | `{"reason": "expired"}` only for an expired token, else `{}` |
| 403 | `invite_required` | Valid token, but no membership yet | `{}` |
| 403 | `account_blocked` | The membership is blocked by an admin | `{}` |
| 403 | `admin_required` | The route needs the `admin` app role | `{}` |
| 403 | `consent_required` | The route sends the user's own text to a model, and AI consent is not granted (P3) | `{"consentVersion": "YYYY-MM"}`, the minimum version accepted |
| 404 | `not_found` | The addressed resource does not exist, or a path id fails its grammar | `{"resource": "story" \| "edition" \| "section" \| "tag" \| "source" \| "saved" \| "suggestion" \| "invite" \| "user" \| "deletion" \| "report"}` |
| 404 | `invite_not_found` | Unknown or revoked invite code | `{}` |
| 409 | `invite_used` | The invite has no uses left | `{}` |
| 409 | `invite_expired` | The invite is past its expiry | `{}` |
| 409 | `duplicate` | The thing already exists elsewhere, for example a suggested feed that is already in the catalog | `{"resource", "id"}` |
| 409 | `limit_reached` | A total cap (not a rate) is reached, for example 500 saved stories | `{"resource", "limit"}` |
| 409 | `state_conflict` | The resource is in a state that does not allow the action (admin routes) | `{"status"}`, the current state |
| 410 | `account_deleted` | The account is being deleted or was deleted | `{"state": "deleting" \| "deleted"}` |
| 413 | `payload_too_large` | Body over 64 KB | `{"limitBytes": 65536}` |
| 415 | `unsupported_media_type` | A body without `Content-Type: application/json` | `{"expected": "application/json"}` |
| 429 | `quota_exceeded` | A per-account quota is used up | `{"quota", "limit", "period": "day" \| "week", "resetsAt"}` |
| 500 | `internal` | An unexpected server error (logged with the request id) | `{}` |
| 503 | `auth_unavailable` | Auth cannot be evaluated: settings are missing or the signing keys cannot be fetched. **Fails closed** | `{"reason": "config" \| "jwks"}` |
| 503 | `degraded` | A dependency is down or out of budget (database, queue, pool, model) | `{"dependency": "database" \| "queue" \| "pool" \| "model", "reason"?}` |

Rules that hold across the whole API:

- **410 is only `account_deleted`.** It is the one response that makes a client wipe its local data
  (§3). Nothing else may use 410. That is why an expired invite is a 409, and a story that has aged out
  is a 404.
- **Validation reasons** (`details.fields[].reason` and `details.reason`) come from one list:
  `required`, `type`, `enum`, `format`, `range`, `length`, `unknown_field`, `duplicate`, `self`,
  `invalid_json`, `not_object`, `empty`. `field` is the wire path in dot notation, for example
  `settings.briefSize` or `events[3].at`.
- **Path ids that fail their grammar are a `404 not_found`, answered before any I/O.** An id in a path
  is either a known resource or it is not. Query and body fields that fail validation are a 400.
- **Paths and methods not in this document** are answered by the Functions host with its own empty
  404, without the envelope. Clients treat a 404 without an envelope as `not_found`.
- **412 never reaches a client.** Optimistic concurrency is internal: the server re-reads and retries
  once, then answers `503 degraded` with `details: {"dependency": "database", "reason": "contention"}`
  and `Retry-After: 1`.
- Every 429 and every 503 carries `Retry-After`.

### 1.5 Ids

| Id | Grammar | Example | Notes |
|---|---|---|---|
| `storyId` | `^s\d{8}[A-Za-z0-9_-]{12}$` | `s20261021Ab3dE_gH1jKl` | `s` + the story's first-seen UTC date + the first 12 characters of its first article's id. Self-locating: the date is the story's partition. Migrated v1 saves use the v1 publish date and v1 id (`legacy: true`) |
| `editionId` | `^\d{4}-\d{2}-\d{2}\.(morning\|midday\|evening)$` (and a real date) | `2026-10-21.morning` | The UTC date of the slot. Slots run at 05:05, 10:05 and 16:05 UTC, which is the same calendar date in Switzerland all year |
| `inviteId` | `^[0-9a-f]{32}$` | `5c1e9a7b3d2f4e6a8c0b1d3f5a7c9e1b` | `sha256("mynews-invite:" + normalised code)`, first 32 hex characters. Shown to admins only. The code itself is never stored |
| `uid` | `^[0-9a-f]{32}$` | `3f9a1c0b7d2e4f6a8b1c3d5e7f9a0b2c` | `HMAC-SHA256(MYNEWS_HMAC_KEY, lower(oid))`, first 32 hex characters. The pseudonym used by admin routes, audit and telemetry |
| `oid` | lower-case GUID | `4f0d6c1e-8a3b-4c2d-9e7f-1a2b3c4d5e6f` | Entra object id. Returned only in the caller's own `Me` and in the admin's pending identity-removal list. Never logged and never accepted from a client |
| `deletion id` | = the account's `uid` | | `/admin/deletions/{id}` |
| `eventId`, `reportId` | UUID (any version), case-insensitive, stored lower-case | `0b7e3c1a-5d2f-4e8b-9a6c-1f3e5d7b9a2c` | Client-generated idempotency keys |
| `suggestionId` | `^[0-9a-f]{32}$` | | `sha256("mynews-sugg:" + normalised URL)`, first 32 hex (P3) |
| `sectionId`, `tagId`, `sourceId` | `^[a-z0-9]+(-[a-z0-9]+)*$`, at most 40 characters | `technology`, `eu-ai-act`, `heise-developer` | Slugs from the vocabulary and the catalog |

The nine `sectionId`s: `switzerland`, `world`, `business`, `technology`, `ai`, `software`, `security`,
`science`, `climate`.

### 1.6 Headers

**Request**

| Header | Routes | Meaning |
|---|---|---|
| `Authorization: Bearer <access token>` | all except `GET /health` | §2.1 |
| `Content-Type: application/json` | routes with a body | `charset`, if given, must be `utf-8` |
| `If-None-Match` | `GET /brief`, `GET /topics`, `GET /catalog` | §1.11 |
| `Accept-Language` | redeem and admin bootstrap | Prefills `locale` and `languages` when a membership is created. Ignored afterwards: `me.locale` decides |
| `X-Request-Id` | any | Optional. Echoed when it matches `^[A-Za-z0-9._:-]{8,64}$`; otherwise the server issues its own |
| `X-MyNews-Client: ios/<version> (<build>)` | any | Informational, for logs and `minBuild` decisions. Never enforced |

**Response**

| Header | When |
|---|---|
| `X-Request-Id` | Every response from this app, including errors, 304s and `/health` |
| `Retry-After: <seconds>` | Every 429 and 503, as an integer number of seconds (never an HTTP date) |
| `ETag` | 200 and 304 from `GET /brief`, `GET /topics`, `GET /catalog` |
| `Cache-Control` | `no-store` everywhere, except `private, no-cache` on the three ETag routes |
| `WWW-Authenticate` | Every 401: `Bearer realm="mynews"`, plus `, error="invalid_token"` for `invalid_token` |
| `Content-Type` | `application/json; charset=utf-8` on every body |
| `Content-Disposition` | `GET /me/export`: `attachment; filename="mynews-export-YYYY-MM-DD.json"` |

### 1.7 Request bodies

- At most **65,536 bytes**, by `Content-Length` or by the bytes actually read: otherwise
  `413 payload_too_large`.
- `Content-Type` must be `application/json`: otherwise `415 unsupported_media_type`.
- The body must be one UTF-8 JSON object: otherwise `400 validation_failed` with reason `invalid_json`
  or `not_object`.
- Routes that take no body ignore any body sent.
- Per-route item caps, all enforced as `400 validation_failed` (`length`/`range`):

| Body | Cap |
|---|---|
| `POST /me/feedback` `events` | 1–50 |
| `PATCH /me/interests` `changes` | 1–50 |
| `PATCH /me/sources` `changes` | 1–100 |
| `PUT /me/statements` | ≤ 10 statements of 1–200 characters |
| `POST /me/describe` `text` | 1–1,000 characters |
| `POST /me/reports` `note` | ≤ 500 characters |
| `POST /me/suggestions` `url` | ≤ 2,048 characters |
| `POST /stories/search` `query` | 2–64 characters |
| `POST /admin/invites` | `label` 1–40, `maxUses` 1–10, `expiresInDays` 1–90, `count` 1–20 |
| `POST /admin/users/{uid}/block` `reason` | ≤ 200 characters |

### 1.8 Finite lists and cursors

Every list has a documented maximum, so no response grows with the archive.

- A list that can exceed one page takes `cursor` and returns `nextCursor` (`null` at the end). Cursors
  are opaque strings of at most 512 characters. Clients pass them back verbatim and never parse them.
  A malformed or expired cursor is a `400 validation_failed` (`fields: [{"field": "cursor", "reason":
  "format"}]`), and the client restarts without one.
- A list that always fits in one response says `truncated: true` when its cap cut it short.

| Route | Page | Maximum | Order |
|---|---|---|---|
| `GET /brief/more` | — | 30 | Rank |
| `GET /briefs` | — | 21 (7 days × 3) | Newest first |
| `GET /stories` | 30 | 100 within 48 h | Rank (topic pages) or newest first (source pages) |
| `POST /stories/search` | — | 20 stories, 10 sections/tags, 10 sources | Relevance |
| `GET /me/saved` | 50 | 500 | Newest save first |
| `GET /me/history` | 50 | 200 | Newest open first |
| `GET /me/suggestions` | — | 50 | Newest first |
| `GET /admin/invites` | — | 200 | Newest first |
| `GET /admin/users` | — | 500 (`truncated`) | Newest first |
| `GET /admin/deletions` | — | 200 | Newest request first |
| `GET /admin/audit` | 100 | 1,000 | Newest first |
| `GET /admin/runs` | — | 50 | Newest first |
| `GET /admin/suggestions`, `GET /admin/reports` | — | 200 | Newest first |

### 1.9 Idempotency

- **GET is safe.**
- **State writes are idempotent:** `PATCH /me`, `PATCH /me/interests`, `PATCH /me/sources`,
  `PUT /me/statements`, `PUT` and `DELETE /me/saved/{id}`. They carry the desired state, never a delta,
  and mute and pause expiries are absolute timestamps, so a replay lands on the same state.
- **Event writes carry client ids:** each `POST /me/feedback` event and each `POST /me/reports` has a
  UUID. A replay answers `duplicate` per event, or `200` for a report.
- **`POST /me/redeem` is idempotent per account.** A caller who is already a member gets `200` with
  `Me` and consumes nothing. A retry after a timeout continues where the first attempt stopped (§2.3).
- **`DELETE /me` repeated** answers `202` again while the deletion runs, then `410`.
- **`POST /me/suggestions`** with a URL the same user already suggested answers `200` with the existing
  suggestion.
- **Admin:** revoke and complete are idempotent, and blocking into the current state answers `200`.
  `POST /admin/invites` is **not** idempotent: each call mints new codes.

### 1.10 Quotas

One table, in `backend/core/quotas.py`. Counters live in the caller's own partition
(`user|{oid}/quota|{yyyy-mm-dd}` and `quota|w{yyyy}-W{ww}`) and are taken with a conditional increment,
so concurrent requests cannot overshoot.

| Quota | Limit | Period | Route | What counts |
|---|---|---|---|---|
| `redeem` | 10 | day | `POST /me/redeem` | Each well-formed code that reaches the invite lookup (P1) |
| `export` | 2 | day | `GET /me/export` | Each export (P1) |
| `feedback` | 2,000 | day | `POST /me/feedback` | Each *new* event (duplicates are free) (P3) |
| `describe` | 5 | day | `POST /me/describe` | Each call (P3) |
| `reports` | 20 | day | `POST /me/reports` | Each new report (P3) |
| `suggestions` | 3 | ISO week | `POST /me/suggestions` | Each new suggestion (P3) |

- Days reset at 00:00 UTC, weeks at Monday 00:00 UTC.
- Exceeding a quota answers `429 quota_exceeded` with `Retry-After` = seconds until the reset, and
  `details` = `{"quota": "redeem", "limit": 10, "period": "day", "resetsAt": "2026-10-22T00:00:00Z"}`.
- **Caps are not quotas.** At most 500 saved stories is a total, so going over is a
  `409 limit_reached`, which waiting does not fix.
- **Model spend is global, not per user.** When the day's or the month's model reservation is used up,
  model-backed routes (`describe`, `statements`) answer `503 degraded` with `dependency: "model"`.

### 1.11 Caching (ETag)

`GET /brief`, `GET /topics` and `GET /catalog` send a strong `ETag`: `"` + the first 32 hex characters
of `sha256(response body)` + `"`. A request whose `If-None-Match` lists that value (a `W/` prefix and
`*` are accepted) gets `304 Not Modified` with no body, plus `ETag`, `Cache-Control` and
`X-Request-Id`. The tag covers the whole body, including per-user state such as read marks, so it
changes whenever anything the user would see changes. All other responses are `Cache-Control: no-store`.

---

## 2. Authentication

### 2.1 Tokens

The identity provider is the Microsoft Entra External ID tenant **"von Mentlen News"** (country
Switzerland; sign-in host `vonmentlennews.ciamlogin.com`). Clients use MSAL with the authority
`https://vonmentlennews.ciamlogin.com/` and request the scope `api://<ENTRA_API_CLIENT_ID>/access_as_user`.
The app registration `mynews-api` sets `api.requestedAccessTokenVersion = 2`, which the exact `iss`
and `aud` checks below depend on.

`backend/core/auth.py` checks, in this order, and **never falls open**:

0. **Configuration.** `ENTRA_TENANT_ID`, `ENTRA_API_CLIENT_ID`, `ENTRA_ALLOWED_CLIENT_IDS`,
   `ENTRA_REQUIRED_SCOPE`, `ENTRA_ADMIN_ROLE` and `MYNEWS_HMAC_KEY` must be present, must not be an
   unresolved `@Microsoft.KeyVault(` reference, and the ids must be GUIDs. Otherwise every non-public
   route answers `503 auth_unavailable` with `reason: "config"` and `Retry-After: 60`, and the missing
   setting *names* (never values) go to the log. Until the tenant exists these settings are empty, so
   the API is closed by construction.
1. **Header.** Exactly one `Authorization` header with the `Bearer` scheme, at most 8 KB, three
   dot-separated segments. Missing: `401 unauthenticated`. Malformed: `401 invalid_token`. Tokens in
   query strings or cookies are ignored.
2. **JOSE header.** `alg` must be `RS256` and `kid` must be present: otherwise `401 invalid_token`.
   The algorithm is fixed by the server, never taken from the token.
3. **Signing key.** Looked up by `kid` in the JWKS at
   `https://{tid}.ciamlogin.com/{tid}/discovery/v2.0/keys` through one process-wide `PyJWKClient`
   (keys cached in memory for 12 h, never persisted; an unknown `kid` triggers one rate-limited
   refetch). A fetch failure answers `503 auth_unavailable` with `reason: "jwks"` and `Retry-After: 5`,
   and logs `ALERT jwks_fetch_failed`. A `kid` that is still unknown is a `401 invalid_token`.
4. **Signature and claims** (PyJWT 2.15.1, `leeway` 60 s):
   - `iss` equals `https://{ENTRA_TENANT_ID}.ciamlogin.com/{ENTRA_TENANT_ID}/v2.0` exactly;
   - `aud` equals `ENTRA_API_CLIENT_ID` (strict, a single audience);
   - `exp`, `nbf` and `iat` are required and checked;
   - `ver` equals `"2.0"`, and `tid` equals `ENTRA_TENANT_ID`;
   - `oid` is present and is a GUID;
   - `azp` is in `ENTRA_ALLOWED_CLIENT_IDS` (the iOS and CLI client ids);
   - `scp`, split on spaces, contains `ENTRA_REQUIRED_SCOPE` (`access_as_user`). App-only tokens have
     no `scp` and fail here.

   Any failure is a `401 invalid_token`. `details.reason` is `"expired"` only for an expired token, so
   a client can tell a routine refresh from a real fault, and nothing else is revealed.
5. **Principal.** `oid` (lower-cased), `uid` (§1.5), `roles` (the token's `roles` claim, sorted, exactly
   as issued) and `admin` (`ENTRA_ADMIN_ROLE` in `roles`).

The service never reads `sub`, `name`, `email` or `preferred_username`: they are mutable or
app-specific, and the device already shows the email from its MSAL account. The token is never
logged. Logs carry the `uid` and, on failure, the exception class name only.

### 2.2 Access levels

Each handler carries exactly one level, set by a decorator in `core/auth.py`.
`test_route_inventory.py` asserts the level of every registered route, and that only `health` is
public.

| Level | Requires | Routes |
|---|---|---|
| `public` | Nothing | `GET /health` |
| `token` | A valid token | `POST /me/redeem` |
| `member` | A valid token and a membership | Everything under `/me` except redeem, and all content routes |
| `admin` | A member whose token carries the `admin` role | Everything under `/admin` |

**The member gate** costs one point read of `user|{oid}/me` per request, and the handler reuses that
document:

| `me` document | Token | Result |
|---|---|---|
| exists, `active` | — | Proceed |
| exists, `blocked` | — | `403 account_blocked`, except `GET /me/export` and `DELETE /me`, which a blocked member may still use |
| exists, `deleting` | — | `410 account_deleted` (`state: "deleting"`), except `DELETE /me`, which answers `202` again |
| missing, and a tombstone exists | — | `410 account_deleted` (`state: "deleted"`) |
| missing, no tombstone | `admin` role | Bootstrap a membership (§2.4), then proceed |
| missing, no tombstone | no `admin` role | `403 invite_required` |

**The admin level** checks the role first, with no I/O, so a non-admin gets `403 admin_required`
without a database read. Then it runs the member gate, so an admin's first admin call bootstraps the
membership too.

**Role changes** take effect with the next access token (Entra stamps `roles` at issue time, so up to
60–90 minutes). Blocking takes effect at once, because it is server state.

### 2.3 The invite gate

A valid token without a membership gets `403 invite_required` everywhere except `POST /me/redeem`.

**Codes.** 12 symbols from Crockford's Base32 alphabet `0123456789ABCDEFGHJKMNPQRSTVWXYZ`, which is
60 random bits from `secrets`, shown as `XXXX-XXXX-XXXX`. The server **normalises** input before
anything else: at most 64 characters are accepted; spaces, tabs, line breaks, non-breaking spaces and
hyphens or dashes (`-`, U+2010–U+2015, U+2212) are removed; letters are upper-cased; `O` becomes `0`,
and `I` and `L` become `1`. Anything that is then not exactly 12 alphabet symbols is a
`400 invite_malformed`. The invite's id is `sha256("mynews-invite:" + code)[:32]`, so the database
never holds a usable code. The plaintext appears once, in the `POST /admin/invites` response.

**Redemption** (`core/invites.redeem`) is safe to retry after a failure at any step:

1. Normalise the code (`400 invite_malformed`).
2. Read `me`. If it exists: `active` answers `200` with `Me`, `blocked` answers
   `403 account_blocked`, and `deleting` answers `410`. If it is missing and a tombstone exists:
   `410 account_deleted`. If the caller has the `admin` role: bootstrap instead (§2.4) and answer `201`.
3. Take one `redeem` quota unit (`429 quota_exceeded` after 10 a day).
4. **Conditional patch** of `sys/invite|{inviteId}`: increment `uses` and append `{uid, at}` to
   `redemptions`, only if the invite is `active`, `uses < max_uses`, `expires_at` is in the future,
   **and this uid is not in `redemptions` yet**.
   - The invite does not exist: `404 invite_not_found`.
   - The predicate failed: re-read the invite. If this uid is already in `redemptions`, an earlier
     attempt got this far, so **continue**. Otherwise: revoked answers `404 invite_not_found`
     (revocation is not revealed), expired answers `409 invite_expired`, and anything else answers
     `409 invite_used`.
5. Upsert the registry document `users/{oid}`, then create `user|{oid}/me`. A conflict on the create
   means a concurrent request won, and its document is used. The registry is written first, so a
   member never lacks one.
6. Write the audit entry `invite.redeemed`, and answer `201` with `Me`.

Only redemption (or the admin bootstrap) creates `user|{oid}/me` and `users/{oid}`.

**Brute force** is bounded by 60-bit codes, 10 attempts per account per day, and a 30-day default
expiry. Failed attempts are logged with the `uid`, never the code.

**App Review** gets a multi-use code (`maxUses` 5, 60 days) and a pre-redeemed demo account without the
`admin` role.

### 2.4 Admin bootstrap

A token with the `admin` role and no membership gets one on its first `member` or `admin` call, or on
redeem, without an invite: the registry document, then `me` with `invite_id: "bootstrap-admin"`, then
the audit entry `member.bootstrapped`. Concurrent first calls are safe: one create wins and the others
read its document. A tombstoned account is **not** bootstrapped (it gets `410`), so a stale device
cannot resurrect an account its owner just deleted.

### 2.5 Isolation

No route accepts an `oid` or a partition key from a client. Member routes read and write only the
caller's partition `user|{oid}`, derived from the validated token. Admin routes address accounts by
`uid`. Ids that belong to someone else (a saved story, a suggestion, a feedback event) are resolved
inside the caller's own partition, so they answer `404 not_found` or a per-item `not_found`, never the
other account's data. Story ids are global, but they only ever lead to shared content.
`test_isolation.py` runs every route as account A with account B's ids.

---

## 3. Client behaviour

**Route on `error.code`; fall back to the status class for an unknown code.** Never show `message`.

| Response | Client action |
|---|---|
| 401 (any code) | One silent token refresh with `forceRefresh`, then retry once. If that fails, show the re-auth banner and keep the cache. Never wipe |
| 403 `invite_required` | Invite-code screen (with a Paste button) |
| 403 `account_blocked` | Blocked screen. Export and delete stay available |
| 403 `admin_required` | Hide the admin area and refetch `/me` |
| 403 `consent_required` | Consent sheet, then retry |
| 404 | The item is gone. The outbox drops it |
| 409 `invite_used` / `invite_expired` | Specific copy: "This code has already been used." / "This code has expired. Ask for a new one." |
| 409 (other) | Specific copy where one exists. The outbox drops the item |
| **410 `account_deleted`** | **Wipe**: sign out of MSAL, delete the account's local directory, show Welcome with "Your MyNews account was deleted." |
| 400, 413, 415 | A client bug: log it with the `X-Request-Id`. The outbox drops the item |
| 429 | Honour `Retry-After`, keep showing cached content, show the quota copy |
| 500, 503, timeouts, offline | Keep cached content with an inline "Couldn't update" line. Honour `Retry-After` when present, else back off |

**Only `410 account_deleted` wipes local data.** No other status or code, including a 401 or a 404 on
`/me`, may delete anything on the device. The user's own actions are separate: signing out, and
signing out after `DELETE /me` answers `202`, clear the account directory as part of the sign-out.

**The outbox** (signals and settings written while offline or in the background):

- **Only unsent items coalesce.** Before sending: `save` then `unsave` of the same story sends
  nothing; a `more` followed by its own undo sends nothing; for change lists, the newest unsent change
  per `(type, id)` wins. An item already sent is never rewritten or "replaced".
- **Retracting a sent event is an explicit undo:** a new event with `signal: "undo"` and
  `targetEventId` set to the sent event's id.
- **Event ids are client UUIDs**, one per event. Resending the same id is always safe.
- **Per-item results decide each item's fate:** `accepted` and `duplicate` delete it; `rejected`
  deletes it as permanent and logs the `code`. One bad item never blocks the others.
- **Request-level outcomes:** 2xx applies per-item results; 401 stops the flush and refreshes the
  token; 403 `invite_required` or `account_blocked` stops and routes the app; 429 stops until
  `Retry-After`; 5xx, 503 and transport errors stop with backoff of 30 s × 2^attempts, capped at 6 h,
  ±20 % jitter; 400, 404 and 409 drop the item.
- One flush in flight at a time, oldest first, at most 50 events per request. Items older than 7 days
  are dropped locally (the server would reject them as `stale`).
- **Sign-out** flushes the outbox first, warns about anything still unsent, then wipes the account
  directory and removes the MSAL account.

**Refresh:** on launch, on foreground, at `nextExpectedAt` plus jitter, and when the network returns,
throttled to once per 10 minutes. `If-None-Match` is sent on the three ETag routes.

---

## 4. Versioning

- `GET /health` reports `api: 1`. Within API 1 every change is **additive**: new routes, new response
  fields, new enum values and new error codes. Clients ignore unknown fields, map unknown values to
  `unknown`, and fall back on the status class for unknown codes.
- A breaking change gets a **new route name**. The old route stays until `minBuild` has passed every
  build that used it. There is no `/v2` prefix.
- **`minBuild`** (`MIN_SUPPORTED_BUILD`, default `0`) is the oldest iOS build (`CFBundleVersion`) the
  backend supports. It appears in `GET /health` and in `Me`. A build below it shows "Update MyNews in
  TestFlight" and makes no further calls. The server never enforces it: a build check on the server
  would turn a typo in an app setting into an outage.
- Request bodies are validated strictly, so the server deploys before any client that sends a new
  field.

---

## 5. Route index

Function names are unique across the app; `test_route_inventory.py` compares this table with the
indexed app for every registered (P1) row. Every HTTP route uses `auth_level=ANONYMOUS` at the host,
because authentication is the decorator's job.

| Method | Path | Level | Phase | Function | Request → Response schema |
|---|---|---|---|---|---|
| GET | `/health` | public | P0 (kept) | `health` | — → `Health` |
| POST | `/me/redeem` | token | **P1** | `me_redeem` | `Redeem` → `Me` |
| GET | `/me` | member | **P1** | `me_get` | — → `Me` |
| PATCH | `/me` | member | **P1** | `me_patch` | `MePatch` → `Me` |
| DELETE | `/me` | member (blocked, deleting allowed) | **P1** | `me_delete` | — → `DeleteAccepted` |
| GET | `/me/export` | member (blocked allowed) | **P1** | `me_export` | — → `Export` |
| GET | `/admin/overview` | admin | **P1** | `admin_overview` | — → `AdminOverview` |
| GET | `/admin/health` | admin | **P1** | `admin_health` | — → `AdminHealth` |
| GET | `/admin/invites` | admin | **P1** | `admin_invites_list` | — → `InviteList` |
| POST | `/admin/invites` | admin | **P1** | `admin_invites_create` | `InviteCreateRequest` → `InviteCreated` |
| POST | `/admin/invites/{id}/revoke` | admin | **P1** | `admin_invite_revoke` | — → `Invite` |
| GET | `/admin/users` | admin | **P1** | `admin_users_list` | — → `AdminUserList` |
| POST | `/admin/users/{uid}/block` | admin | **P1** | `admin_user_block` | `AdminBlockRequest` → `AdminUser` |
| GET | `/admin/deletions` | admin | **P1** | `admin_deletions_list` | — → `AdminDeletionList` |
| POST | `/admin/deletions/{id}/complete` | admin | **P1** | `admin_deletion_complete` | — → `AdminDeletion` |
| GET | `/admin/catalog` | admin | P2 | `admin_catalog_list` | — → `AdminSourceList` |
| PATCH | `/admin/catalog/{id}` | admin | P2 | `admin_catalog_patch` | `AdminSourcePatch` → `AdminSource` |
| GET | `/admin/runs` | admin | P2 | `admin_runs_list` | — → `RunList` |
| POST | `/admin/runs/harvest` | admin | P2 | `admin_runs_harvest` | `RunRequest` → `RunAccepted` |
| POST | `/admin/runs/cycle` | admin | P2 | `admin_runs_cycle` | — → `RunAccepted` |
| GET | `/brief` | member | P3 | `brief_get` | — → `Edition` |
| GET | `/brief/more` | member | P3 | `brief_more` | — → `BriefMore` |
| GET | `/briefs` | member | P3 | `briefs_list` | — → `BriefList` |
| GET | `/stories` | member | P3 | `stories_list` | — → `StoryList` |
| POST | `/stories/search` | member | P3 | `stories_search` | `SearchRequest` → `SearchResult` |
| GET | `/stories/{id}` | member | P3 | `story_get` | — → `StoryDetail` |
| GET | `/topics` | member | P3 | `topics_get` | — → `Topics` |
| GET | `/catalog` | member | P3 | `catalog_get` | — → `Catalog` |
| GET | `/catalog/{id}` | member | P3 | `catalog_source_get` | — → `CatalogSourceDetail` |
| PATCH | `/me/interests` | member | P3 | `me_interests_patch` | `InterestsPatch` → `InterestsResult` |
| PATCH | `/me/sources` | member | P3 | `me_sources_patch` | `SourcesPatch` → `SourcesResult` |
| PUT | `/me/statements` | member | P3 | `me_statements_put` | `StatementsPut` → `Statements` |
| POST | `/me/describe` | member | P3 | `me_describe` | `DescribeRequest` → `DescribeResult` |
| DELETE | `/me/model` | member | P3 | `me_model_delete` | — → 204 |
| POST | `/me/feedback` | member | P3 | `me_feedback` | `FeedbackRequest` → `FeedbackResult` |
| GET | `/me/saved` | member | P3 | `me_saved_list` | — → `SavedList` |
| PUT | `/me/saved/{id}` | member | P3 | `me_saved_put` | — → `SavedState` |
| DELETE | `/me/saved/{id}` | member | P3 | `me_saved_delete` | — → 204 |
| GET | `/me/history` | member | P3 | `me_history_list` | — → `HistoryList` |
| DELETE | `/me/history` | member | P3 | `me_history_delete` | — → 202 `{}` |
| POST | `/me/reports` | member | P3 | `me_reports_create` | `ReportRequest` → `ReportResult` |
| POST | `/me/suggestions` | member | P3 | `me_suggestions_create` | `SuggestionRequest` → `Suggestion` |
| GET | `/me/suggestions` | member | P3 | `me_suggestions_list` | — → `SuggestionList` |
| GET | `/me/suggestions/{id}` | member | P3 | `me_suggestion_get` | — → `Suggestion` |
| GET | `/admin/suggestions` | admin | P3 | `admin_suggestions_list` | — → `AdminSuggestionList` |
| POST | `/admin/suggestions/{id}/decision` | admin | P3 | `admin_suggestion_decision` | `AdminSuggestionDecision` → `AdminSuggestion` |
| GET | `/admin/reports` | admin | P3 | `admin_reports_list` | — → `AdminReportList` |
| POST | `/admin/reports/{id}/resolve` | admin | P3 | `admin_report_resolve` | `AdminReportResolve` → `AdminReport` |
| GET | `/admin/audit` | admin | P3 | `admin_audit_list` | — → `AuditList` |

Blueprints: `api/public.py` (health), `api/account.py` (the `/me` P1 routes), `api/admin.py` (every
admin route), and from Phase 3 `api/content.py` and `api/me_state.py`.

Non-HTTP functions: `harvest_timer` (moved verbatim) and `maintenance_timer` (daily 02:15 UTC) in
`jobs/timers.py`; `account_purge_worker` (queue `account-purge`) and `ops_jobs_worker` (queue
`ops-jobs`) in `jobs/queues.py`. Phase 2 adds `cycle_timer`; Phase 3 adds `suggestions_worker`.

---

## 6. Routes

Every route below can also answer `401 unauthenticated` / `invalid_token`,
`503 auth_unavailable`, `503 degraded` and `500 internal` (public `/health` aside). The per-route
error lists name only the route's own codes.

### 6.1 Public

#### `GET /health` — public · P0 (unchanged)

Static liveness and the contract version. **No I/O**: no settings object, no Cosmos, no network, so it
has nothing to fail on and nothing to echo. Dependency health is the admin route's job.

```json
{"status": "ok", "api": 1, "minBuild": 0, "time": "2026-10-21T10:00:00Z"}
```

`minBuild` is `MIN_SUPPORTED_BUILD`; an unset or non-integer value serves `0` with a logged warning.
Schema: `Health`.

### 6.2 Account (Phase 1)

#### `POST /me/redeem` — token · P1

Redeems an invite code and creates the caller's membership (§2.3).

```json
{"code": "K7QM-2HXA-9RTV"}
```

| Status | Body | When |
|---|---|---|
| 201 | `Me` | The membership was created now (by the code, or by the admin bootstrap) |
| 200 | `Me` | The caller already was an active member. No code or quota is consumed |

Errors: `400 invite_malformed` · `400 validation_failed` (no `code` string) · `404 invite_not_found` ·
`409 invite_used` · `409 invite_expired` · `403 account_blocked` · `410 account_deleted` ·
`429 quota_exceeded` (`redeem`).
Quota: `redeem`, 10 a day. Idempotency: §1.9. `Accept-Language` prefills `locale` and `languages`.

#### `GET /me` — member · P1

The caller's account and settings. The app calls it on launch to decide its phase.

```json
{
  "oid": "4f0d6c1e-8a3b-4c2d-9e7f-1a2b3c4d5e6f",
  "uid": "3f9a1c0b7d2e4f6a8b1c3d5e7f9a0b2c",
  "roles": ["admin"],
  "createdAt": "2026-10-21T10:00:00Z",
  "locale": "de-CH",
  "languages": ["de", "en"],
  "timeZone": "Europe/Zurich",
  "settings": {
    "briefSize": 12,
    "editions": ["morning", "midday", "evening"],
    "somethingDifferent": true,
    "emphasis": "balanced",
    "openInReader": false,
    "summaryInList": true,
    "personalisedRanking": true,
    "historyRetentionDays": 90
  },
  "consent": {"aiProcessing": {"granted": false, "version": null}},
  "onboarding": {"completedAt": null},
  "minBuild": 0
}
```

| Field | Values | Notes |
|---|---|---|
| `oid`, `uid` | §1.5 | The caller's own ids. `uid` is what the owner sees in the admin users list, so support can find an account without anyone reading an email address |
| `roles` | array of strings | **Copied from the validated token**, sorted. `["admin"]` gates the admin area. `[]` for everyone else |
| `locale` | `"en"` · `"de-CH"` | The language of server-provided labels and summaries |
| `languages` | non-empty subset of `"de"`, `"en"` | Content languages: starter packs, topic pages, candidates |
| `timeZone` | IANA name, at most 64 characters | Default `Europe/Zurich` |
| `settings.briefSize` | `10` · `12` · `15` | Short / Standard / Long. Default 12. The brief is never padded |
| `settings.editions` | non-empty subset of `"morning"`, `"midday"`, `"evening"` | Default all three |
| `settings.somethingDifferent` | bool | Default `true` |
| `settings.emphasis` | `"interests"` · `"balanced"` · `"big_stories"` | "What matters more?" Default `"balanced"` |
| `settings.openInReader` | bool | Default `false` |
| `settings.summaryInList` | bool | The one-line teaser or summary in lists. Default `true` |
| `settings.personalisedRanking` | bool | `false` ranks by importance and freshness only; mutes still apply. Default `true` |
| `settings.historyRetentionDays` | `30` · `90` · `180` | How long opens and dwell signals are kept. Default 90 |
| `consent.aiProcessing` | `{"granted": bool, "version": "YYYY-MM" \| null}` | `version: null` until the user has answered. The client compares `version` with its own consent text's version as a string |
| `onboarding.completedAt` | timestamp or `null` | `null` sends the app through onboarding |
| `minBuild` | integer | §4 |

Every settings key is always present: keys added later read as their defaults on older accounts.
Errors: `403 invite_required` · `403 account_blocked` · `410 account_deleted`. Not ETag-cached:
`Cache-Control: no-store`. Schema: `Me`.

#### `PATCH /me` — member · P1

Changes any subset of the whitelisted fields and answers `200` with the full `Me`.

```json
{
  "timeZone": "Europe/Zurich",
  "languages": ["de"],
  "settings": {"briefSize": 15, "editions": ["morning", "evening"]},
  "consent": {"aiProcessing": {"granted": true, "version": "2026-10"}},
  "onboarding": {"completed": true}
}
```

- Whitelist: `locale`, `languages`, `timeZone`, `settings.*` (any subset of the keys above),
  `consent.aiProcessing` (both `granted` and `version` required), and `onboarding.completed`.
  Anything else, including `oid`, `uid` and `roles`, is `400 validation_failed` (`unknown_field`). An
  empty body or an empty `settings` object is `400` (`empty`).
- `onboarding.completed: true` stamps `completedAt` once, and repeating it keeps the first time.
  `false` clears it, which sends the app through onboarding again.
- The server stamps the consent time (stored, exported, not echoed).
- `locale` and `languages` are mirrored to the registry document, which the cycle reads to decide
  which summary languages are in use.

Errors: `400 validation_failed` · `403 account_blocked` · `410 account_deleted`. Idempotent. Schema:
`MePatch` → `Me`.

#### `DELETE /me` — member, blocked and deleting allowed · P1

Starts account deletion and answers at once; the data is purged in the background.

```json
{"state": "deleting", "identityRemoval": "manual"}
```

1. `me.status` and `users/{oid}.status` become `deleting` (with `deletion_requested_at`). From this
   moment every other route answers `410 account_deleted`.
2. One message goes to the `account-purge` queue (queue output binding, connection `JOBS_STORAGE`),
   and the audit entry `account.deletion_requested` is written.
3. `202 Accepted`. A repeat answers `202` again without a second message: the daily maintenance timer
   re-enqueues any deletion stuck for more than an hour.

`identityRemoval` is `"manual"` when `IDENTITY_DELETE_MODE=manual` (the owner removes the Entra user
within 7 days) and `"automatic"` in `graph` mode (Later). On `202` the app signs out, which like
every sign-out clears the account's local directory, and its confirmation copy says what is still in
flight: "Your MyNews data is being deleted now. Your sign-in will be removed within 7 days. If you
used Sign in with Apple, you can also remove MyNews under Settings › Apple Account › Sign in with
Apple." The purge order is in [data-model.md](data-model.md#8-deletion-and-purge-order). After the
purge, `GET /me` answers `410 account_deleted`.

Errors: `403 invite_required` (never a member) · `410 account_deleted` (already purged). Schema:
`DeleteAccepted`.

#### `GET /me/export` — member, blocked allowed · P1

Everything stored for the caller, as a downloadable JSON file
(`Content-Disposition: attachment; filename="mynews-export-2026-10-21.json"`).

```json
{
  "schema": "mynews-export/1",
  "exportedAt": "2026-10-21T10:00:00Z",
  "account": {"oid": "…", "uid": "…", "roles": [], "createdAt": "…", "locale": "de-CH", "languages": ["de"], "timeZone": "Europe/Zurich", "settings": {"…": "…"}, "consent": {"aiProcessing": {"granted": true, "version": "2026-10"}}, "onboarding": {"completedAt": "…"}, "minBuild": 0},
  "documents": [
    {"type": "me", "id": "me", "status": "active", "created_at": "…", "consent": {"ai_processing": {"granted": true, "version": "2026-10", "at": "…"}}},
    {"type": "user", "id": "4f0d6c1e-…", "uid": "…", "status": "active", "last_active_at": "…"}
  ]
}
```

- `account` is exactly the `Me` shape.
- `documents` is every document in the caller's partition plus the registry document, **as stored**
  (snake_case, field meanings in [data-model.md](data-model.md)), minus Cosmos system properties
  (`_rid`, `_self`, `_etag`, `_attachments`, `_ts`) and `pk`, and minus the quota counters. Phase 3
  documents (saves, signals, editions, suggestions) appear here automatically.
- Residual data that is not in the export (and outlives a deletion): audit entries and logs keyed by
  `uid`, the tombstone, and Cosmos backups for 7 days. The privacy page discloses them.

Errors: `429 quota_exceeded` (`export`) · `410 account_deleted`. Quota: `export`, 2 a day. Schema:
`Export`.

### 6.3 Content (Phase 3, draft)

Shared shapes (`StoryCard.schema.json`):

- **`StoryCard`**: `id`, `title` (the publisher's headline, verbatim), `url`,
  `publisher {id, name}`, `publishedAt`, `lang`, `section {id, label}`, `tags [{id, label}]` (≤ 5),
  `contentType` (`report`, `analysis`, `opinion`, `explainer`, `review`, `official`, `rumour`,
  `satire`, `live`, `sponsored`), `teaser` (the publisher's own feed description, at most the source's
  `teaser_max`, never machine-translated; `null` when there is none), `summaryFirst` (the first AI
  bullet in the user's `locale`, Tier A only, else `null`), `summaryStatus` (`ready`, `pending`,
  `not_allowed`, `unavailable`), `byline`, `outletsCount`, `paywalled`, `satire`, `isUpdate`,
  `reasons [Reason]` and `my {read, saved, reaction}`.
- **`Reason`**: `{code, target, count}`, where `target` is `{type: "section" | "tag" | "source", id,
  label}` or `null`, and `count` is an integer or `null`. Codes: `follow_section`, `follow_tag`,
  `subscribed_source`, `similar_to_likes`, `coverage` (`count` = outlets), `must_know`,
  `something_different`, `update` (`count` = new outlets), `not_in_your_sources` (the card offers
  "Add"). Labels arrive in the user's `locale`, and the client fills its own localised sentence
  templates ("You follow %@"). The plan's `followTopic` is `follow_section` here.
- **AI text is attached at response time.** Editions store cards without AI text; the API adds
  summaries from the pool or the `{storyId}.{lang}` summary documents and skips withdrawn ones. A tier
  downgrade therefore takes effect on the next request.

#### `GET /brief?edition=current|{editionId}` — member · P3

The finite brief: at most `briefSize` stories in total, made of at most 2 *Must know*, then *For you*,
and at most 1 *Something different*. It is never padded. It is built lazily on the first request and
never waits on a model. `current` is the newest published slot among the user's `editions`. (The card
arrays are elided in this example.)

```json
{
  "edition": {"id": "2026-10-21.morning", "slot": "morning", "date": "2026-10-21", "publishedAt": "2026-10-21T05:21:00Z", "builtAt": "2026-10-21T05:24:10Z", "nextExpectedAt": "2026-10-21T10:31:00Z", "size": 12, "readingMinutes": 8, "readCount": 4},
  "sections": [
    {"kind": "must_know", "stories": [], "hiddenByFilters": 1},
    {"kind": "for_you", "stories": [], "hiddenByFilters": 0},
    {"kind": "something_different", "stories": [], "hiddenByFilters": 0}
  ],
  "moreTodayCount": 14
}
```

`sections` always lists the three kinds in this order (possibly with empty `stories`).
`nextExpectedAt` is the next slot time plus the measured p90 cycle duration, which drives "Next brief
around 12:05". ETag: yes. Errors: `400 validation_failed` (`edition`) · `404 not_found` (`edition`:
older than 7 days or never published) · `503 degraded` (`dependency: "pool"`, `reason:
"pool_not_ready"`, `Retry-After: 300`) before the first pool exists. Schema: `Edition`.

#### `GET /brief/more?edition=` — member · P3

"Show N more from today": at most 30 further cards for the same edition, never repeating the brief's
own. `{"editionId", "stories": [StoryCard], "hiddenByFilters"}`. Errors as `/brief`. Schema:
`BriefMore`.

#### `GET /briefs?days=7` — member · P3

Earlier briefs: `{"editions": [{"id", "slot", "date", "publishedAt", "size", "readCount"}]}`, newest
first, `days` 1–7 (default 7), at most 21. Editions live 8 days. Schema: `BriefList`.

#### `GET /stories?section=|tag=|source=&cursor=` — member · P3

Finite browsing over the live pool's last **48 h**: exactly one of `section`, `tag` or `source`
(otherwise `400 validation_failed`). Topic pages (`section`, `tag`) draw on every active source in the
user's `languages`, ranked. Source pages are chronological. Hard filters (mutes, removed and paused
sources) apply, and `hiddenByFilters` counts what they hid.

```json
{"filter": {"type": "tag", "id": "eu-ai-act", "label": "EU AI Act"}, "windowHours": 48, "order": "ranked", "stories": [], "hiddenByFilters": 0, "nextCursor": null}
```

Pages of 30, at most 100 in total. The cursor pins the pool run, so pages never shift. Errors:
`404 not_found` (`section`, `tag` or `source`). Schema: `StoryList`.

#### `POST /stories/search` — member · P3

The query travels in the body, so it never reaches request telemetry (URLs are logged; bodies are
not). `{"query": "UBS"}` (2–64 characters) →
`{"stories": [StoryCard] (≤ 20, live pool), "sections": [{id, label}], "tags": [{id, label}] (≤ 10 together), "sources": [{id, name}] (≤ 10)}`.
Schemas: `SearchRequest` → `SearchResult`.

#### `GET /stories/{id}` — member · P3

`StoryDetail`: `{"story": StoryCard, "summary": {"lang", "bullets" (3), "outletsUsed", "generatedAt"} | null,
"coverage": [{"publisher", "title", "url", "lang", "publishedAt", "paywalled"}] (≤ 30),
"entities": [string] (≤ 5), "reasons": [Reason + "control"]}`. Each reason pairs with its control
(`{"action": "unfollow" | "unmute" | "mute" | "pause_source" | "remove_source" | "add_source" |
"less_like_this", "target"}`) for the Why sheet. The AI summary is labelled on the client: "Summary by
AI from N outlets. May contain errors." with N = `outletsUsed`. Errors: `404 not_found` (`story`).
Schema: `StoryDetail`.

#### `GET /topics` — member · P3

The two-level vocabulary in the user's `locale`, with the user's state:
`{"vocabVersion", "sections": [{"id", "label", "description", "state": "follow" | "neutral" | "mute",
"mutedUntil", "tags": [{"id", "label", "state", "mutedUntil"}]}], "statements": [{"id", "text"}],
"learned": [{"tag": {id, label}, "strength": "often" | "sometimes"}]}`. ETag: yes. Schema: `Topics`.

#### `GET /catalog` — member · P3

Every active source with the user's state, plus starter packs:
`{"catalogVersion", "sources": [CatalogSource], "starterPacks": [{"section", "lang", "sourceIds"}]}`.
`CatalogSource` = `{id, name, homepage, lang, country, section {id, label}, tags [{id, label}], kind
(newsroom, wire, blog, official, rumour, satire), flags {paywallHeavy, satire, rumour, aiReserved},
aiSummaries, defaultOn, typicalPerDay, my {state: "subscribed" | "removed" | "paused" | "neutral",
pausedUntil}}`. **`flags.aiReserved: true` marks a feed whose publisher reserves AI use**: MyNews
sends only its headline to the classifier and never summarises it. That is the per-source flag the
owner asked for, shown as a label in the app. `aiSummaries` is true only for Tier A. ETag: yes.
Schema: `Catalog`.

#### `GET /catalog/{id}` — member · P3

`CatalogSource` plus `recentHeadlines: [{title, url, publishedAt}]` (≤ 10). Errors: `404 not_found`
(`source`). Schema: `CatalogSourceDetail`.

### 6.4 Me state (Phase 3, draft)

#### `PATCH /me/interests` — member · P3

An idempotent change list for follows and mutes of sections and tags:

```json
{"changes": [{"type": "tag", "id": "eu-ai-act", "state": "follow"}, {"type": "section", "id": "business", "state": "mute", "until": "2026-10-28T10:00:00Z"}]}
```

`state` is `follow`, `neutral` or `mute`. `until` applies to `mute` only: an absolute time (clamped to
between 1 hour and 366 days ahead) or `null`/absent for "always". 1–50 changes. → `200`
`{"results": [{"index", "status": "applied" | "unchanged" | "rejected", "code": null | "not_found" | "invalid"}], "follows": [{type, id, since}], "mutes": [{type, id, until}]}`.
A bad item is rejected on its own. Schemas: `InterestsPatch` → `InterestsResult`.

#### `PATCH /me/sources` — member · P3

The same pattern for sources: `{"changes": [{"id", "state": "subscribed" | "removed" | "paused" |
"neutral", "until"?}]}`, 1–100 changes; `paused` requires `until`. **`removed` is a hard filter;
`neutral` is not**: merely not subscribing never hides a source's stories from followed topics. Unknown
or retired sources are rejected per item (`not_found`). → `200`
`{"results", "sources": [{id, state, pausedUntil}]}`. Schemas: `SourcesPatch` → `SourcesResult`.

#### `PUT /me/statements` — member · P3

Replaces the user's own interest statements: `{"statements": [{"text"}]}` (≤ 10 × 200 characters) →
`200` `{"statements": [{"id", "text", "createdAt"}]}`. Only affirmative statements are embedded.
Errors: `403 consent_required` · `503 degraded` (`model`). Schemas: `StatementsPut` → `Statements`.

#### `POST /me/describe` — member · P3

"Describe what you want": one model call parses free text into proposals the user confirms.
**Nothing changes on the server.** `{"text"}` (1–1,000 characters) → `200`
`{"proposals": {"follow": [{type, id, label}], "mute": [{type, id, label}], "statements": [string]}, "unmatched": [string]}`.
Errors: `403 consent_required` · `429 quota_exceeded` (`describe`, 5 a day) · `503 degraded`
(`model`). Schemas: `DescribeRequest` → `DescribeResult`.

#### `DELETE /me/model` — member · P3

"Reset learning": clears the learned like and dislike prototypes. Follows, mutes, sources and
statements stay. → `204`. Idempotent.

#### `POST /me/feedback` — member · P3

Reading and learning signals, batched by the outbox:

```json
{"events": [
  {"id": "0b7e3c1a-5d2f-4e8b-9a6c-1f3e5d7b9a2c", "storyId": "s20261021Ab3dE_gH1jKl", "signal": "open", "at": "2026-10-21T06:01:12Z", "editionId": "2026-10-21.morning", "position": 3},
  {"id": "2c9d4e6f-7a8b-4c1d-8e2f-3a4b5c6d7e8f", "storyId": "s20261021Ab3dE_gH1jKl", "signal": "dwell", "at": "2026-10-21T06:01:40Z", "dwellMs": 28000},
  {"id": "5e1f2a3b-4c5d-4e6f-9a0b-1c2d3e4f5a6b", "storyId": "s20261021Ab3dE_gH1jKl", "signal": "undo", "at": "2026-10-21T06:02:00Z", "targetEventId": "9a8b7c6d-5e4f-4a3b-8c2d-1e0f9a8b7c6d"}
]}
```

- `signal`: `open`, `dwell` (requires `dwellMs`, 0–3,600,000; ≥ 20 s counts as a weak like),
  `more` (like), `less` (dislike, "Hidden · Undo"), `hide` (dislike) and `undo` (requires
  `targetEventId`, the id of an event **already sent**; its `storyId` must match the target's).
- `at` must lie within [now − 7 days, now + 5 minutes]; otherwise the event is rejected as `stale`.
- 1–50 events. `400` is only for an invalid envelope; every per-event problem is a per-item result.

→ `200` `{"results": [{"id", "status": "accepted" | "duplicate" | "rejected", "code": null |
"not_found" | "invalid" | "stale"}]}`, one per event, in request order. Existing ids answer
`duplicate`. New events are written in one transactional batch with the quota increment and one model
update, so a learning signal is never half-applied. Quota: `feedback`, 2,000 new events a day (the
whole request answers `429` when the batch would exceed it). Schemas: `FeedbackRequest` →
`FeedbackResult`.

#### `GET /me/saved?cursor=` — member · P3

`{"items": [{"storyId", "title", "url", "publisher": {id, name}, "publishedAt", "teaser", "savedAt", "legacy"}], "nextCursor"}`.
Saves are self-contained snapshots (title, url, publisher, date and the publisher's description, with
no AI text), so they outlive the 21-day story documents. Pages of 50, at most 500. Schema: `SavedList`.

#### `PUT /me/saved/{id}` and `DELETE /me/saved/{id}` — member · P3

`PUT` saves (a like signal too) → `200` `{"storyId", "saved": true, "savedAt"}`; saving again keeps the
first `savedAt`. `DELETE` → `204`, also when nothing was saved. Errors: `404 not_found` (`story`, for
an unknown story on `PUT`) · `409 limit_reached` (`saved`, 500). Schema: `SavedState`.

#### `GET /me/history?days=30&cursor=` — member · P3 · `DELETE /me/history` — member · P3

History comes from `open` signals: `{"items": [{"storyId", "title", "url", "publisher", "openedAt"}], "nextCursor"}`,
`days` 1–180 (capped by `historyRetentionDays`), pages of 50, at most 200. `DELETE` clears opens and
dwell signals in the background (`202 {}`); learned preferences stay. Schema: `HistoryList`.

#### `POST /me/reports` — member · P3

"Report a problem": `{"id": UUID, "storyId", "kind": "summary_wrong" | "summary_offensive" |
"satire_unlabelled" | "broken_link" | "duplicate" | "other", "note"?}` → `201` `{"id", "status":
"open"}`, or `200` for an id already received. **Reports never reach a model.** Per Open decision 5, a
free-text `note` requires AI consent (`403 consent_required`). Errors: `404 not_found` (`story`) ·
`429 quota_exceeded` (`reports`, 20 a day). Schemas: `ReportRequest` → `ReportResult`.

#### `POST /me/suggestions`, `GET /me/suggestions`, `GET /me/suggestions/{id}` — member · P3

"Suggest a source": `{"url"}` → `202` with a `Suggestion` in status `checking`; the app then polls
`GET /me/suggestions/{id}` every 2 s for up to 60 s. `200` when the same user already suggested the same
normalised URL. `Suggestion` = `{id, url, status, feed: {title, homepage, lang, sampleHeadlines (≤ 3)}
| null, sourceId, submittedAt, updatedAt}` with `status` ∈ `checking`, `pending_review` (found a feed;
sent for review), `already_in_catalog`, `not_a_feed`, `unreachable`, `blocked`, `failed`, `approved`,
`rejected`. Errors: `400 validation_failed` (`url`: not https, not a public host) ·
`409 duplicate` (`resource: "source"`, the feed is already in the catalog) · `429 quota_exceeded`
(`suggestions`, 3 a week) · `404 not_found` (`suggestion`). Lists: at most 50. Schemas:
`SuggestionRequest` → `Suggestion`, `SuggestionList`.

### 6.5 Admin

Admin routes need the `admin` role (§2.2). Every mutation writes an audit entry with the admin's
`uid`. No admin route returns an email address, and only the pending identity-removal list returns an
oid, because the owner needs it to find the account in Entra.

#### `GET /admin/overview` — admin · P1

One screen of counts. Phase 2 and 3 sections are `null` until their phase.

```json
{
  "generatedAt": "2026-10-21T10:00:00Z",
  "users": {"total": 6, "active7d": 5, "new30d": 6, "blocked": 0, "deleting": 0},
  "invites": {"active": 3, "used": 4, "expired": 1, "revoked": 0},
  "deletions": {"purging": 0, "awaitingIdentityRemoval": 1, "overdue": 0},
  "pipeline": {"lastHarvest": {"at": "2026-10-21T09:30:04Z", "ok": true}, "lastCycle": null},
  "secrets": {"appleSecretRenewBy": "2027-04-01", "appleSecretDaysLeft": 162},
  "spend": null,
  "suggestions": null,
  "reports": null
}
```

In Phase 1, `pipeline.lastHarvest` comes from the v1 harvest's run documents in the legacy container.
`invites` counts the newest 200 invites by status. Schema: `AdminOverview`.

#### `GET /admin/health` — admin · P1

Deep checks, each timed, each classified. **Exception text is never echoed**: a failed check reports
an `error` class (`timeout`, `unavailable`, `forbidden`, `not_found`, `throttled`, `misconfigured`,
`failed`). Always `200`; `status` is `degraded` when any check fails.

```json
{
  "status": "ok",
  "checkedAt": "2026-10-21T10:00:00Z",
  "api": 1,
  "minBuild": 0,
  "identityDeleteMode": "manual",
  "hmacKeyFingerprint": "a1b2c3d4e5f6",
  "checks": {
    "config": {"ok": true, "missing": [], "unresolvedKeyVaultRefs": []},
    "cosmos": {"ok": true, "ms": 18, "error": null},
    "legacyCosmos": {"ok": true, "ms": 25, "error": null},
    "jwks": {"ok": true, "ms": 2, "keys": 7, "error": null},
    "queues": {"ok": true, "ms": 41, "error": null, "poison": {"account-purge-poison": 0, "ops-jobs-poison": 0}, "oldestMessageMinutes": {"account-purge": null, "ops-jobs": null}},
    "harvest": {"ok": true, "lastRunAt": "2026-10-21T09:30:04Z", "lastRunOk": true, "ageMinutes": 30},
    "deletions": {"ok": true, "stuckPurging": 0, "overdueIdentityRemovals": 0},
    "secrets": {"ok": true, "appleSecretRenewBy": "2027-04-01", "daysLeft": 162}
  }
}
```

| Check | Fails when |
|---|---|
| `config` | Any required setting is missing (names only) or still an unresolved Key Vault reference |
| `cosmos` | A single-partition count on `users` errors or exceeds 5 s |
| `legacyCosmos` | Reading the last v1 harvest run from `items` fails (Phase 1 only) |
| `jwks` | The signing keys cannot be read (from cache or a fetch) |
| `queues` | A poison queue holds messages, or a main queue's oldest message is older than 10 minutes |
| `harvest` | No successful harvest in 3 h |
| `deletions` | A purge has been running for more than 1 h, or an identity removal is waiting for more than 7 days |
| `secrets` | `APPLE_SECRET_RENEW_BY` is within 30 days or past |

`hmacKeyFingerprint` is `sha256("mynews-hmac-fp:" + MYNEWS_HMAC_KEY)[:12]`, the same value
`postprovision.sh` checks, so a regenerated key is visible without revealing it. Phase 2 adds `pool`,
`budget` and `cycle`. Schema: `AdminHealth`.

#### `GET /admin/invites?status=` — admin · P1

`{"invites": [Invite]}`, newest first, at most 200. `status` is `active`, `used`, `expired`,
`revoked` or `all` (default). `Invite` = `{id, label, status, createdAt, createdBy (uid), expiresAt,
maxUses, uses, lastRedeemedAt}`, where `status` is derived: `revoked` if revoked, else `used` if
`uses ≥ maxUses`, else `expired` if past `expiresAt`, else `active`. Codes are never returned again.
Schema: `InviteList`.

#### `POST /admin/invites` — admin · P1

```json
{"label": "Family wave 1", "maxUses": 1, "expiresInDays": 30, "count": 3}
```

→ `201` `{"invites": [{"id", "code": "K7QM-2HXA-9RTV", "label", "expiresAt", "maxUses"}]}`.
`maxUses` defaults to 1 and `expiresInDays` to 30; `count` 1–20 (default 1); `label` is required.
**This response is the only place a plaintext code ever appears**, and it is never logged. One audit
entry `invite.created` per invite. Not idempotent. Schemas: `InviteCreateRequest` → `InviteCreated`.

#### `POST /admin/invites/{id}/revoke` — admin · P1

→ `200` `Invite` with `status: "revoked"`. Revoking again answers `200` unchanged. A revoked code
redeems as `404 invite_not_found`. Errors: `404 not_found` (`invite`). Schema: `Invite`.

#### `GET /admin/users` — admin · P1

`{"users": [AdminUser], "truncated": false}`, newest first, at most 500. `AdminUser` =
`{uid, status: "active" | "blocked" | "deleting", admin, createdAt, lastActiveAt, inviteLabel, locale,
languages, blockedAt}`. `admin` is the role last seen on the user's token. No oid, no email. Schema:
`AdminUserList`.

#### `POST /admin/users/{uid}/block` — admin · P1

`{"blocked": true, "reason": "…"}` blocks; `{"blocked": false}` unblocks. Writes `me` first (that is
what the gate reads), then the registry, then the audit entry `user.blocked` or `user.unblocked` with
the reason. → `200` `AdminUser`. Blocking into the current state answers `200`. Errors:
`404 not_found` (`user`) · `400 validation_failed` (`reason: "self"`: admins cannot block themselves)
· `409 state_conflict` (`status: "deleting"`). Schemas: `AdminBlockRequest` → `AdminUser`.

#### `GET /admin/deletions?status=` — admin · P1

The "pending identity removals" list. `status` is `awaiting_identity_removal` (default), `purging`,
`completed` or `all`. → `{"deletions": [AdminDeletion]}`, newest request first, at most 200.
`AdminDeletion` = `{id (uid), status, identityMode, oid, requestedAt, purgedAt, identityRemovedAt,
docsDeleted, overdue}`. `oid` is present until the identity is removed, then `null`. `overdue` means
waiting for more than 7 days after the purge. Schema: `AdminDeletionList`.

**Manual identity removal (owner runbook):** Entra admin center › Users › find the `oid` › Delete,
then Deleted users › Delete permanently, then `POST /admin/deletions/{id}/complete`.

#### `POST /admin/deletions/{id}/complete` — admin · P1

Records that the owner removed the Entra user. Allowed only in `awaiting_identity_removal`: it sets
`identityRemovedAt`, erases the stored `oid`, starts the 30-day expiry of the tombstone, writes the
audit entry `identity.removed`, and answers `200` `AdminDeletion` with `status: "completed"`. Again on a
completed deletion: `200` unchanged. Errors: `404 not_found` (`deletion`) · `409 state_conflict`
(`status: "purging"`: wait for the purge). Schema: `AdminDeletion`.

#### `GET /admin/catalog`, `PATCH /admin/catalog/{id}` — admin · P2

The full catalog with tiers, AI policy, legal evidence and health:
`AdminSource` = `{id, name, feedUrl, homepage, lang, country, section, tags, kind, family, authority,
tier: "A" | "B" | "C" | null, aiPolicy {embed, classify, fulltext, summarize, translate}, legal
{basis, evidenceUrl, checkedAt, note}, flags {paywallHeavy, satire, rumour, aiReserved}, status:
"pending" | "active" | "paused" | "retired", defaultOn, teaserMax, subscriberCount, health
{lastFetchAt, lastSuccessAt, consecutiveFailures, itemsLast24h}}`. `legal.note` is the free-text remark
recording why a feed may not be used with AI. `PATCH` takes any subset of `tier`, `aiPolicy`, `legal`,
`flags`, `status`, `defaultOn`, `teaserMax`. **A downgrade** of `tier` or of `aiPolicy.summarize` or
`translate` marks the source's summaries withdrawn and republishes the pool (through `ops-jobs`).
Errors: `404 not_found` (`source`) · `400 validation_failed`. Schemas: `AdminSourceList`,
`AdminSourcePatch` → `AdminSource`.

#### `GET /admin/runs?kind=&limit=20`, `POST /admin/runs/harvest`, `POST /admin/runs/cycle` — admin · P2

`GET` → `{"runs": [{"id", "kind", "startedAt", "finishedAt", "ok", "error" (≤ 300 characters, an error
class and a short note, never a stack trace), "counters": {}, "estimatedUsd", "ruCharge"}]}` (≤ 50).
`POST …/harvest` `{"sourceId"?, "force": true}` and `POST …/cycle` enqueue an `ops-jobs` message and
answer `202` `{"jobId"}`. Errors: `404 not_found` (`source`). Schemas: `RunList`, `RunRequest`,
`RunAccepted`.

#### `GET /admin/suggestions?status=pending_review`, `POST /admin/suggestions/{id}/decision` — admin · P3

`AdminSuggestion` = `Suggestion` minus the per-user fields, plus `probe {https, robots: "allowed" |
"disallowed" | "unknown", contentSignal, tdmReserved, itemsLast7d}`, `submitCount`, `decidedAt` and
`decisionNote`. The decision body is
`{"decision": "approve", "tier", "section", "tags", "flags", "aiPolicy", "legal", "name"?, "defaultOn"}`
or `{"decision": "reject", "note"}`. Approval creates the catalog source as `pending` →
`200` `AdminSuggestion` with `sourceId`. Errors: `409 state_conflict` (already decided) ·
`409 duplicate` (`source`). Schemas: `AdminSuggestionList`, `AdminSuggestionDecision` →
`AdminSuggestion`.

#### `GET /admin/reports?status=open`, `POST /admin/reports/{id}/resolve` — admin · P3

`AdminReport` = `{id, storyId, kind, note, status: "open" | "resolved", resolution, createdAt,
resolvedAt, reporter (uid, or "deleted" after the reporter's account is purged)}`. Resolve body:
`{"resolution": "fixed" | "no_action" | "source_flagged", "note"?}`, idempotent. Schemas:
`AdminReportList`, `AdminReportResolve` → `AdminReport`.

#### `GET /admin/audit?cursor=` — admin · P3

`{"entries": [{"id", "at", "actor" (uid or "system"), "action", "target": {"type", "id"}, "details"}],
"nextCursor"}`, newest first, pages of 100, at most 1,000. Schema: `AuditList`.

---

## 7. Schemas

All schemas are JSON Schema **draft 2020-12**, one file per body under
[`docs/api/schemas/`](api/schemas/), with `$id`
`https://tvonment.github.io/my-news/api/schemas/<Name>.schema.json`. Shared definitions (ids,
timestamps, enums, `Ref`, `Reason`) are in `Common.schema.json` and referenced relatively. Response
schemas set `additionalProperties: false`, so a fixture with an undocumented field fails CI; clients
still ignore unknown fields (§1.2). Phase 2 and 3 schemas carry `"x-phase"` and the word *Draft* in
their description: they are valid schemas, but their shapes may still change before their phase
ships. Phase 1 schemas are final.

| Schema | Phase | Used by |
|---|---|---|
| `Common` | P1 | Shared `$defs` |
| `Error` | P1 | Every error |
| `Health` | P0 | `GET /health` |
| `Me` | P1 | `GET /me`, `PATCH /me`, `POST /me/redeem`, `Export.account` |
| `MePatch` | P1 | `PATCH /me` request |
| `Redeem` | P1 | `POST /me/redeem` request |
| `DeleteAccepted` | P1 | `DELETE /me` |
| `Export` | P1 | `GET /me/export` |
| `Invite`, `InviteList`, `InviteCreateRequest`, `InviteCreated` | P1 | Admin invites |
| `AdminUser`, `AdminUserList`, `AdminBlockRequest` | P1 | Admin users |
| `AdminDeletion`, `AdminDeletionList` | P1 | Admin deletions |
| `AdminOverview`, `AdminHealth` | P1 | Admin overview and health |
| `StoryCard`, `Edition`, `BriefMore`, `BriefList`, `StoryList`, `StoryDetail`, `SearchRequest`, `SearchResult` | P3 | Content |
| `Topics`, `Catalog`, `CatalogSourceDetail` | P3 | Topics and catalog |
| `InterestsPatch`, `InterestsResult`, `SourcesPatch`, `SourcesResult`, `StatementsPut`, `Statements`, `DescribeRequest`, `DescribeResult` | P3 | Me state |
| `FeedbackRequest`, `FeedbackResult`, `SavedList`, `SavedState`, `HistoryList`, `ReportRequest`, `ReportResult` | P3 | Signals |
| `SuggestionRequest`, `Suggestion`, `SuggestionList` | P3 | Suggestions |
| `AdminSource`, `AdminSourceList`, `AdminSourcePatch`, `RunList`, `RunRequest`, `RunAccepted` | P2 | Admin catalog and runs |
| `AdminSuggestionList`, `AdminSuggestion`, `AdminSuggestionDecision`, `AdminReportList`, `AdminReport`, `AdminReportResolve`, `AuditList` | P3 | Admin moderation and audit |
