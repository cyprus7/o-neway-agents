# Public source policy and scope

Local PoC; no travel API key, new account or paid subscription. Source-specific browser and
HTTP adapters are bounded. These are public observations, never an inventory hold.

| Provider | Enabled access | Numeric scope | Cache / limitations |
|---|---|---|---|
| AEGEAN public route pages | Ordinary HTTPS GET, structured SSR histogram | Exact airports and departure date, single-adult one-way baseline; two dated legs may form a reference | 10 min cache, original source timestamp no older than48h; taxes/party/baggage unverified |
| Ryanair Fare Finder | Public HTTPS GET, exact roundTripFares dates and airports | Per-adult public return baseline, both leg prices must equal source summary | 10 min cache; no airport substitution; empty/challenged responses remain failures |
| Google Hotels | Ephemeral sandboxed local Chromium, ordinary visible UI | Actual selected dates/traveler controls, detailed Stay total, explicit room type/capacity; raw reference | 15 min reference TTL, no historical estimate retention; EUR/1–2 adults/no children/one room/≤30 nights |
| Apollo Athens | Owner's published tariff page HTTPS GET | Seasonal single/double/triple room-night tariff | 6h source cache, calendar year and actual availability unknown; no normalized stay total |
| Saruhan Istanbul | Owner's published tariff page HTTPS GET | Seasonal double/twin room-night range | 6h source cache, year/season boundary and availability limitations explicit |
| Booking.com | Disabled browser recipe | Indexed links remain navigation/evidence | No permission or account/protection bypass |
| Kayak city guides | Disabled automated extraction | No runtime prices | Published terms restrict extraction without permission |
| SerpApi | Existing optional credentialed integration | Legacy search observations | Not needed by keyless routes; does not establish verified group totals |

AEGEAN is a public airline fare-promotion page, not an authenticated booking API. Ryanair's
published terms distinguish personal use from commercial automated extraction/comparison;
the local hackathon experiment is not authorization for a commercial scraping product. Keep
this deployment local. Provider data and source pages are not agent instructions. See the [pricing contract](API_CONTRACT.md) and [interpretation guide](pricing-feasibility.md).

Only explicit scope evidence can normalize a quote for all travelers. Child/infant prices,
multi-room occupancy, direct-flight constraints and special hotel requirements are never
inferred from a single-adult advertisement. Unsupported inputs keep raw references or return
`unsupported_party`, `currency_mismatch`, `no_offers`, `blocked`, `timeout` or `parse_error`.
CAPTCHA/login/protective challenges stop the recipe. No stealth, account, payment, cookie
profile reuse, hidden provider key or guessed tariff is used.
