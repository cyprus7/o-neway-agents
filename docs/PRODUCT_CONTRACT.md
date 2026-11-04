# Application and channel contract

Endpoint examples and compatibility spelling are in [the API contract](API_CONTRACT.md). This document describes identity, commands, channels and aftercare. Endpoint behavior is verified by integration tests; `/api/sessions` is the trip API prefix.

## Identity

Trusted IdentityResolver(request) resolves an internal owner. Default guest/Telegram cookies; hosts supply configured issuer/subject mapping from trusted middleware, never public X-User-ID. Missing/expired login401; another owner's resource404 for reads/mutations. Jobs inherit trip ownership. SSE authenticates before streaming, rechecks at snapshot/heartbeat and closes on expiry/revocation/deletion. Cursors grant no access. /health is public without private state; metrics require operator/host authorization.

POST /api/auth/guest issues an opaque token cookie: expiry12h, HttpOnly, Secure on HTTPS, SameSite=Lax, hashed at rest. Localhost HTTP is explicit Secure exception. Verified Telegram login uses Secure, SameSite=None, Partitioned cookies so the same API and native EventSource work inside Telegram Web's cross-site frame. The auth cookie remains HttpOnly; the CSRF cookie remains readable only to the application origin. Registered-user credential reissue preserves this partitioned delivery policy. Cookie mutations require exact allowed Origin and session-bound CSRF header; no wildcard credentialed CORS. Telegram login verifies Origin too. No tokens in URL/localStorage/SSE URLs. A trip URL is navigation only.

Cookie issuance expires the opposite storage variant to avoid same-name credentials shadowing each other; logout revokes the server access session and expires both variants. Auth responses are not cacheable. Bootstrap verifies the resulting authenticated session before loading trips or opening SSE. A rejected bootstrap fails once with an explicit re-entry message, rather than automatically remounting and minting repeated sessions. A later401 clears private state and requires explicit re-entry. No browser security setting changes are required. Older clients that cannot store embedded cookies receive a bounded failure; native Telegram or a supported browser remains the supported entry path.

Telegram may retain an old launch when a Mini App is reopened/reloaded. New access
credentials still require the five-minute signed launch window. An authentic expired
launch may only resume an already-valid cookie session when exact bot/subject mapping,
session-bound CSRF and Origin all match. This read-only resume creates no user/session,
sets no cookie and never extends the existing absolute expiry. Tampered/future/malformed
data, another owner and missing/revoked/expired credentials do not resume. A denied
authentic expired launch returns telegram_launch_expired with a close/reopen-from-bot
instruction. The UI calls the official SDK close method instead of reloading the same
old launch. SDK ready signals a rendered useful shell, including a safe terminal error;
it is independent of authorization and never enables private controls on failure.

## Commands/projection

Existing message/refresh/select/quote/checkout routes delegate to TripApplication. Add owner-scoped GET /api/sessions (cursor list default20/max50), POST /api/jobs/{id}/cancel, GET /api/sessions/{id}/receipts, and memory/export/delete under /api/me.

Async mutations carry Idempotency-Key8–128 chars and expected_revision. Same owner/key/hash returns original job/result; different hash409 idempotency_conflict. Stale revision conflicts before work. Create/login have no trip revision. Quote/checkout retain stricter quote/revision/source binding. Accepted202 returns job_id, sequence, current revision, safe status. One active per trip; user/global admission failure429 with retry info.

States queued/running/succeeded/failed/canceled/interrupted. One worker, one running plus two queued per user,32 queued globally, fair rotation. Resume only never-started queued work. Running-at-restart becomes interrupted; no automatic model replay. Cancel fences result commit and does not refund spent quota. Explicit retry uses new key/current revision. Result and terminal job commit together; SSE only after commit, preserving durable sequences and private projection.

Plan projection includes active_revision, accepted_plan_revision, work status and superseded snapshot. No cross-owner/epoch merge. Changes invalidate selection/quote; explain/compare preserves revision. Historical receipts stay immutable.

## Agent policy and changes

Typed proposal: clarify/explore/refine/compare/explain. One validated action per turn. Model tool names/URLs never drive arbitrary dispatch. Explain/compare use saved option IDs/evidence, or ask clarification for invalid IDs. Explanation can be deterministic. Selection/checkout always require user commands.

| Change | Invalidate/recompute | Preserve when scope matches |
|---|---|---|
| budget | feasibility/ranking, selection/quote | source observations, trip scope |
| dates | timed inventory, weather/events, connections, selection/quote | stable places, unrelated wishes |
| origin | route/transfer/checks, selection/quote | destination lodging if full scope matches |
| destination | route/destination facts, selection/quote | travelers/unrelated wishes |
| travelers/rooms/pets | scoped inventory/conditions/checks, selection/quote | independent geography |
| lodging requirements | lodging eligibility/ranking/checks, selection/quote | compatible route facts |
| explanation/comparison | no inventory/revision mutation | accepted state |

Unknown does not satisfy a hard constraint. Explicit wishes/negations override memory. Price arithmetic stays deterministic.

## Candidate capability

Add optional inventory_candidates capability and POST /v1/inventory/candidates to travel-data.v1; reuse scoped ComponentBundle plus stable candidate_id/source identity. limit1–9; sum requested across destinations at most9; display at most3. No Cartesian expansion. Older peers lacking capability use existing inventory. Keep correlation/fingerprint validation,256KiB request/2MiB response and45s total including queue/network/read. Fixed refresh keeps identity. Gateway remains inference/session-free. One agent-side enrichment batch across shortlist within018 budgets, not one per candidate. Covered catalog hotels skip external lodging discovery/pricing. No new browser provider required.

## Telegram

POST /api/auth/telegram validates raw initData with official HMAC, constant-time compare, rejects duplicate/malformed fields/missing user, checks now-300s <= auth_date <= now+30s. Never authorize from initDataUnsafe. Map unique(telegram:<configured bot_id>, decimal user_id), not username. Reauthentication resolves same user; no auto-merge with guest.

POST /api/telegram/webhook checks configured secret header before parsing; body max64KiB. Private-chat /start,/new,/trips,/help,/stop and text only. Safely ignore unsupported updates. User/chat active-trip mapping; deep-link trip still needs owner check. Atomically dedup(bot_id,update_id) with command admission before2xx. Never wait on inference. Explicit local polling uses same handler/persisted cursor; never polling+webhook simultaneously. Short escaped response and Mini App link; rich decisions/payment remain in shared app. No bot secrets in browser/logs/traces.

Mini App uses same API/SSE/cookie, ready after usable initialization, theme/stable viewport/safe areas/Back, ordinary-browser fallback. start_param is not authorization. Use fake app checkout, never Telegram real payments.

## Host lifecycle

create_app(dependencies=...) provides repository/model/data factories plus IdentityResolver, with migration compatibility for current factories. Lifespan owns/closes resources exactly once. Mounted host enters child lifespan explicitly. Reuse one runtime across routes/mounts; independent per-mount inference clients unsupported. REST host uses trusted gateway/context authentication and scoped mapping; no unauthenticated identity header. No generic storage SDK promise.

## Aftercare/metrics

Owner-scoped receipts/drafts/reminders delegate to application modules. Amendments create new linked trips. Service requests remain draft/archived; reminders need consent/timezone/cancel. No supplier dispatch. Metrics have finite event/outcome labels, no owner/session label cardinality or raw prompts/source pages. User explanations are distinct from developer diagnostics.

Reminder public objects expose `state`: scheduled/sending/sent/canceled/failed/delivery_unknown. The UI consumes that existing field; there is no `status` alias. Cancellation applies to scheduled/sending, while terminal states remain immutable or retain uncertainty. Draft objects separately use `status`.
