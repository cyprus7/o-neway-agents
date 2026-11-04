import { expect, test } from '@playwright/test';
import type { Page } from '@playwright/test';
import type { Session } from '../src/types';

async function chat(page: Page, open: boolean) {
  if (test.info().project.name !== 'actual-mobile') return;
  const close = page.getByRole('button', { name: 'Закрыть чат' });
  if (open && !await close.isVisible()) await page.getByRole('button', { name: 'Обсудить поездку' }).click();
  if (!open && await close.isVisible()) await close.click();
}
async function current(page: Page): Promise<Session> {
  const id = await page.evaluate(() => localStorage.getItem('one-request-session'));
  const response = await page.request.get(`/api/sessions/${id}`);
  expect(response.status()).toBe(200); return response.json();
}

// Actual app/worker/SSE/HTTP gateway/fallback/demo calculator. Only intent patches,
// geocoder results and public search-link inputs are finite offline fixtures.
test('Kazan to Maldives replan without IATA preserves one-night scope and supports demo checkout', async ({ page }) => {
  await page.goto('/'); await chat(page, true);
  const composer = page.getByRole('textbox', { name: 'Расскажите о путешествии' });
  await expect(composer).toBeEnabled();
  await composer.fill('Из Казани в Ларнаку с 31 декабря по 1 января на одну ночь, двое взрослых, до 100000 RUB. Тихий отель с завтраком, без хостелов.');
  await composer.press('Enter');
  await expect(page.locator('.option-card')).toHaveCount(3);
  const initial = await current(page);
  expect(initial.intent.origin).toBe('Казань');
  expect(initial.intent.destination).toBe('Ларнака');
  expect(initial.intent.date_from).toMatch(/-12-31$/); expect(initial.intent.date_to).toMatch(/-01-01$/);
  expect(Date.parse(initial.intent.date_to!) - Date.parse(initial.intent.date_from!)).toBe(86_400_000);
  await chat(page, false);
  await page.getByRole('button', { name: /Посмотреть маршрут/ }).first().click();
  await expect(page.getByRole('region', { name: 'Выбранный маршрут' })).toBeVisible();
  await chat(page, true); await composer.fill('А если Мальдивы?'); await composer.press('Enter');
  await expect.poll(async () => {
    const session = await current(page);
    return session.intent.destination === 'Мальдивы' && session.revision > initial.revision && session.options.length > 0;
  }).toBe(true);
  let replanned = await current(page);
  expect(replanned.selected_option_id).toBeNull();
  for (const field of ['origin', 'date_from', 'date_to', 'nights_min', 'nights_max', 'adults', 'children_ages', 'budget_total', 'currency', 'hotel_requirements'] as const) {
    expect(replanned.intent[field]).toEqual(initial.intent[field]);
  }
  expect(replanned.intent.nights_min).toBe(1); expect(replanned.intent.nights_max).toBe(1); expect(replanned.intent.adults).toBe(2);
  expect(replanned.intent).toMatchObject({ ...initial.intent, destination: 'Мальдивы' });
  expect(replanned.options.every(option => option.demo_pricing?.schema_version === 'demo-pricing.v1')).toBe(true);
  expect(replanned.options.every(option => option.demo_pricing?.components.length === 3)).toBe(true);
  expect(replanned.options.every(option => option.stay.source_url?.startsWith('https://www.booking.com/hotel/mv/'))).toBe(true);
  expect(new Set(replanned.options.map(option => option.stay.title)).size).toBe(3);
  expect(new Set(replanned.options.map(option => option.stay.source_url)).size).toBe(3);
  expect(replanned.options.every(option => option.country_code === 'MV' && option.destination_scope === 'country' && option.destination_aliases?.includes('maldives'))).toBe(true);
  expect(replanned.options.every(option => option.flight.quote?.availability_verified !== true && option.stay.quote?.availability_verified !== true)).toBe(true);
  await chat(page, false);
  await expect(page.locator('.option-card')).toHaveCount(replanned.options.length);
  await expect(page.locator('.option-card').getByText('Ориентировочно · демо', { exact: true })).toHaveCount(replanned.options.length);
  expect(replanned.options.every(option => !initial.options.some(old => old.id === option.id))).toBe(true);
  await expect(page.getByRole('region', { name: 'Выбранный маршрут' })).toHaveCount(0);
  const sources = replanned.options.map(option => option.stay.source_url).sort();
  const revision = replanned.revision;
  await page.getByRole('button', { name: 'Проверить все цены', exact: true }).click();
  await expect.poll(async () => (await current(page)).revision, { timeout: 15_000 }).toBeGreaterThan(revision);
  replanned = await current(page);
  expect(replanned.options).toHaveLength(3);
  expect(replanned.options.map(option => option.stay.source_url).sort()).toEqual(sources);
  expect(replanned.options.every(option => option.country_code === 'MV' && option.destination_scope === 'country' && option.destination_aliases?.includes('maldives'))).toBe(true);
  expect(replanned.intent).toMatchObject({ ...initial.intent, destination: 'Мальдивы' });
  await expect(page.getByText('Обновление цен завершено', { exact: true }).first()).toBeVisible();
  await expect(page.locator('.option-card').getByText('Ориентировочно · демо', { exact: true })).toHaveCount(3);
  await page.locator('.option-card').first().screenshot({ path: test.info().outputPath('maldives-demo-card.png') });
  await page.getByRole('button', { name: /Посмотреть маршрут/ }).first().focus(); await page.keyboard.press('Enter');
  const itinerary = page.getByRole('region', { name: 'Выбранный маршрут' });
  await expect(itinerary).toContainText('Мальдивы');
  await expect(itinerary.locator('.itinerary-part .part-description > .component-price > .price-value-line > strong')).toHaveCount(3);
  await itinerary.getByRole('button', { name: /Забронировать в демо/ }).click();
  const dialog = page.getByRole('dialog');
  await expect(dialog).toContainText(/Все суммы.*вымышленные/);
  await expect(dialog.getByLabel('Номер карты')).toHaveValue('1234 5678 9012 3456');
  await dialog.getByRole('button', { name: /Оплатить .* в демо/ }).click();
  await expect(page.getByRole('heading', { name: 'Демо-оплата прошла' })).toBeFocused();
  await expect(dialog).toContainText('не забронированы');
  const receiptResponse = await page.request.get(`/api/sessions/${replanned.id}/receipts`);
  expect(receiptResponse.status()).toBe(200);
  const receipts = (await receiptResponse.json()).items;
  expect(receipts).toHaveLength(1);
  expect(receipts[0].demo_payment.paid_amount_minor).toBe(replanned.options[0].demo_pricing!.example_total_minor);
  expect(receipts[0].demo_payment.currency).toBe('RUB');
  await page.keyboard.press('Escape');
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: test.info().outputPath('maldives-replanned-itinerary.png'), fullPage: true });
});
