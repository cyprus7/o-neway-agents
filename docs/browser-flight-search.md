# Browser flight search

The application uses a separate `flight-search` service behind the existing provider
gateway. The agent still depends only on `TravelDataPort`; it never imports
Playwright or calls a travel provider. The gateway starts flight research while
lodging discovery is running, then merges the independent results.

```text
agent -- travel-data.v1 --> provider-gateway
                            +-- flight client --> flight-search --> Google Flights
                            `-- lodging discovery --> hotel price-worker
```

The first reviewed source is Google Flights. Yandex is reserved as a possible
replacement and requires its own recipe and observed validation before use.
Only supported airport mappings, adult-only economy/EUR round trips are eligible.
Unsupported scope is explicit. The private API is
[`flight-search.v1`](FLIGHT_SEARCH_CONTRACT.md).
It accepts structured scope and a UTC deadline, never arbitrary URLs, prompts,
provider credentials, browser profiles or session data.

The recipe uses a fresh sandboxed Chromium context. It checks visible controls,
full-year travel dates, passenger count, currency and displayed airport codes.
After selecting an outbound flight it inspects return candidates whose source
labels explicitly state a round-trip group price. Departure and arrival dates,
local times, duration, stops and carriers come from those observed rows.
No vendor booking, account, payment or challenge-solving action is performed.

The displayed amount remains a **from-price reference**. The final supplier offer,
baggage, availability, mandatory-charge completeness and connection feasibility
are unverified. This amount never enters a verified trip total or budget pass.
Outbound and return headlines are never added together. A stable itinerary
identity excludes price, page position and observation time. Fixed refresh cannot
substitute another itinerary; compatible failed refresh retains the old source
times and a warning. A changed scope invalidates incompatible evidence.
An unknown observation without an itinerary identity may discover its first
route during refresh; an existing fixed identity never grants substitution.

The whole service operation, including queueing and bounded cleanup, has a
35-second deadline; the gateway retains its existing inventory45/pricing42-second
budgets. One browser context and a finite pending queue prevent unbounded work.
Disconnect and last-waiter cancellation close abandoned work. Gateway cache keys
include complete flight preferences, target identity and provider policy.
`WEB_FALLBACK_ENABLED=0` blocks flight dispatch as well as other external data.
Structured lookup and refresh make no inference calls.

Compose starts the private service automatically. It publishes no host port,
mounts no persistent volume and receives only the explicit tracing environment.
The sandbox uses the existing reviewed seccomp file, non-root UID10001,
no-new-privileges, dropped capabilities, one CPU, 384MiB memory and 64MiB shared
memory. The hotel worker, search and embedding services remain independent.

Traces follow [the existing privacy rules](tracing.md): an unset endpoint creates
no SDK/exporter/middleware; only the owned gateway-to-flight HTTP hop propagates
`traceparent`. Browser requests to Google carry no trace context or baggage.
Finite flight source/queue/client/cache outcomes omit URLs, route IDs, travel
content, payloads and exception text. Local Jaeger remains memory-only.

Source references:
[Google Flights help](https://support.google.com/travel/answer/2475306?hl=en),
[Yandex flight search help](https://yandex.ru/support/travel/ru/avia/find-and-buy).
