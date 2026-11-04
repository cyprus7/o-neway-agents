# One Request / Один запрос

Version 1 — local PoC, Russian language. Owned source: `design/tokens.css`; component recipes: `design/components.css`; visual reference: `design/reference.html`; 390px responsive wrapper: `design/mobile-preview.html`.

## Purpose and visual idea

One useful travel brief becomes three understandable options. The interface should feel like a thoughtful travel editor working beside the user. Warm paper, deep green, compact practical controls, generous editorial headlines. Keep the comparison and evidence visible while the conversation continues. Avoid marketing pages, gradients, glass panels, oversized empty dashboards, or technical agent names in product copy.

This is an original PoC treatment, not an assertion of official Ostrovok brand assets. Logo: simple geometric journey symbol + **один запрос** wordmark. Product secondary label: «Путешествия с Островком». The favicon is a separate **1!** project mark, retaining the lime/blue concept from the user's supplied ICO (preserved as `design/favicon-reference.ico`). After inspecting the [official Ostrovok wordmark](https://ostrovok.ru/) on 2026-10-25, the project mark uses separate bold glyphs and a distinct exclamation dot: its 32-unit SVG has a 7-unit gap between the numeral and punctuation, so they remain distinct at 16px. Source: `frontend/public/favicon.svg`; preferred SVG plus 16/24/32/48/64/128/256px ICO fallback. Rebuild the ICO with the authoring-only Pillow script `scripts/build_favicon.py`; no rendering library is added to the app runtime. No externally hosted fonts or images required. System sans covers Cyrillic reliably; Georgia gives headlines an editorial voice.

## Foundations

Import `tokens.css` before application styles. All colours, control sizes, spacing and corner radii must come from tokens. Prefer `--color-canvas`, `--color-surface`, `--color-ink`, `--color-muted`, `--color-brand`, `--color-line` in every component. `components.css` is a reusable recipe sheet; app may map its own classes to those tokens.

| Role | Token | Value | Use |
| --- | --- | --- | --- |
| Page | `--color-canvas` | `#f5f3ed` | Main workspace |
| Paper | `--color-surface` | `#fffefa` | Cards and composer |
| Ink | `--color-ink` | `#202e2b` | Headings and key amounts |
| Body | `--color-text` | `#31403b` | Paragraphs |
| Secondary | `--color-muted` | `#66716a` | Metadata with readable contrast |
| Action | `--color-brand` | `#22574b` | Primary CTA, selected option |
| Soft green | `--color-brand-soft` | `#e4ede4` | Selection and verified chip |
| Terracotta | `--color-accent` | `#b8502e` | Small editorial markers |
| Divider | `--color-line` | `#dcded3` | Non-interactive boundaries |
| Warning | `--color-warning` | `#855513` | Missing prices, estimates |
| Focus | `--color-focus` | `#a3472a` | Three-pixel keyboard outline |

Small metadata: 11px only for uppercase eyebrows or compact chips; normal metadata/body: 13–15px. Important instructions and missing-price copy use at least 13px. Headlines: Georgia 30px; intro 40–56px; line height 1.08–1.22. Body 15px/1.55. Amounts use tabular numerals. Limit paragraph width to ~65 characters.

Space uses a 4px base: 4, 8, 12, 16, 20, 24, 32, 40, 48, 64. Card inner padding 20px; section spacing 32px; compact stack gap 12px. Cards radius 20px; inputs/buttons 10px; chips pill. Only very light shadows. Borders establish grouping.

## Information architecture

Desktop at ≥1120px: full-width 76px masthead, 352px fixed-width conversation column left, flexible main workspace right, max workspace content 1216px. Conversation column is warm paper with a right divider. Main padding 32px. Conversation bottom composer stays visible; message history scrolls independently. Keep at least 280px for cards. At 1120–1280px shrink sidebar to 304px and main padding to 24px; comparison remains three columns if width allows, otherwise two with the final option full width.

Tablet at 760–1119px: conversation is a full-width panel above results, collapsible after a successful search with visible «Уточнить запрос» button; cards form two columns. Mobile below 760px: single column, 20px horizontal padding, masthead 64px, headline 34–40px. Conversation comes first and is collapsible; composer spans width. Cards stack vertically; itinerary rows stack. Every action has at least 44×44px hit area. No horizontal page overflow at 320px.

1. Masthead: journey symbol, wordmark, «Путешествия с Островком», small «Локальный PoC» badge, optional new-trip action.
2. Conversation: «Ваше путешествие» eyebrow; «Начнём с желания» / «Давайте уточним детали» title; a clear user message; assistant reply; composer with label and useful example prompts.
3. Workspace header: eyebrow «Ваш следующий маршрут»; editorial headline «Хорошая поездка начинается с одного запроса.» in empty state; after results «Три способа сменить обстановку». One concise explanatory line, request chips.
4. Execution activity: small real event list from `events`, one semantic label per step, live running state. Expandable details under «Как мы искали». Never fabricate timer-based activity.
5. Results: count label «Варианты поездки» + sort context «Сначала — лучшее совпадение». Three equal destination cards.
6. Selected itinerary: selected destination header, why it fits and tradeoffs, flight/hotel/transfer rows, evidence list, clearly simulated checkout.

## Components and states

### Conversation and composer

Assistant messages left aligned with small brand mark, no bubble for long body; user messages have pale green rounded bubble. Speaker label visible. Preserve whitespace, escape all content. Composer uses a real labelled textarea, 3–4 rows; helper «Например: море на неделю, без пересадок, до 200 000 ₽ на двоих». Submit: green square arrow icon button with accessible label «Отправить запрос». Show «Уточнить маршрут» or «Найти варианты» on larger CTA. Enter sends; Shift+Enter inserts newline, and helper communicates this if implemented. While busy, prevent duplicate submissions, retain typed text, display real queue/running state. Errors appear adjacent to composer with retry action; do not discard the previous plan.

Empty-state suggestions are full-width or wrapping 44px minimum buttons, e.g. «На море вдвоём», «Выходные в новом городе», «В горы с семьёй». Their click populates the composer, allowing edits before sending. If directly sending, accessible label must make that clear. Clarifying question is a visible assistant message, not a blocking modal. Assumptions appear as «Приняли за основу» details and can be changed in conversation. Replanning shows actual `last_changes` in a compact «Обновили маршрут» notice.

### Destination card

Top media ratio ~1.8:1. Use supplied image only if provider actually supplies a safe URL. Otherwise local CSS/SVG destination artwork, labelled decorative; avoid fake hotel imagery. A clear selected checkmark and border supplement colour. Location eyebrow, destination heading, one-line subtitle, 2–3 reasons, climate chip with semantic provenance, score only if meaningful. Separate footer for price and CTA. Footer aligns consistently across all three cards.

Price rules: full observed total → «От 180 000 ₽» plus exact scope and checked timestamp; partial total → «Известная часть: 85 000 ₽» plus amber «Не все цены доступны»; unavailable total → «Стоимость уточняется». No absent value becomes zero or a made-up package total. `estimated` components explicitly say «Оценка» beside the number. Climate for distant dates says «Обычно в этот период», never a forecast icon implying live data. Weather unavailable gets plain text, not placeholder temperature.

CTA default «Посмотреть маршрут»; selected «Маршрут выбран» plus check icon. Selection remains operable via a button, with `aria-pressed`. Cards should not contain nested interactive elements inside one huge button. Hover gently strengthens border, no large lift. Score, source confidence and constraints should be textual or use icon plus text.

### Itinerary and evidence

Rows: icon, semantic label «Перелёт» / «Проживание» / «Трансфер», title/description, price status. Desktop has three columns; mobile stacks price below description. Clicking a real external source is clearly a link. Source row contains provider, retrieved time, freshness text («Получено сейчас», «Из кеша», «Из справочника», «Источник недоступен»). Links open using safe URL validation. Search result links are labelled «Результат поиска», never «Билет доступен» without verified inventory.

No source / partial inventory remains useful: destination rationale visible, missing inventory highlighted, external search links available when safe. Separate «Что стоит учесть» for risks, visa/time/date uncertainties. Hard-constraint failures show warning or unavailable state and disable checkout with reason; never hide failed constraints inside an unlabelled score.

### Checkout

The explicit user request of 2026-10-25 adds a complete simulated payment journey. Keep actual PricingSummary visible, then a separate **«Демо покупки»** invoice with fictional transport/accommodation/transfer prices, service fee, included demo taxes, group scope and exact total. Never substitute these amounts into source prices or budget checks. CTA **«Забронировать в демо · [сумма]»** opens the reviewed breakdown and a prefilled, read-only fictional card (`1234 5678 9012 3456`, `12/30`, `123`). Card data is display-only; the server accepts no card fields or client amount. The explicit **«Оплатить [сумма] в демо»** action creates a persistent simulated receipt. Heading **«Демо-оплата прошла»** always accompanies **«Деньги не списаны. Билеты, отель и трансфер не забронированы.»** Invoice and receipt are visibly fictional. No gateway or real reservation is involved. Reject expired/changed invoices and failed constraints with review/retry guidance, retain the itinerary, trap and restore focus, and support Escape. See feature010.

### Loading, failure, empty results

Before first search show purposeful empty canvas with concise benefits, no fabricated cards. During initial fetch use 3 static skeleton blocks plus truthful current event; existing plan stays visible during replan and is labelled «Обновляем». Status region uses `aria-live="polite"`; don't reannounce entire message history each poll. Failure uses inline warm-red notice and retry. If no option satisfies hard constraints, explain constraints and offer editable suggested changes rather than invent alternatives. Demo fixture data, if ever used for an explicit demo, requires a persistent visible «Демонстрационные данные» label.

## Accessibility and implementation

- Document `lang="ru"`, one h1, ordered heading levels, landmarks for masthead / conversation / main, skip link. Icon-only controls get Russian accessible names. Decorative SVG has `aria-hidden="true"`.
- Normal text must reach 4.5:1 contrast; larger text and non-text controls 3:1. Weak decorative borders may not encode controls alone; input boundary uses `--color-line-strong` and focus is explicit. Do not put white text on orange or muted grey.
- Keyboard order follows visible layout; selected state has both text/check icon and border; messages and agents statuses never rely on colour alone. Focus remains in composer after submit; navigating an itinerary may move focus to its labelled heading using `tabindex=-1`.
- Respect `prefers-reduced-motion`; transitions ≤220ms, no parallax, no automatic scrolling while user is reading old messages. Avoid continuous shimmer; a static skeleton is adequate.
- Format money with `Intl.NumberFormat('ru-RU')`; dates with `Intl.DateTimeFormat('ru-RU')`; preserve source currency. Display absolute retrieval date/time when freshness matters.
- Tokens and assets are local and deterministic. Do not introduce tracking, remote web fonts, cloud design calls, or browser secrets.

## Local design tooling status

Brief collected and confirmed using the local MCP. The running Docker container exposes port 7456. Its agent inventory reports no available Local Codex, and the container has no `codex` executable. Therefore no design generation run was commissioned, and no cloud model was enabled. This bundle is authored by the separate Codex design agent and can be reviewed in the local workspace. A copy was written through local daemon file APIs to project `one-request-travel-local`. The daemon raw-file URL requires its existing Bearer authorization; don't embed a token in a browser URL. The root agent integrates and previews these files through the application's own local server.

Browser QA performed against a temporary, loopback-only static server: desktop rendering, 390px iframe viewport, stacked itinerary, selection updates, focus on selected itinerary, and simulated-checkout dialog. No horizontal overflow was visible in the mobile render. Token contrast measured: muted on paper 5.03:1; muted on canvas 4.58:1; subtle on paper 4.56:1; input boundary on paper 3.01:1. The temporary server was stopped after review.

## Integration acceptance

Review at 1440px and 390px widths; all comparison cards readable; composer remains usable; every missing-price distinction visible; sources have timestamps; no checkout text implies a real transaction. Verify keyboard focus, unknown budget/date states, long Russian messages and destination names, reduced motion, backend failure, no options, and a second conversational replan.
