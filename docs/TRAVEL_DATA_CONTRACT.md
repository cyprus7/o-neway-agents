# Travel data gateway v1

Pure contract location: backend/app/domain/travel_data.py. HTTP base is operator configuration, never a command field. Default Compose base http://provider-gateway:8091.

| Operation | Endpoint | Whole deadline maximum |
|---|---|---:|
| resolve_place | POST /v1/places/resolve | 8s |
| weather | POST /v1/weather | 18s |
| search | POST /v1/search | 22s |
| inventory | POST /v1/inventory | 45s |
| refresh_inventory | POST /v1/inventory/refresh | 45s |
| regional_events | POST /v1/events | 12s |
| capabilities | GET /v1/capabilities | 2s |

Request and response schemas are Pydantic extra=forbid. Body caps initially256KiBrequest/2MiBresponse; the final contract constants and OpenAPI are executable truth. Deadline includes semaphore queue, source operation and body consumption. Fingerprint binds operation and traveler/place input. No arbitrary source navigation, filesystem path or query execution endpoint. Invalid/late/oversized/mismatched responses are unavailable; credentials and raw exception bodies never enter public diagnostics. There is no automatic direct-provider fallback.

Provider health/readiness inspect local process/cache/catalog configuration without upstream dispatch. Capabilities report policy, catalog coverage/source dates, configured optional sources and degraded status. Agent health remains session/storage health; data availability is a separate capability, not invented inventory.
