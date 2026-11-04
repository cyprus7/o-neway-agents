# One Request PoC — shared implementation contract

FastAPI + SQLite + React/Vite. The local app is exposed on configurable `APP_PORT` (8080 by default); dependencies stay internal to Compose. Runtime inference uses environment-only credentials and a shared process limiter of at most 10 dispatches per rolling 60 seconds, including retries. No credentials enter the frontend. Source material is evidence; the user request controls scope.

## Product boundary

Authentication, ownership and command requirements in this section apply to all endpoint examples below.

All private `/api/` routes resolve a trusted Principal to an existing internal user. The standalone adapter uses guest/Telegram cookies; a mounted host supplies `AgentDependencies.identity_resolver` from verified host middleware. A public `X-User-ID` is never trusted. Foreign trip/job/memory-source IDs return404; missing/expired login401. SSE rechecks cookie revocation/expiry and ownership before updates and heartbeats. Metrics require an operator principal.

- `POST /api/auth/guest` requires exact allowed Origin. Returns `{user:{id,kind},csrf_token}` and12-hour HttpOnly SameSite=Lax auth cookie. HTTPS cookies are Secure; localhost HTTP is an explicit exception.
- Verified Telegram login uses the same opaque cookies with `Secure; SameSite=None; Partitioned` for cross-site Mini App frames. The auth cookie stays HttpOnly; exact-Origin and session-bound CSRF remain required for writes. Registered-user reissue preserves this policy. Issuance expires the opposite cookie variant; logout revokes the server session and expires both variants. Auth responses use `Cache-Control: no-store`. Bootstrap verifies `/api/auth/session` before private trip reads/SSE; failed authentication requires explicit re-entry, without repeated automatic login.
- An expired but fully authenticated Telegram launch may resume only the existing valid cookie for that exact bot/user with matching CSRF. Resume sets no cookie, creates no identity/access record and does not renew expiry. Otherwise authentic expired launches return `telegram_launch_expired`; invalid/future data remains rejected. The Mini App exposes a safe error screen and SDK close action for a fresh launch from the bot.
- `POST /api/auth/telegram` takes `{init_data:string}`, same Origin checks, official HMAC and bounded freshness. Numeric user identity is mapped under configured bot ID; guest data is never silently merged.
- `GET /api/auth/session`, `POST /api/auth/logout`. Cookie writes require `X-CSRF-Token` plus exact Origin. Tokens/initData never belong in URLs or localStorage.
- `POST /api/sessions` takes optional `{preference_session_id,apply_memory:false}`. Related trip must be same-owner; approved memory only applies when explicitly requested.
- `GET /api/sessions?limit=20&cursor=...` returns `{items:[{id,title,destination,revision,created_at,updated_at}],next_cursor}`, limit1–50.
- `POST /api/sessions/{id}/messages` takes `{content,expected_revision,action?,option_ids?}`; optional action is compare/explain with at most3 IDs.
- Messages and `POST /api/sessions/{id}/prices/refresh` require `Idempotency-Key`8–128 characters. Same owner/key/hash returns original job; conflicting payload409; missing revision422/stale409. Acceptance includes `job_id,sequence,status,deduplicated`; messages additionally return current revision.
- `POST /api/jobs/{id}/cancel` persists cancellation and interrupts active execution. Completed work is never replayed by retrying the same key.
- `POST /api/sessions/{id}/select` accepts `{option_id,expected_revision?}`; stale supplied revisions409. Current selection/quote/checkout stay blocked during active work.
- `GET /api/me/memory` returns `{items:[{field,value,source_trip_id,consented_at,updated_at}]}`. `PUT /api/me/memory/{field}` takes `{value,source_trip_id}`; DELETE forgets it. Stable allowlist: origin, currency, climate_preferences, trip_styles, direct_flight_preferred, max_travel_minutes, pets. Dates, budget, traveler counts/ages and arbitrary inferred wishes are excluded.
- `GET /api/me/export`, `DELETE /api/sessions/{id}`, `DELETE /api/me` are owner-scoped. Deletion fences abandoned writes and cancels work.
- `GET /api/sessions/{id}/receipts` returns `{items:[receipt]}` with immutable simulated receipts. New receipts include `itinerary:{intent,option}`; old missing context stays unknown.
- `GET /api/sessions/{id}/aftercare` returns `{receipts,drafts,manual_context,reminders,reminders_available}`. `POST .../drafts` takes `{kind:"early_checkin"|"service_request",text}`; `PATCH .../drafts/{draft_id}` only accepts `{status:"archived"}`. No supplier send operation exists.
- `PUT .../manual-context` takes `{destination,hotel,date_from,date_to}` and always labels it user-provided/unverified. `POST .../amend` takes `{receipt_id?,date_from?,date_to?,request?}` plus Idempotency-Key, returning `{session,job_id}` for a new same-owner linked trip. Fixed source hotel identity is preserved until an explicit destination change; source rates do not cross changed scopes. Original receipts are never rewritten.
- `POST .../reminders` takes `{local_time,timezone,consent:true,text}`. Local time has no offset; an IANA zone must produce exactly one future UTC instant within365 days. Ambiguous/nonexistent DST times422; absent linked/configured Telegram409. `DELETE .../reminders/{id}` cancels scheduled work; in-flight uncertainty remains `delivery_unknown`, never a claimed successful cancellation/delivery.
- Reminder responses and `aftercare.reminders[]` use `state` (not `status`): `scheduled`, `sending`, `sent`, `canceled`, `failed`, `delivery_unknown`. Only scheduled/sending work has an applicable cancellation action. Terminal states retain their exact label; uncertain delivery is never rendered as success. Drafts independently use `status`.
- All aftercare IDs share the same foreign-owner404 rule, cookie/Origin/CSRF requirements and deletion fences. No supplier booking, payment or message is performed.

Example: after guest login, create a trip; submit `{"content":"Из Варшавы в Ларнаку","expected_revision":0}` with `Idempotency-Key: trip-command-001`, cookie, Origin and CSRF header. A second user using that trip ID receives404 even with their own valid token. Retrying the identical command returns the original job; changing text under that key409. Compare via `{"content":"Сравни варианты","expected_revision":1,"action":"compare","option_ids":["r1-…","r1-…"]}` and a new key. It uses saved evidence without new model/provider calls.

One singleton worker owns command execution: one running, two queued per owner,32 queued globally, fair owner rotation. Only never-started queued work resumes. Running-at-restart becomes interrupted and requires explicit retry with a new key. Public terminal success retains compatible `completed` spelling; canceled/interrupted join queued/running/completed/failed/superseded.

Session adds `accepted_plan_revision` and `previous_plan:{revision,intent,options}|null`. Accepted options stay readable during extraction; changed intent invalidates selection and moves previous options into a read-only snapshot with their original source scope. Terminal result/job commit atomically. Unknown facts remain unknown.

The optional `travel-data.v1` capability `inventory_candidates` adds `POST /v1/inventory/candidates`, limit1–9 and stable scoped candidate IDs. Agent requests at most3 per destination and9 total, presents at most3 distinct choices. Old peers use inventory fallback. Price research shares the singleton inference client and one optional batch per command across parallel candidates. No additional service is introduced.


## HTTP (apply the product-boundary requirements above)

- `GET /health` -> `{status:"ok", ...}`; no secret values.
- `GET /api/config` -> `{llm_configured:boolean,llm_dispatch_enabled:boolean,llm_model:string,requests_per_minute:number,timezone:string,providers:object,pricing:object,...}` public safe capabilities. `llm_dispatch_enabled` reflects `LLM_ENABLED`; `llm_configured` additionally requires credentials/base URL/model and is false when dispatch is disabled. Neither boolean reveals credential values or proves upstream health.
- `POST /api/sessions` body `{}` -> Session.
- `GET /api/sessions/{id}` -> Session.
- `POST /api/sessions/{id}/messages` body `{content:string}` -> `{job_id:string,sequence:number}` (202). Exactly one running job per session. Sequence is the committed acceptance barrier for stream reconciliation.
- `GET /api/jobs/{id}` -> `{id,status:"queued"|"running"|"completed"|"failed",session_id,error?:string}`. Diagnostic compatibility endpoint; the UI receives updates through SSE. User-safe error only.
- `GET /api/sessions/{id}/events` -> `text/event-stream`: named `session` events containing `{sequence,session,job,server_time,reason}`. Complete current public projection on every connect/reconnect, then post-commit updates. `job` is latest active or terminal job with private payload omitted. Durable sequence is independent of itinerary revision. Intermediate snapshots may coalesce; Last-Event-ID does not promise historical replay. Same-sequence freshness projections are valid. Missing trip returns404 before streaming.
- `POST /api/sessions/{id}/select` body `{option_id:string}` -> Session.
- `POST /api/sessions/{id}/checkout` body `{option_id:string}` -> `{id,status:"simulated",message:string,...}`. Never takes payment. Must reject impossible/stale options.
- `GET /api/metrics` -> safe aggregate operational metrics.

## Session

`{id, created_at, updated_at, revision:number, intent:TravelIntent, messages:Message[], options:TripOption[], events:AgentEvent[], selected_option_id:string|null, assumptions:string[], clarification:string|null, last_changes:string[], preferences:object}`

Message: `{id,role:"user"|"assistant",content,created_at}`.

AgentEvent: `{id,step:string,status:"running"|"completed"|"warning"|"failed",message:string,created_at,details?:object}`. Events represent real execution, not staged timers.

TravelIntent: `{origin:string|null,destination:string|null,date_from:string|null,date_to:string|null,nights_min:number|null,nights_max:number|null,adults:number,children_ages:number[],pets:string[],budget_total:number|null,currency:string,climate_preferences:string[],trip_styles:string[],max_travel_minutes:number|null,direct_flight_preferred:boolean,hotel_requirements:string[],hard_constraints:string[],soft_constraints:string[],assumptions:string[]}`. Patches preserve prior fields, especially on clarification.

Iteration003: `transport_requirement:<readable text>` in hard/soft arrays scopes special cabin,
equipment or other transport conditions to route search; it is not a new transport mode.
The prefix is omitted from user-facing constraint labels. `hotel_requirements` belongs only
to lodging. Unknown `transport:*` modes produce clarification instead of becoming flights.
`max_travel_minutes` is an overall journey cap: one short flight cannot verify unknown ground legs.
Lodging and transport evidence searches are separate; destination relevance is not address or
inventory verification. No public HTTP/schema fields were added by003.

TripOption: `{id,destination,country,subtitle,latitude:number,longitude:number,score:number,estimated_total:number|null,currency,price_status:"observed"|"partial"|"unavailable",flight:TravelComponent,stay:TravelComponent,transfer:TravelComponent,weather:object|null,reasons:string[],tradeoffs:string[],risks:string[],evidence:Evidence[],constraint_checks:object[],image_url?:string,booking_url?:string}`.

TravelComponent: `{title,description,price:number|null,currency,source_url:string|null,status:"observed"|"unavailable"|"estimated",details?:object,evidence?:Evidence[]}`. `estimated` allowed only for explicitly identified deterministic estimates, never masquerades as live price. Missing prices never become zero in totals/rankings.

Evidence: `{provider,source_url,retrieved_at,freshness:"live"|"cached"|"snapshot"|"unavailable",confidence:string,label:string}`. Expose distinction search evidence vs verified itinerary availability. Weather for distant dates must be labeled historical/climate, not forecast. Links may supplement unavailable price inventory.

## Modular data boundary011

The external session/job/selection/checkout/SSE API remains the product integration boundary.
Default agent composition calls a separate private provider gateway through `TravelDataPort`;
it never loads concrete catalog/search/provider adapters on gateway failure. The internal
`travel-data.v1` API is documented in
[the versioned contract](TRAVEL_DATA_CONTRACT.md).
`GET /api/config` preserves existing provider/pricing projections and additionally exposes
`data_gateway` status/contract metadata. If gateway capabilities are unavailable, external
fallback is reported false and providers unavailable rather than inferred from agent env.
`GET /health` continues to report agent/storage health independently of data availability.

[Integration guide and diagrams](architecture.md) and
[the FastAPI host example](../examples/host_integration.py) show REST/SSE integration or a
`/travel` mount with explicit child lifespan. The host frontend must use the prefixed API;
the standalone React demo uses root paths. The application enforces ownership through a trusted
identity resolver or standalone guest/Telegram cookies, as specified above.

Current catalog capabilities describe named source POIs, accommodation-kind counts,
independent source counts, overlapping coverage circles and original release dates. These
are factual snapshot metadata, not verified operating-hotel counts, room availability or rates.

## Expanded search and browser providers006

`/api/config.providers.search` is `{name,configured,capability:"search_evidence_only",providers:SearchCapability[]}`.
Each capability is `{id:"searxng"|"ollama",name,configured:boolean,status,reason:string|null,capability}`.
Status is `ready` before an attempted configured operation, or the latest `live`, `cached`,
`unavailable`, `disabled` or active `cooldown` observation. `configured` is a configuration check,
not successful authentication or proof of search coverage. Reasons are bounded codes such as
`missing_key`, `rate_limited`, `deadline_exceeded`, `authentication_failed` or `invalid_response`;
upstream error bodies, keys and auth headers are never public.

SearXNG GET search and fixed hosted `POST https://ollama.com/api/web_search` execute in parallel.
Only backend reads `OLLAMA_API_KEY`; the request uses Bearer auth and `{query,max_results:10}`.
Missing key disables Ollama alone. This is separate from internal CPU embeddings. Numeric free-tier
quotas are not claimed. Each provider has a <=10s whole-operation deadline including semaphore
queue, a1MiB response ceiling and no redirect following. `429` creates a `Retry-After` cooldown
(seconds or HTTP date; defensive1s..24h bounds,60s fallback); it does not cause an immediate retry loop.

Internal `SearchHit` contains provider/title/url/snippet/retrieved_at/freshness/engine/stale_if_error;
`SearchOutcome` adds provider/status/reason. Only eligible lodging/route hits enter stable round-robin
URL merging; fragments do not create new sources. Destination/origin and booking-country checks
precede the15-candidate ranker limit. Public evidence keeps up to3 lodging plus3 transport results.
Each result retains its own provider/time/freshness after merge and ranking. Search caches are
separate by schema/provider/endpoint/query; ordinary evidence TTL is1h, concrete hotel fallback1d.
On provider failure a cache fetched less than7d ago may be used with original `retrieved_at`,
`freshness=cached` and an explicit saved-result warning. Cache reuse does not renew its times.
Search snippets are untrusted text and cannot populate prices, verified inventory or policy flags.

Concrete hotel fallback also federates, accepting only Booking hotel cards and exact Ostrovok
hotel-card hosts/paths with destination relevance. It preserves `property_location_verified=false`;
an indexed card is not an address, rate or availability check.

`/api/config.pricing.providers` additionally includes `id=ostrovok`, configured/status and supported
scope. The internal worker accepts `provider_id=google_hotels|ostrovok`, structured context and an
optional matching `target_entity_id` (`google-hotel:<24hex>` or `ostrovok:<24hex>`); callers cannot
supply an arbitrary URL. New lodging candidates prefer Ostrovok then Google; fixed-property
refresh remains with its provider and must match the derived entity ID. Ostrovok currently supports
Athens/Istanbul/Prague/Tbilisi, EUR, one room,1–2 adults without children,1–30 nights. The recipe
checks displayed city/property, dates including year, guests/rooms and currency, stops at challenge,
and does not enter booking/payment flows. Existing worker deadline20s includes queue; cleanup has
a separate bounded2s budget. Application price refresh retains its whole55s deadline.

An observed Ostrovok tariff includes room/rate terms and included/excluded/unknown charges from
the same offer. It currently remains `outcome=reference`, `value.kind=lower_bound`,
`normalized_group=null`, `component_complete=false` and `availability_verified=false` because
mandatory-charge completeness and full rate applicability are not established. A tax line in CZK
must not be converted or added to an EUR amount implicitly. Existing pricing/SSE contracts remain.

`LLM_ENABLED=0` rejects generation before quota acquisition or HTTP dispatch; read views, SSE and
structured provider refresh remain available. Optional model-assisted price extraction is disabled.
The limiter and 429 backoff are process-local; separate processes do not share a quota counter.

## UI

Pricing002 additions: `TravelIntent.rooms` defaults to1 (range1..9); optional `room_allocation`
must account for all adults and child ages. `TravelComponent.quote` is a typed PriceObservation;
`TripOption.pricing_summary` is computed server-side. Monetary values are finite Decimal strings
with kind `point`, `closed_range`, `lower_bound` or `unknown`. Raw `value` may be a public
reference; only `normalized_group` participates in requested-trip arithmetic. Preserve original
unit, observed scope, source URL, observed/retrieved/expiry timestamps and independent freshness.
The legacy numeric `price` field does not establish compatible scope. UI must not sum it.

- `POST /api/sessions/{id}/prices/refresh` body `{expected_revision:number,option_id?:string}`
  returns202 `{job_id,sequence,deduplicated}`; no URLs, injected quotes or provider credentials accepted.
  Refresh and messages share one active job per session. Duplicate same-revision/target refresh
  coalesces; competing mutations return409. Application has one55s refresh deadline within the
  60s job lifetime, including queued time. No generation or embedding call is needed.
- `GET /api/jobs/{id}` adds `kind` (`message` or `prices_refresh`), `result_revision` and
  `superseded` terminal status. Private snapshots/payload are omitted from responses.
- Verification activity012 adds public `target_option_ids` and `result_option_ids` to GET job
  and SSE snapshot.job, each defaulting to an empty list with at most3 IDs. Targets belong to
  `captured_revision`; completed result IDs belong to `result_revision` and include only refreshed
  options. Old/message jobs have no inferred scope. These fields identify work, never verified facts.
- Applied refresh atomically increments revision, regenerates ALL option IDs and clears selection.
  A late result cannot overwrite a newer revision/context. The user selects the refreshed option.
- `/api/config.pricing` exposes keyless capabilities and browser readiness; no secret values.
  GET session recomputes quote freshness/summary. Expiry is validated before existing receipt
  lookup; stale quotes return409 `price_expired`. Unknown/reference-only plans remain explicit
  research simulations, with unverified budget and inventory.

Live005: one native EventSource per view; no periodic client job/session reads. HTTP POSTs remain
commands. After bootstrap the stream owns session state, so late command responses cannot overwrite
newer snapshots. Submission pending ends after acknowledgement AND a streamed cursor reaches its
accepted sequence; active latest job owns execution busy. Completion before acknowledgement is safe.
Heartbeat comments plus named `heartbeat` events arrive every15s; retry2s, closed-connection retries
cap4s, watchdog45s, manual retry immediate. Old subscriptions close on trip switch/unmount. Failed
connections preserve readable results with visible reconnecting status. Expiry triggers a fresh
projection without writes, price renewal or provider/inference requests. SQLite commits state and
update cursor atomically; one-slot subscriber wakeups are emitted after outer commit. This adapter
supports the existing single Uvicorn process; multiple workers require shared cross-process wakeups.

`details.pricing_legs` contains the sanitized date/route/amount/currency/source basis of a
two-leg public fare. `details.pricing_outcomes` preserves source-specific failures. Public
references have `outcome=reference, normalized_group=null`; known date/entity conflicts from
the browser are rejected even when the source labels its amount a reference.
Lodging request identity also includes `destination_city`, separately from the transport
airport; city aliases must match the enabled recipe before it opens a page.

Russian primary language. Desktop side conversation panel + main workspace with request summary, visible genuine agent events, three comparison cards, provenance and missing-price badges, selectable itinerary flight/stay/transfer, visibly simulated checkout. Responsive mobile. First empty state with useful example prompts. Subsequent messages replan same session, preserve intent and show changed fields. Session saved in browser; reload survives backend restart via SQLite.

## Regional event context (007)

Session options add `regional_events` separately from `evidence`, hotel requirements and prices.
The result has status ready|empty|unavailable|dates_required|not_checked, at most3 items and a
nullable UTC `checked_at`. Legacy payloads default to not_checked/empty. See the [event domain models](../backend/app/regional_events/domain.py) for source/date/place fields.

Events require physical destination/nearby-region eligibility and explicit source dates overlapping
the trip; snippets and recurring annual guesses are insufficient. Dates are publisher-local days.
Source URLs/retrieval/cache status remain visible; tickets and attendance are unverified. The UI
shows optional context for the selected route, with no automatic scheduling or purchase.

Message replanning rebuilds scope; price refresh retains events. Existing SSE saves/reconnect carry
the field without polling. Public projection expires source facts after1h (even if a new cached
check occurred); source timestamps in storage remain unchanged. Additional discovery is bounded12s
with no generation calls and does not change route ranking, constraints, hotel identity or totals.

## Local Cyprus catalog (009)

`GET /api/config` adds `external_data_fallback_enabled` and `providers.catalog`: status/reason,
row_count, coverage, release_id, original retrieval/source dataset times and ODbL attribution URL.
Weather, map and inventory capability flags reflect the effective fallback policy.

Stay.details adds discovery_mode=local_catalog, catalog_property_id, catalog_version,
catalog_retrieved_at, stable pricing_entity_id, source raw osm_tags and at most2 alternatives.
Existing Evidence uses freshness=snapshot and original timestamp. Local property matches skip
hotel web/Overpass/browser/SerpApi hotel calls and cannot be replaced by unrelated rate lookup.
Missing catalog uses explicit bounded fallback; WEB_FALLBACK_ENABLED=0 prevents all external
travel-data requests. LLM ownership/limits are independent. Price and condition schemas are unchanged.

## Browser flight observations (017)

The existing inventory/refresh operations carry the additive
`flight.details.flight_route` observation: both local-date/time legs, ordered
observed connections, carriers/stops/duration, actual source scope and original
timestamps. `pricing_entity_id=google-flight:<64hex>` binds that itinerary;
amount, rank and observation time do not define its identity. The gateway calls
the private [flight-search.v1 service](FLIGHT_SEARCH_CONTRACT.md)
in parallel with initial lodging discovery. No new planner or public command is
required. `pricing_outcomes` distinguishes a private service request, attempted
browser job, source failure and cache reuse; it does not count each browser HTTP
request as a separate source job.

Paired-return group from-prices are `outcome=reference`, `value.kind=lower_bound`,
`scope.unit=group_trip`, with `normalized_group=null`, unknown availability/final
fees/baggage. They cannot establish a verified budget or payable total. Fixed
refresh cannot substitute another itinerary. Compatible retention/cache reuse
preserves original times; new party/date/constraint scope cannot inherit stale
prices. Unsupported scopes, blocking and missing source proof remain explicit.

## Published price knowledge (018)

Default composition wraps the HTTP data port with `KnowledgeTravelDataPort`. Missing hotel,
transfer and flight prices use private POST `/v1/pricing/documents`: gateway search → fetched
pages → the agent's existing singleton model. At most three alternative candidates per component
are extracted in one batch; the planner receives ordinary components, without search orchestration.
The [price-document models](../backend/app/pricing/documents.py) and
[agent enrichment adapter](../backend/app/infrastructure/knowledge_travel_data.py) define bounds.
Each price document carries `delivery=live_fetch|cache_reuse` (default `live_fetch`); page cache
reuse preserves `retrieved_at` and remains cache reuse when a new model batch interprets it.

Validated prices use provider_id `knowledge-prices`, outcome `reference`, normalized_group null.
Copied page proof binds amount/currency, property or airport/city, unit and direction. Ranges retain
both endpoints; nightly references with unstated room scope retain unit `unknown` and literal source
labels. No conversion, nightly/return multiplication or inventory confirmation occurs. Source URLs,
original times and 30-minute expiry remain visible. Raw pages and rejected model output stay private.

Refresh jobs allow 240 seconds/work cap235 including shared model queue/backoff. Extraction shares
the same 10/60-second limiter, serialization and retries as intent extraction, without re-extracting
intent. Inference maintenance skips interpretation; specialized non-model sources remain available.
External-data-off blocks search/fetch independently. Earlier002 no-model results remain historical.
Demo invoice amounts remain explicitly fictional and independent of these published references.

## Country discovery and no-airport candidates (021)

`travel-data.v1` PlaceRef/PlaceData adds optional `scope: locality|country` (default
`locality`). GeoNames PCLI keeps country scope across the gateway; country searches do
not use a local-radius Overpass query at the country's centroid. Only explicit country
scope can use an indexed Booking country path as country-level relevance; city searches
still require city relevance. This does not verify a property address or availability.

Indexed hotel candidates require neither an airport nor a provider-specific pricing ID.
Up to3 source-bound alternatives carry independent evidence; lodging and dependent
transfer facts are not copied between properties. `TripOption` persists optional
`country_code`, `destination_scope` (default locality) and `destination_aliases` (default
empty) so refresh preserves non-catalogue geography. Older unknown places are resolved
when refreshing. All candidates still receive the separate020 illustrative demo range.
The current browser worker is skipped for missing IATA; its contract is unchanged.
HTTP errors report unavailable, actual deadlines/timeouts report timeout. Empty search
events report warning rather than successful collection.

## Demo invoice and payment (010)

`POST /api/sessions/{id}/checkout/quote` takes `{option_id, expected_revision}` after selection.
It returns a signed15-minute context-bound quote_id, currency matching demo_pricing (EUR before020), pricing_basis=synthetic_demo,
line_items, subtotal_minor, service_fee_minor, total_minor, expiry, original source_total if any,
and explicit unknown real conditions. All invoice amounts are fictional server-calculated integer
cents, separate from real quotes/budget. Taxes are included in fictional tariffs; demo fee2.5%.

`POST /api/sessions/{id}/checkout` additionally accepts `{quote_id, expected_revision}`. Server
validates current selection, idle session, revision, expiry/signature and exact source/intent scope
before idempotent receipt lookup. Changed sources invalidate a quote even without revision change.
Legacy `{option_id}` remains supported. Errors409 provide safe `{code,message}` where applicable.
Quoted receipts add `demo_payment` with status=simulated_paid, paid_amount_minor and reviewed invoice;
real estimated_total and availability are unchanged. No card data is accepted/transmitted/stored.
UI fixed fake card1234…3456 is presentation only. Server restart invalidates unspent process-signed
quotes; refreshing a quote recovers. Receipt persistence remains existing SQLite checkout storage.


## Complete illustrative prices (020)

`TripOption.demo_pricing` is separate from factual `pricing_summary` and component quotes.
The additive field contains complete decimal-string component/fee/total ranges, scope,
comparable-or-synthetic provenance and a server-calculated example in integer minor units.
See [exact DTO and calculation contract](DEMO_PRICING_CONTRACT.md).
Old sessions and previous plans receive it during read-only projection using each plan's own
intent. No new endpoint, provider call or migration. Checkout quotes use the same example
amounts and currency, while retaining their existing `synthetic_demo` classification, expiry
and source/revision binding. Demo estimates do not change factual budget, availability,
normalized prices or failed constraints. Legacy compatibility amounts still describe facts.

## Independent stay and flight dates (022)

`TravelIntent` and private `travel-data.v1` `DataIntent` add paired nullable
`flight_departure_from`/`flight_departure_to` (ISO local calendar dates). Both null
retain legacy same-day departure. An explicit window contains one to three dates,
within three days before `date_from`, ending no later than check-in. `date_from` /
`date_to` still describe hotel check-in / check-out; seven nights never include
the outbound travel days. Changing only the window preserves stay dates; changing
check-in shifts existing relative offsets unless the window is explicitly patched.

The gateway searches at most three dates, latest first, under its existing whole
operation deadline. Private `flight-search.v1` constraints add optional
`arrival_date`, omitted when null for legacy hash compatibility. It filters the
outbound local arrival day only; return departure remains `date_to`. This is a
search condition, not proof of time zones, connection feasibility or hotel check-in.
Actual quoted flight departure may precede hotel check-in and must belong to the
window; its context hash, fixed refresh and document research use that same date.
`flight.details.departure_window` describes the requested search dates and arrival
day; `selected_departure_date` is present only when a source observation supplies
the date. UI renders the search window separately from hotel nights and source facts.
No new endpoint, service, inference batch or database migration is introduced.
