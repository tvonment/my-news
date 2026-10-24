# MyNews data model (v2)

> **Status.** The storage side of [`docs/api.md`](api.md), written with it in Phase 1, step 1. It lists
> every document type by partition: its fields, its TTL and who writes it. It also lists the index
> paths each query needs, the RU budget, and the deletion order.
>
> Phase 1 documents are **final**: `users`, `me`, quota counters, invites, deletions, tombstones and
> audit entries. Phase 2 and 3 documents are a **draft** that the pipeline and selection work may still
> refine. Every field name a query touches is a constant in `backend/core/models.py`, and the index
> policy below is `infra/modules/cosmos-index-policy.json`, copied verbatim.

Contents: [1 Stores](#1-stores) · [2 Conventions](#2-conventions) · [3 Partition map](#3-partition-map) ·
[4 Phase 1 documents](#4-phase-1-documents-final) · [5 Phase 2 and 3 documents](#5-phase-2-and-3-documents-draft) ·
[6 Index policy and query inventory](#6-index-policy-and-query-inventory) · [7 RU notes](#7-ru-notes) ·
[8 Deletion and purge order](#8-deletion-and-purge-order) · [9 Queue messages](#9-queue-messages) ·
[10 The legacy container](#10-the-legacy-container-items)

---

## 1. Stores

| Store | What | Phase |
|---|---|---|
| Cosmos container **`news`** | All v2 data. `pk` `/pk` (hash v2), `defaultTtl` −1, **600 RU/s**, opt-in indexing from creation | P1 |
| Cosmos container **`items`** | v1's container, read and written only by the v1 harvest until Phase 2 deletes it. Lowered to **400 RU/s** | until P5 |
| Blob container `pool` (private) | `pool/runs/{runId}-{n}.npz` (n = 1 publish, 2 republish) and `pool/current.npz`. A lifecycle rule deletes `runs/` after 10 days | P2 |
| Storage queues | `account-purge`, `suggestions`, `ops-jobs`, each with `-poison`, on the Function App's storage account, connection `JOBS_STORAGE` | P1 |

The two containers add up to the free tier's 1,000 RU/s. The account is provisioned, not serverless,
because only a provisioned account can carry the free tier. After the migration and a 14-day soak,
`items` is deleted by hand and `news` is raised to 1,000 RU/s (Phase 5).

`COSMOS_CONTAINER` (default `news`) is what `core.cosmos.get_container()` returns, so all new code
uses it. `LEGACY_COSMOS_CONTAINER` (default `items`) is passed explicitly, and only by
`pipeline/harvest.py`, `core/sources.py` and the Phase 1 admin reads of v1 harvest runs.

---

## 2. Conventions

- **One container, type-discriminated.** Every document has `id`, `pk`, `type` and an integer `v`
  (its schema version, so a later migration can find old shapes).
- **Field names are snake_case.** The API maps them to camelCase explicitly (api.md §1.2).
- **Ids use only `[A-Za-z0-9-_.|]`.** Cosmos recommends alphanumeric ids, because `/`, `\`, `?` and
  `#` break SDK paths.
- **Timestamps are strings from `core.models.iso()`**: UTC, seconds precision, `Z`
  (`2026-10-21T05:04:00Z`). Because the format is fixed, comparing the strings compares the times,
  which is what every range filter (`c.last_active_at >= @since`) relies on. A timestamp written any
  other way (fractional seconds, `+00:00`) silently breaks those filters.
- **`ttl`** is an integer number of seconds. `-1` means never, and leaving it out inherits the
  container default (−1). It is never `null`, which Cosmos rejects. **TTL counts from the last write
  (`_ts`)**, so a document that must expire at a fixed time recomputes `ttl` on every write (invites,
  for example).
- **Leave a key out rather than writing `null`** whenever a query tests it with `IS_DEFINED`.
  `IS_DEFINED` is true for an explicit `null`, which is the v1 lesson recorded in `core/models.py`.
- **All writes go through `core/cosmos.py`**, which gives typed errors (`NotFound`, `AlreadyExists`,
  `PreconditionFailed`, `Unavailable`), RU accounting from `x-ms-request-charge`, pacing, and the
  `LOCAL_READONLY` guard: a local run against production cannot write unless the call is inside
  `allow_prod_writes(reason)`, which scripts open only with `--allow-prod-write`.
- **Concurrency.** Whole-document updates use `replace_if_match` on `_etag`. Counters and guarded
  transitions use a conditional patch (`filter_predicate`, at most 10 operations; `incr` creates a
  missing field). The API never surfaces a 412.
- **Point reads before queries, and no cross-partition queries in `news`.** Every query passes a
  `partition_key`. `enable_scan_in_query` is never used.

---

## 3. Partition map

| `pk` | Phase | Documents | What bounds it |
|---|---|---|---|
| `users` | P1 | One registry document per member, id = `oid` | The number of members |
| `user\|{oid}` | P1 | `me`, quota counters. P3: `subs`, `model`, `ed\|…`, `fb\|…`, `save\|…`, `sugg\|…` | Caps and TTLs: a heavy reader stays around 3,000 documents |
| `sys` | P1 | `invite\|…`, `deletion\|…`, `tombstone\|…`, `audit\|…`. P2: `budget\|…`, `pool`, `lease\|…`, `run\|…`, `coverage`, `migration\|v1-v2` | TTLs (audit 365 d, runs 90 d) |
| `catalog` | P2 | `src\|…`, `vocab\|…`. P3: `sugg\|…`, `report\|…` | ~100 sources |
| `src` | P2 | `state\|{source}`, `ring\|{source}` | Two per source |
| `article\|{yyyy-mm-dd}` | P2 | Articles by first-seen UTC day | ~450 a day, TTL 21 d |
| `story\|{yyyy-mm-dd}` | P2 | Stories and their summaries by first-seen UTC day | TTL 21 d |

Every logical partition stays far below Cosmos's 20 GB cap. The content partitions are bucketed by
day because a single unbounded partition can never be split later without a migration.

---

## 4. Phase 1 documents (final)

### 4.1 `users` / `{oid}`: the registry

One per member: what the admin lists, the maintenance timer and (from Phase 2) the cycle query,
without fanning out over every user partition.

| Field | Type | Notes |
|---|---|---|
| `id` | string | The `oid`, a lower-case GUID |
| `pk` | `"users"` | |
| `type` | `"user"` | |
| `v` | `1` | |
| `uid` | string, 32 hex | `HMAC-SHA256(MYNEWS_HMAC_KEY, oid)[:32]`. Indexed: admin routes address users by it |
| `status` | `"active"` · `"blocked"` · `"deleting"` | Mirrors `me.status`. Indexed |
| `is_admin` | bool | The role last seen on the user's token, refreshed at most daily |
| `created_at` | timestamp | Indexed (composite with `type`) |
| `invite_id` | string | The redeemed `inviteId`, or `"bootstrap-admin"` |
| `invite_label` | string or null | Copied from the invite, so the admin list needs no join |
| `locale` | `"en"` · `"de-CH"` | Mirrors `me.locale`. From Phase 2 the cycle reads it to know which summary languages are in use |
| `languages` | array of `"de"` · `"en"` | Mirrors `me.languages` |
| `last_active_at` | timestamp | Updated at most once per UTC day. Indexed |
| `blocked_at`, `blocked_by` | timestamp, uid | Present only while blocked |
| `deletion_requested_at` | timestamp | Present only while deleting. Indexed |
| `updated_at` | timestamp | |

TTL: none. **Writers:** redeem and admin bootstrap (upsert, *before* `me` is created, so a member never
lacks a registry document); the member gate's daily touch (patch `last_active_at`, `is_admin`);
`PATCH /me` (patch `locale`, `languages`); block and unblock (patch); `DELETE /me` (patch
`status`, `deletion_requested_at`); the purge worker (delete, **last**).

### 4.2 `user|{oid}` / `me`: the membership

Its existence *is* the membership: the member gate is one point read of it per request (~1 RU), and
the handler reuses the document.

| Field | Type | Notes |
|---|---|---|
| `id` | `"me"` | |
| `pk` | `"user\|{oid}"` | The oid lives only in the partition key, never in a field |
| `type` | `"me"` | |
| `v` | `1` | |
| `status` | `"active"` · `"blocked"` · `"deleting"` | What the gate decides on |
| `created_at`, `updated_at` | timestamp | |
| `invite_id` | string | As in the registry |
| `locale` | `"en"` · `"de-CH"` | Prefilled from `Accept-Language` at creation |
| `languages` | array, non-empty subset of `"de"`, `"en"` | Prefilled from `Accept-Language` |
| `time_zone` | IANA name | Default `Europe/Zurich` |
| `settings.brief_size` | `10` · `12` · `15` | Default 12 |
| `settings.editions` | array of `"morning"` · `"midday"` · `"evening"` | Default all three |
| `settings.something_different` | bool | Default `true` |
| `settings.emphasis` | `"interests"` · `"balanced"` · `"big_stories"` | Default `"balanced"` |
| `settings.open_in_reader` | bool | Default `false` |
| `settings.summary_in_list` | bool | Default `true` |
| `settings.personalised_ranking` | bool | Default `true` |
| `settings.history_retention_days` | `30` · `90` · `180` | Default 90. Sets the TTL of new `open` and `dwell` signals |
| `consent.ai_processing` | `{granted: bool, version: "YYYY-MM" \| null, at: timestamp \| null}` | `at` is stamped by the server and exported, not echoed |
| `onboarding.completed_at` | timestamp or null | |
| `last_active_at` | timestamp | The gate's daily-touch marker |
| `is_admin` | bool | The role last seen, refreshed daily |
| `blocked_at` | timestamp | Present only while blocked |
| `deletion_requested_at` | timestamp | Present only while deleting |

Settings keys missing from an older document read as their defaults. TTL: none. No email address
is ever stored. **Writers:** redeem and bootstrap (create; a 409 means a concurrent request won); the
gate's touch (patch, at most once per UTC day, together with the registry); `PATCH /me`
(`replace_if_match`, re-read and re-apply once on a 412); block and unblock (patch); `DELETE /me`
(patch); the purge worker (delete, after the tombstone is written).

### 4.3 `user|{oid}` / `quota|{yyyy-mm-dd}` and `quota|w{yyyy}-W{ww}`

| Field | Type | Notes |
|---|---|---|
| `id` | `"quota\|2026-10-21"` or `"quota\|w2026-W43"` | UTC day; ISO year and week |
| `pk` | `"user\|{oid}"` | |
| `type` | `"quota"` | |
| `v` | `1` | Written by the create path only |
| `redeem`, `export`, `feedback`, `describe`, `reports` | int | Daily counters, one field per quota key, created by the first `incr` |
| `suggestions` | int | The only weekly counter |
| `ttl` | 172,800 (2 days) or 691,200 (8 days) | |

**Writer:** `core.quotas.consume(oid, key, n)` only: a patch `[{op: incr, path: /{key}, value: n}]`
with the predicate `FROM c WHERE NOT IS_DEFINED(c.{key}) OR c.{key} <= {limit − n}`. A 404 creates the
document with the counter at `n` (a 409 race retries the patch once), and a 412 becomes
`429 quota_exceeded`. Keys come from the quota table, never from input, so they can be inlined into
the predicate safely. Quota documents are left out of the export.

### 4.4 `sys` / `invite|{inviteId}`

| Field | Type | Notes |
|---|---|---|
| `id` | `"invite\|{inviteId}"` | `inviteId` = `sha256("mynews-invite:" + normalised code)[:32]`. The code itself is never stored |
| `pk` | `"sys"` | |
| `type` | `"invite"` | |
| `v` | `1` | |
| `label` | string, 1–40 | |
| `created_at` | timestamp | Indexed |
| `created_by` | uid | |
| `expires_at` | timestamp | |
| `max_uses` | int, 1–10 | |
| `uses` | int | |
| `status` | `"active"` · `"revoked"` | Stored. `used` and `expired` are derived when reading |
| `redemptions` | array of `{uid, at}`, at most `max_uses` | `uid` becomes `"deleted"` when that account is purged |
| `last_redeemed_at` | timestamp or null | |
| `revoked_at`, `revoked_by` | timestamp, uid | Present once revoked |
| `ttl` | int | Seconds from this write until `expires_at` + 90 days, recomputed on every write |

**Writers:** mint (create; an id collision, which 60-bit codes make vanishingly rare, retries with a
new code); redeem (conditional patch: `incr /uses`, `add /redemptions/-`, `set /last_redeemed_at`,
`set /ttl`, with the predicate `FROM c WHERE c.status = 'active' AND c.uses < c.max_uses AND
c.expires_at > '{now}' AND NOT ARRAY_CONTAINS(c.redemptions, {"uid": "{uid}"}, true)`, where `now` and
`uid` are server-generated and safe to inline); revoke (patch); the purge worker (patch the
redemption's `uid` to `"deleted"`).

### 4.5 `sys` / `deletion|{uid}`

One per account deletion. It is the purge's resume record and the admin's "pending identity removals"
list.

| Field | Type | Notes |
|---|---|---|
| `id` | `"deletion\|{uid}"` | |
| `pk` | `"sys"` | |
| `type` | `"deletion"` | |
| `v` | `1` | |
| `uid` | uid | |
| `oid` | GUID | Needed to remove the Entra user by hand. **Removed** once the identity is removed |
| `status` | `"purging"` · `"awaiting_identity_removal"` · `"completed"` | Indexed |
| `identity_mode` | `"manual"` · `"graph"` | `IDENTITY_DELETE_MODE` at the time |
| `created_at` | timestamp | The deletion request time (`me.deletion_requested_at`). Indexed |
| `purge_started_at`, `purged_at` | timestamp | |
| `identity_removed_at`, `identity_removed_by` | timestamp, uid | Set by `POST /admin/deletions/{id}/complete` |
| `decremented_source_ids` | array of sourceId | Subscriber counts already decremented, so a resumed purge never counts twice (P3) |
| `anonymised` | `{invite: bool, reports: bool}` | Resume guard |
| `docs_deleted` | int | |
| `attempts` | int | Worker runs |
| `last_error` | string or null | The exception class name only |
| `updated_at` | timestamp | |
| `ttl` | int | −1 until `completed`, then 90 days |

**Writers:** the purge worker (create on its first run, then patches); the complete route
(`replace_if_match`); the maintenance timer reads it.

### 4.6 `sys` / `tombstone|{uid}`

| Field | Type | Notes |
|---|---|---|
| `id` | `"tombstone\|{uid}"` | |
| `pk` | `"sys"` | |
| `type` | `"tombstone"` | |
| `v` | `1` | |
| `uid` | uid | No oid, no other data |
| `deleted_at` | timestamp | |
| `identity_removed` | bool | |
| `ttl` | int | −1 while the Entra user may still sign in; 30 days once the identity is removed |

**Writers:** the purge worker (upsert, *before* `me` is deleted); the complete route (patch). **Readers:**
the member gate and redeem, only when `me` is missing (1 RU), to answer `410` instead of
`403 invite_required`. While the identity still exists, the tombstone stops the same oid from
redeeming a new invite into a "fresh" account that the owner believes is deleted.

### 4.7 `sys` / `audit|{yyyymmddTHHMMSSZ}|{8 hex}`

| Field | Type | Notes |
|---|---|---|
| `id` | `"audit\|20261021T100000Z\|1a2b3c4d"` | Time plus a random suffix: sortable and collision-free |
| `pk` | `"sys"` | |
| `type` | `"audit"` | |
| `v` | `1` | |
| `created_at` | timestamp | Indexed |
| `actor` | uid or `"system"` | |
| `action` | string | See below |
| `target` | `{type, id}` | `type` ∈ `user`, `invite`, `deletion`, `source`, `suggestion`, `report`, `run`. `id` is a uid, inviteId, sourceId and so on |
| `details` | object | Small. **Never** an oid, an email address, an invite code, a token or a user's free text |
| `request_id` | string or null | The `X-Request-Id` that caused it |
| `ttl` | 31,536,000 (365 days) | |

Phase 1 actions: `member.bootstrapped`, `invite.created`, `invite.revoked`, `invite.redeemed`,
`user.blocked`, `user.unblocked`, `account.deletion_requested`, `account.purged`, `identity.removed`.
Phase 2 and 3 add `catalog.updated`, `run.requested`, `suggestion.approved`, `suggestion.rejected` and
`report.resolved`. **Writer:** `core.audit.record()`, a best-effort create: a failure is logged and
never fails the request it describes.

---

## 5. Phase 2 and 3 documents (draft)

### 5.1 `catalog`

| Id | Type | Fields | TTL | Writers |
|---|---|---|---|---|
| `src\|{sourceId}` | `source` | `name`, `feed_url`, `homepage`, `lang`, `country`, `section`, `tags`, `kind` (newsroom, wire, blog, official, rumour, satire), `family`, `authority` (0–1), `teaser_mode` (`description` · `none`), `teaser_max` (≤ 150 for EU press publishers), `tier` (`A` · `B` · `C` · null), `ai_policy {embed, classify, fulltext, summarize, translate}` (bools), `legal {basis, evidence_url, checked_at, note}`, `flags {paywall_heavy, satire, rumour}`, `status` (pending · active · paused · retired), `default_on`, `poll_minutes`, `subscriber_count`, `health {last_fetch_at, last_success_at, consecutive_failures, newest_item_at, items_24h}`, `created_at`, `updated_at` | none | Seed script, admin `PATCH`, harvest (`health`), `PATCH /me/sources` and the purge (`subscriber_count`) |
| `vocab\|current` | `vocab_pointer` | `version` | none | Vocab release script |
| `vocab\|v{n}` | `vocab` | `version`, `sections [{id, labels {en, de}, descriptions {en, de}}]`, `tags [{id, section, labels, aliases, prototype}]` (≤ 240; `prototype` int8 base64), `content_types`, `created_at` | none (immutable) | Vocab release script |
| `sugg\|{suggestionId}` | `suggestion` | `url` (normalised), `status`, `feed {title, homepage, lang, sample_headlines}`, `probe {https, robots, content_signal, tdm_reserved, items_last_7d}`, `source_id`, `submit_count`, `attempts`, `created_at`, `updated_at`, `decided_at`, `decided_by`, `decision_note`. **No user ids**: each submitter holds a pointer in their own partition | 180 days after the decision | `POST /me/suggestions`, the `suggestions` worker, the admin decision |
| `report\|{reportId}` | `report` | `story_id`, `kind`, `note`, `uid` (becomes `"deleted"` on purge), `status` (open · resolved), `resolution`, `resolved_at`, `resolved_by`, `created_at` | 365 days | `POST /me/reports`, admin resolve, the purge |

The per-source AI restriction lives here, not in code: `tier`, `ai_policy` and `legal.note` (the
free-text remark saying why a feed may not be used with AI). The app shows Tier C as
`flags.aiReserved`. `core/tiers.py` (Phase 2) is the only place that turns these into model input.

### 5.2 `src`

| Id | Type | Fields |
|---|---|---|
| `state\|{sourceId}` | `source_state` | `resolved_url`, `etag`, `last_modified`, `body_sha256`, `etag_works`, `last_fetch_at`, `last_success_at`, `newest_item_at`, `consecutive_failures`, `last_error` |
| `ring\|{sourceId}` | `ring` | `seen` (the last 300 item ids, newest first), `updated_at`. Written only when new items appear |

No TTL. Writer: harvest v2.

### 5.3 `article|{yyyy-mm-dd}` (first-seen UTC day)

Id: `base64url(sha256(canonical_url))`, 43 characters, content-addressed (kept from v1), so dedupe is
a 409 on create instead of a query. Type `article`, `v: 2`.

Fields: `source_id`, `family`, `url`, `canonical_url`, `title` (≤ 300), `teaser` (≤ 1.5 KB) and
`teaser_src` (`description` · `content`; only `description` is ever displayed), `byline` (≤ 80),
`lang`, `published_at`, `first_seen_at` (indexed), `tier` (a snapshot), `enrich_status` (`pending` ·
`done` · `failed` · `dropped`; indexed), `attempts`, `next_try_at`, `last_error`, `emb` (int8, base64,
512 dims), `enr {section, tags (≤ 5), content_type, nw (0–100), flags, entities (≤ 5), fallback, model,
prompt_v}`, `story_id`, `updated_at`. TTL 21 days. Writers: harvest v2 (create), enrich and stories
(patch).

### 5.4 `story|{yyyy-mm-dd}` (first-seen UTC day)

| Id | Type | Fields |
|---|---|---|
| `{storyId}` | `story` | `first_seen_at`, `last_pub_at` (indexed), `updated_at`, `n`, `centroid` (int8 base64), `member_ids` (≤ 30), `members [{id, pk, source_id, family, tier, title, url, published_at, nw}]` (≤ 30), `families`, `n_fam`, `section`, `tags` (≤ 8), `content_type`, `satire`, `paywall_any`, `tier_a_ids`, `primary {id, pk, source_id, title, teaser_display, url, byline, lang, published_at}`, `nw`, `importance` |
| `{storyId}.{lang}` | `summary` | `status` (`pending` · `done` · `failed` · `withdrawn`; indexed), `lease_until`, `attempts`, `bullets` (3), `n_outlets_used`, `source_article_ids`, `source_ids` (indexed, for withdrawals), `lang`, `translated_from`, `model`, `prompt_v`, `cost_micros`, `members_hash`, `created_at` |

TTL 21 days. A member is added with the predicate `NOT ARRAY_CONTAINS(c.member_ids, '{articleId}')`,
so a retried cycle cannot add it twice. Writers: stories and summaries stages; a catalog downgrade sets
`withdrawn`.

### 5.5 `sys` (Phase 2)

| Id | Type | Fields | TTL |
|---|---|---|---|
| `budget\|{yyyy-mm-dd}` | `budget` | `cap_micros`, `committed_micros`, `settled_micros`, `calls`, `by_kind {classify, embed, summarize, translate, describe}`. Reserve = conditional `incr` | 35 days |
| `budget\|{yyyy-mm}` | `budget` | The same, for the monthly ceiling | 400 days |
| `pool` | `pool` | `run_id`, `blob`, `built_at`, `cycle {date, slot}`, `n_stories`, `next_cycle_at`, `p90_cycle_s`, `vocab_v`. Advanced by a conditional patch | none |
| `lease\|{name}` | `lease` | `holder`, `expires_at` (epoch seconds). 25 minutes, renewed per batch | none |
| `run\|{kind}\|{yyyymmddTHHMMSSZ}` | `run` | `kind` (harvest · cycle · maintenance), `created_at` (= start), `started_at`, `finished_at`, `ok`, `error`, `counters`, `n_stories`, `ru_charge`, `estimated_usd` | 90 days |
| `coverage` | `coverage` | `p90 {section: value}`, `computed_at` | none |
| `migration\|v1-v2` | `migration` | `status`, `plan_sha256`, `export_dir`, `owner_oid`, `started_at`, `finished_at`, `counts`, `created`, `merged` | none |

### 5.6 `user|{oid}` (Phase 3)

| Id | Type | Fields | TTL |
|---|---|---|---|
| `subs` | `subs` | `follows {"section:ai": {since}, "tag:apple": {since}}`, `mutes {"tag:crypto": {since, until}}`, `sources {"heise": {state: subscribed · removed · paused, since, until}}`, `statements [{id, text, created_at, vec}]` (≤ 10), `updated_at`. At most 300 follows and mutes. `replace_if_match` | none |
| `model` | `model` | `likes [{vec, w, n, last}]` (≤ 6), `dislikes` (≤ 4), `n_likes_eff`, `tag_affinity` (top 30), `section_exposure` (7 days), `emb_model`, `updated_at`. `replace_if_match` | none |
| `ed\|{editionId}` | `edition` | `edition_id`, `slot`, `date`, `created_at` (= built), `published_at`, `next_expected_at`, `pool_run_id`, `size`, `reading_minutes`, `sections [{kind, hidden_by_filters, cards}]`, `more` (≤ 30 cards), `more_today_count`. A card holds ids and publisher text only, **never AI text**; labels and summaries are attached at response time | 8 days |
| `fb\|{eventId}` | `fb` | `signal` (indexed), `story_id`, `edition_id`, `position`, `dwell_ms`, `created_at` (= the event's `at`; indexed), `received_at`, `weight`, `vec` (int8 snapshot, so undo and later recomputation work), `section`, `tags`, `source_id`, `title`, `url`, `publisher_name` (for history), `target_event_id` (undo), `undone_at` | `open` and `dwell`: `history_retention_days`; explicit signals: 180 days |
| `fb\|save-{storyId}` | `fb` | The like signal behind a save, `signal: "save"`, one per story. Unsaving sets `undone_at`; saving again revives it | 180 days |
| `save\|{storyId}` | `save` | `story_id`, `created_at` (= saved), `title`, `url`, `publisher_id`, `publisher_name`, `published_at`, `teaser` (publisher description), `legacy`. At most 500 | none |
| `sugg\|{suggestionId}` | `sugg_ref` | `suggestion_id`, `url`, `created_at`. At most 50 | 180 days |

Feedback writes are one transactional batch in `user|{oid}`: the quota `incr` (by the number of new
events), their `fb` creates, and one `model` replace with if-match, retried once on 412. An open or a
short dwell skips the model. `PUT` and `DELETE /me/saved` batch the `save` document, its `fb` and the
model replace the same way.

---

## 6. Index policy and query inventory

### 6.1 The policy

`infra/modules/cosmos-index-policy.json`, loaded by `data.bicep` with `loadJsonContent` for container
`news` at creation:

```json
{
  "indexingMode": "consistent",
  "automatic": true,
  "includedPaths": [
    {"path": "/type/?"},
    {"path": "/status/?"},
    {"path": "/kind/?"},
    {"path": "/signal/?"},
    {"path": "/uid/?"},
    {"path": "/created_at/?"},
    {"path": "/last_active_at/?"},
    {"path": "/deletion_requested_at/?"},
    {"path": "/enrich_status/?"},
    {"path": "/first_seen_at/?"},
    {"path": "/last_pub_at/?"},
    {"path": "/source_ids/[]/?"}
  ],
  "excludedPaths": [
    {"path": "/*"},
    {"path": "/_etag/?"}
  ],
  "compositeIndexes": [
    [{"path": "/type", "order": "ascending"}, {"path": "/created_at", "order": "descending"}],
    [{"path": "/type", "order": "ascending"}, {"path": "/status", "order": "ascending"}, {"path": "/created_at", "order": "descending"}],
    [{"path": "/type", "order": "ascending"}, {"path": "/kind", "order": "ascending"}, {"path": "/created_at", "order": "descending"}],
    [{"path": "/type", "order": "ascending"}, {"path": "/signal", "order": "ascending"}, {"path": "/created_at", "order": "descending"}],
    [{"path": "/enrich_status", "order": "ascending"}, {"path": "/first_seen_at", "order": "descending"}]
  ]
}
```

**Why opt-in.** With `/*` excluded, a write pays only for the twelve paths above, not for every field
of a 2 KB article. v1 indexed everything, and its RU went into rewriting 20 KB leads and float
embeddings. The cost of opting in is that **a query on a path that is not listed fails** (Cosmos
rejects filters on excluded paths unless scans are enabled, and scans are never enabled here). The
Phase 2 and 3 paths are included from creation: a path costs nothing on documents that do not carry
it, and adding one later means a background re-index during which queries on it return incomplete
results.

**One composite family serves every newest-first list.** Every listed document carries `created_at`,
and each list is written as `WHERE c.type = @type ORDER BY c.type ASC, c.created_at DESC`. Putting
the filter property into the `ORDER BY` is what lets Cosmos serve the query from the composite index
(Cosmos requires a composite index for an `ORDER BY` on two or more properties, and the filter
property in it makes the index usable).

**Changing the policy** goes through `data.bicep` only, which restates the whole policy. Never use
`az cosmosdb sql container update --idx`: it replaces the entire policy, and it is read-modify-write
for everything it does not mention (README, "The fix was to delete the vector policy").

### 6.2 Rules that `test_index_coverage.py` enforces

1. Every `c.<path>` in a `WHERE` or `ORDER BY` of a string literal that **starts with `SELECT`**,
   anywhere under `backend/api`, `core`, `jobs`, `pipeline`, `selection` and the scripts that query
   `news`, is an included path (or `id`/`_ts`, which are always indexed).
2. Every `ORDER BY` over two or more properties matches a composite index exactly, either in the same
   directions or with every direction inverted.
3. A filtered list is ordered as `ORDER BY c.<filter> ASC, …, c.created_at DESC`, so rule 2 applies.
4. No `enable_scan_in_query`, and no `enable_cross_partition_query` against `news`.
5. **Exempt:** conditional-patch predicates (strings that start with `FROM c WHERE`), because they are
   evaluated against one document and use no index; and constants named `LEGACY_SQL_*`, which run
   against `items`, where `/*` is still indexed.
6. Field names in queries come from the `F_*` constants in `core/models.py`, and the test checks those
   constants against `includedPaths` too.

### 6.3 Query inventory

Phase 1 (final). `@type` and the other parameters are always passed as parameters, never formatted
into the string.

| # | Module · function | Partition | SQL | Paths and composite |
|---|---|---|---|---|
| Q1 | `core.invites.list_invites` | `sys` | `SELECT TOP @limit c.id, c.label, c.status, c.created_at, c.created_by, c.expires_at, c.max_uses, c.uses, c.last_redeemed_at FROM c WHERE c.type = @type ORDER BY c.type ASC, c.created_at DESC` | `type`, `created_at`; (type, created_at↓) |
| Q2 | `core.accounts.list_users` | `users` | `SELECT TOP @limit c.uid, c.status, c.is_admin, c.created_at, c.last_active_at, c.invite_label, c.locale, c.languages, c.blocked_at FROM c WHERE c.type = @type ORDER BY c.type ASC, c.created_at DESC` (limit 501, to detect `truncated`) | as Q1 |
| Q3 | `core.accounts.find_registry_by_uid` | `users` | `SELECT * FROM c WHERE c.uid = @uid` | `uid` |
| Q4 | `core.accounts.overview` | `users` | `SELECT VALUE COUNT(1) FROM c WHERE c.type = @type` | `type` |
| Q5 | `core.accounts.overview` | `users` | `SELECT VALUE COUNT(1) FROM c WHERE c.type = @type AND c.status = @status` | `type`, `status` |
| Q6 | `core.accounts.overview` | `users` | `SELECT VALUE COUNT(1) FROM c WHERE c.type = @type AND c.last_active_at >= @since` | `type`, `last_active_at` |
| Q7 | `core.accounts.overview` | `users` | `SELECT VALUE COUNT(1) FROM c WHERE c.type = @type AND c.created_at >= @since` | `type`, `created_at` |
| Q8 | `core.identity_removal.list_deletions` | `sys` | `SELECT TOP @limit * FROM c WHERE c.type = @type AND c.status = @status ORDER BY c.type ASC, c.status ASC, c.created_at DESC`; with `status=all`, the Q1 form | `type`, `status`, `created_at`; (type, status, created_at↓) |
| Q9 | `core.accounts.overview`, admin health | `sys` | `SELECT VALUE COUNT(1) FROM c WHERE c.type = @type AND c.status = @status` | `type`, `status` |
| Q10 | `core.accounts.stuck_deletions` | `users` | `SELECT c.id, c.uid, c.deletion_requested_at FROM c WHERE c.type = @type AND c.status = @status AND c.deletion_requested_at < @cutoff` | `type`, `status`, `deletion_requested_at` |
| Q11 | `core.accounts.stuck_deletions` | `sys` | `SELECT c.id, c.uid, c.oid, c.purge_started_at FROM c WHERE c.type = @type AND c.status = @status` (age filtered in Python) | `type`, `status` |
| Q12 | `core.cosmos.iter_ids` (purge) | `user\|{oid}` | `SELECT c.id FROM c` | none |
| Q13 | `core.accounts.build_export` | `user\|{oid}` | `SELECT * FROM c` (paged) | none |
| L1 | `core.accounts.last_v1_harvest` | **`items`** / `run` | `LEGACY_SQL_LAST_HARVEST` = `SELECT TOP 1 c.id, c.started_at, c.finished_at, c.ok, c.items_new FROM c WHERE c.kind = 'harvest' ORDER BY c.started_at DESC` | exempt (legacy) |

Phase 2 and 3 (draft):

| # | Module | Partition | SQL shape | Paths and composite |
|---|---|---|---|---|
| D1 | `pipeline.enrich` | `article\|{d}` | `SELECT TOP @limit … FROM c WHERE c.enrich_status = @s ORDER BY c.enrich_status ASC, c.first_seen_at DESC` (`next_try_at` filtered in Python) | (enrich_status, first_seen_at↓) |
| D2 | `pipeline.stories` | `story\|{d}` | `… WHERE c.type = @type AND c.last_pub_at >= @since` | `type`, `last_pub_at` |
| D3 | `pipeline.summaries` | `story\|{d}` | `… WHERE c.type = @type AND c.status = @status` | `type`, `status` |
| D4 | catalog downgrade | `story\|{d}` | `SELECT c.id FROM c WHERE c.type = @type AND ARRAY_CONTAINS(c.source_ids, @source)` | `type`, `source_ids[]` |
| D5 | admin runs | `sys` | `… WHERE c.type = @type AND c.kind = @kind ORDER BY c.type ASC, c.kind ASC, c.created_at DESC`; all kinds: Q1 form | (type, kind, created_at↓) |
| D6 | catalog load | `catalog`, `src` | `SELECT * FROM c WHERE c.type = @type` | `type` |
| D7 | admin suggestions and reports | `catalog` | Q8 form | (type, status, created_at↓) |
| D8 | purge: anonymise reports | `catalog` | `SELECT c.id FROM c WHERE c.type = @type AND c.uid = @uid` | `type`, `uid` |
| D9 | admin audit | `sys` | Q1 form with `OFFSET @o LIMIT @n` | (type, created_at↓) |
| D10 | cycle: prebuild and languages in use | `users` | `SELECT c.id, c.locale, c.languages FROM c WHERE c.type = @type AND c.status = @status AND c.last_active_at >= @since` | `type`, `status`, `last_active_at` |
| D11 | saved, briefs, suggestion refs | `user\|{oid}` | Q1 form with `OFFSET @o LIMIT @n` | (type, created_at↓) |
| D12 | history and seen state | `user\|{oid}` | `… WHERE c.type = @type AND c.signal = @signal AND c.created_at >= @since ORDER BY c.type ASC, c.signal ASC, c.created_at DESC` | (type, signal, created_at↓) |
| D13 | per-card `my` state | `user\|{oid}` | `SELECT c.story_id, c.signal, c.created_at, c.undone_at FROM c WHERE c.type = @type AND c.created_at >= @since` | `type`, `created_at` |
| D14 | feedback duplicate check | `user\|{oid}` | `SELECT VALUE c.id FROM c WHERE ARRAY_CONTAINS(@ids, c.id)` | `id` |

Cursors are offsets (`OFFSET @o LIMIT @n`) wrapped in an opaque token. Every list they serve is capped
at 1,000 rows or fewer, where an offset costs little and, unlike a continuation token, always fits in
a URL.

---

## 7. RU notes

Rules of thumb with opt-in indexing: a point read of a document up to ~4 KB is ~1 RU; a create of
~1 KB is ~5–7 RU; a replace or patch costs about twice a create (budget ~10); a delete is about a
create; a projected single-partition query is ~2.5 RU plus ~0.05 per row returned; a single-partition
`COUNT` is ~3 RU. `core.cosmos` records the real `x-ms-request-charge` of every call.

**Phase 1 route costs (estimates):**

| Route | Operations | ~RU |
|---|---|---|
| Any member route | Gate: one point read of `me` (+1 for the tombstone when `me` is missing) | 1–2 |
| First request of a UTC day | +2 patches (`me` and the registry touch) | +20 |
| `GET /me` | The gate | 1 |
| `PATCH /me` | Gate + replace `me` (+ registry patch when `locale` or `languages` change) | 11–21 |
| `POST /me/redeem` | 2 reads + quota patch (+create) + invite patch + registry upsert + `me` create + audit | ~50, once per account |
| `DELETE /me` | Gate + 2 patches + audit (+ a queue message) | ~30 |
| `GET /me/export` | Gate + quota + reading the partition (~0.3 RU per KB) | ~15 + size |
| Admin lists | One query of up to 200–500 projected rows | 5–30 |
| `GET /admin/overview` | ~8 counts + the invite list + one legacy query | ~40 |
| `GET /admin/health` | ~4 queries + 4 queue peeks (storage, not RU) | ~15 |
| Purge | ~6 RU per deleted document + id pages | A heavy reader (3,000 docs) ≈ 18k RU ≈ 6 min at 50 RU/s |

**Throughput budget.** On `news` (600 RU/s), background work stays at or below 65 % (390 RU/s):
harvest v2 100, cycle 150, purge 50, ops 50 — 350 in total, enforced by `core.cosmos.paced(rate)`.
Interactive requests are unpaced and stay under ~30 RU each. In Phase 1 the purge is the only
background writer on `news`. On `items` (400 RU/s), the v1 harvest is paced to ≤ 350 RU/s; its bursts
used to reach 550–600, and any SDK-retried 429s above the pace are accepted until Phase 2 deletes it.

**Volume.** At 50 users: ~150k user documents (~150 MB) at most, ~10k articles and ~5k stories inside
their 21-day TTL, and a few thousand `sys` documents. That is well inside the free tier's 25 GB. TTL
deletes are free on the free tier.

---

## 8. Deletion and purge order

`DELETE /me` and the `account-purge` worker. Every step is idempotent, so the worker can be retried
from any point (`maxDequeueCount` 3, then `account-purge-poison`, and the maintenance timer
re-enqueues anything stuck).

**The request** (`api/account.me_delete` → `core.accounts.request_deletion`):

1. The member gate, which also lets blocked and deleting members through.
2. If `me.status` is not `deleting` yet: patch `me` to `status: deleting` with
   `deletion_requested_at`, patch `users/{oid}` the same way, write the audit entry
   `account.deletion_requested`, and put `{"v": 1, "kind": "account", "oid", "requestedAt"}` on
   `account-purge` through the queue output binding. A repeat skips all of this.
3. Answer `202 {"state": "deleting", "identityRemoval": "manual"}`. From step 2 on, every other route
   answers `410 account_deleted` for this account.

**The worker** (`jobs/queues.account_purge_worker` → `core.accounts.purge_account`), paced at
**50 RU/s**:

1. Parse the message and derive the `uid`. Read `me`.
   - Missing, with a tombstone: an earlier run finished the destructive part, so only finalise (step 9).
   - Missing, without a tombstone: nothing to do. Log and acknowledge.
   - Present but not `deleting`: refuse, log and acknowledge. A queue message alone never deletes an
     active account.
2. Create `sys/deletion|{uid}` if absent (`status: purging`, `oid`, `identity_mode`,
   `created_at` = the request time), and increment `attempts`.
3. **Read `subs` first** (Phase 3; absent in Phase 1). For each subscribed source not yet in
   `decremented_source_ids`: record the id there with a conditional patch
   (`NOT ARRAY_CONTAINS`), then decrement `catalog/src|{id}.subscriber_count`. Recording first means a
   crash can leave a count one too high, never one too low and never doubled.
4. **Purge the partition, paced:** page through `SELECT c.id FROM c` in `user|{oid}` and delete every
   id except `me`, ignoring 404s. `docs_deleted` is patched every 100 deletions. The preview
   delete-by-partition-key is not used: it needs an account capability, is not recommended with
   continuous backup, and cannot be paced.
5. **Anonymise outside the partition:** the invite redemption (`users/{oid}.invite_id` → the
   `redemptions` entry with this uid becomes `"deleted"`), and from Phase 3 the user's reports
   (`uid` → `"deleted"`). Each is recorded in `anonymised`.
6. **Write the tombstone** `sys/tombstone|{uid}` (`ttl` −1).
7. **Delete `me`.** The tombstone exists before `me` disappears, so there is never a moment in which
   the account reads as "never a member" (`403 invite_required`) instead of `410`.
8. **Delete `users/{oid}` last**, so a crash at any earlier point still leaves the registry document
   for the maintenance timer to find.
9. **Finalise:** `purged_at`, `docs_deleted` and `status: awaiting_identity_removal` (manual mode) on
   the deletion document, and the audit entry `account.purged` (uid only). In graph mode (Later) the
   worker deletes the Entra user here and completes the deletion itself.

**Identity removal (manual):** the owner removes the Entra user (Users › Delete, then Deleted users ›
Delete permanently), then calls `POST /admin/deletions/{uid}/complete`. That sets
`identity_removed_at`, removes the stored `oid`, gives the deletion document a 90-day TTL and the
tombstone a 30-day one, and writes `identity.removed`. The target is within 7 days: the admin health
and the maintenance timer flag anything older.

**Maintenance** (`jobs/timers.maintenance_timer`, daily 02:15 UTC):

- re-enqueue `account-purge` for registry documents `deleting` for more than 1 h (Q10), and for
  deletion documents still `purging` more than 1 h after `purge_started_at` (Q11), deduplicated by
  oid;
- log `ALERT identity_removal_overdue` for removals waiting more than 7 days;
- log `ALERT secret_expiry` daily from 30 days before `APPLE_SECRET_RENEW_BY` (and after it);
- from Phase 2, the monthly robots re-check.

**What remains after a deletion** (disclosed on the privacy page): the tombstone (uid only; 30 days
after identity removal), the deletion record (uid; 90 days), audit entries (uid; 365 days), logs keyed
by uid (30 days), Cosmos continuous backup (7 days), and, until the owner removes it, the Entra user.

---

## 9. Queue messages

Messages are JSON with **ids only**, camelCase like the API, written through `@bp.queue_output` (the
host does the base64 encoding the trigger expects). Each carries `v` so a worker can refuse a shape it
does not know. `host.json`: `batchSize` 4, `newBatchThreshold` 2, `maxDequeueCount` 3,
`visibilityTimeout` 00:00:30, `maxPollingInterval` 00:00:05; `functionTimeout` 00:20:00.

| Queue | Message | Producers | Worker |
|---|---|---|---|
| `account-purge` | `{"v": 1, "kind": "account", "oid": "<guid>", "requestedAt": "<ts>"}`. Phase 3 adds `"kind": "history"` (clear opens and dwell signals only) | `DELETE /me`, the maintenance timer; P3 `DELETE /me/history` | `account_purge_worker` (P1) |
| `ops-jobs` | `{"v": 1, "jobId": "<uuid>", "kind": "harvest", "force": true, "sourceId": null, "requestedBy": "<uid>" \| "system", "requestedAt": "<ts>"}`. Phase 2 adds `"kind": "cycle"` and `"republish"` | P2 `POST /admin/runs/*` and catalog downgrades | `ops_jobs_worker` (P1: a forced v1 harvest, optionally of one source) |
| `suggestions` | `{"v": 1, "suggestionId": "<32 hex>"}` | P3 `POST /me/suggestions` | `suggestions_worker` (P3; not registered before it works) |

A worker that sees `dequeue_count` ≥ 3 logs `ALERT purge_failed` (or the matching kind) before
raising, because that attempt is the last one before the poison queue.

---

## 10. The legacy container `items`

v1's container keeps everything v1 wrote, untouched except by the v1 harvest:

| Partition | Documents | Phase 1 users |
|---|---|---|
| `art\|{yyyy-mm-dd}` (publish day) | v1 articles, including the 19 pinned likes (`ttl` −1) | v1 harvest (create, patch) |
| `src` | `source_state` per feed, id = source id | v1 harvest |
| `run` | `RunRecord` per harvest, id `harvest-{ts}`, TTL 90 days | v1 harvest (write); admin overview and health (`LEGACY_SQL_LAST_HARVEST`) |
| `user\|me` | `sources` (the v1 source list), plus v1's profile, centroids and feedback events | v1 harvest reads `sources`; the Phase 5 migration reads the rest |

Phase 1 switches `pipeline/harvest.py` and `core/sources.py` to
`get_container(LEGACY_COSMOS_CONTAINER)` and paces their writes to ≤ 350 RU/s. Phase 2 deletes the v1
harvest in the release that ships harvest v2. Phase 5 migrates the owner's 19 likes, exports again,
and deletes `items` by hand after a 14-day green soak. Its index policy (`/*` included) is never
changed in place.
