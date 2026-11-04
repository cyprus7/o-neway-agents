# Private flight-search.v1

`POST /v1/search`; `GET /health` and readiness. Private bounded HTTP only.

Request fields: version, request_id, aware UTC deadline_at, original request_context and request_context_hash, separately typed flight constraints, optional target_itinerary_id. Airport codes are structured, not URLs. No prompt/session/lodging content/key. Search hash covers all context/constraint/target/provider policy; context hash retains existing trip binding.

Response echoes transport identity/hash, finite outcome/diagnostic and actual-dispatch facts. Successful data contains independently read scope, bounded observed itinerary candidates/selected identity, both legs, source price basis/unit/currency, source URL and original times. Matching echoes alone cannot validate scope. Service model/field spelling is shared in lightweight flight_search.models before gateway implementation.

Limits: request <=64KiB, response <=256KiB, one browser context, bounded pending requests/candidates, <=35s inclusive of queue/work/cleanup, no hidden retries. Gateway deadline remains45s. Disconnect/last-waiter cancellation closes browser work; no orphan tasks. Source URL response must be Google HTTPS with reviewed path/query, no credentials or navigation authority.

Unsupported party/cabin/currency/airport, changed controls/year, challenge/login, no matching/fixed route, queue saturation and timeout remain finite typed outcomes. Missing values never become zero. Error envelopes cannot carry a price. Browser timeout means external dispatch unknown unless independently reported; cache reuse never creates a source-dispatch span.

Initial readback can use a return candidate after an outbound was selected only when the source explicitly states round-trip total and passenger basis. It remains a lower-bound REFERENCE with final supplier offer/availability/fee completeness/baggage unknown. Runtime does not visit sellers or submit bookings.
