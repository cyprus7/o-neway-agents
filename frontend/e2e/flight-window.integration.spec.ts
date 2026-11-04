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
  const response = await page.request.get(`/api/sessions/${id}`); expect(response.status()).toBe(200); return response.json();
}

test('Bora Bora keeps seven hotel nights when the departure window changes and after refresh/demo payment', async ({ page }) => {
  await page.goto('/'); await chat(page, true);
  const composer = page.getByRole('textbox', { name: 'Расскажите о путешествии' });
  await expect(composer).toBeEnabled();
  await composer.fill('Из Казани на Бора-Бора: проживание с 31 декабря 2026 по 7 января 2027, семь ночей, двое взрослых, до 5000 EUR. Вылет 28–30 декабря, прибытие к 31 декабря. Пересадки допустимы, тихий отель с завтраком.');
  await composer.press('Enter'); await expect(page.locator('.option-card')).toHaveCount(3);
  const initial = await current(page);
  expect(initial.intent).toMatchObject({ origin: 'Казань', destination: 'Бора-Бора', date_from: '2026-12-31', date_to: '2027-01-07', nights_min: 7, nights_max: 7, adults: 2, currency: 'EUR', flight_departure_from: '2026-12-28', flight_departure_to: '2026-12-30' });
  const initialStay = initial.options[0].demo_pricing!.components.find(item => item.kind === 'stay')!;
  await chat(page, false);
  const brief = page.getByRole('region', { name: 'Параметры поездки' });
  await expect(brief).toContainText(/Проживание: 31\sдек.*7\sянв.*, 7 ночей/);
  await expect(brief).toContainText(/Вылет: 28\sдек.*30\sдек/);
  await brief.screenshot({ path: test.info().outputPath('bora-stay-flight-window.png') });
  await chat(page, true); await composer.fill('Вылет только 29–30 декабря, остальное без изменений.'); await composer.press('Enter');
  await expect.poll(async () => (await current(page)).intent.flight_departure_from).toBe('2026-12-29');
  await expect.poll(async () => (await current(page)).options.length).toBe(3);
  let session = await current(page);
  expect(session.intent).toMatchObject({ ...initial.intent, flight_departure_from: '2026-12-29' });
  expect(session.options.every(option => option.stay.source_url?.startsWith('https://www.booking.com/hotel/pf/'))).toBe(true);
  expect(session.options[0].demo_pricing!.components.find(item => item.kind === 'stay')!.value).toEqual(initialStay.value);
  expect(session.options[0].demo_pricing!.components.find(item => item.kind === 'stay')!.example_amount_minor).toBe(initialStay.example_amount_minor);
  await chat(page, false);
  await expect(brief).toContainText(/Вылет: 29\sдек.*30\sдек/);
  const sources = session.options.map(option => option.stay.source_url).sort();
  const revision = session.revision;
  await page.getByRole('button', { name: 'Проверить все цены', exact: true }).click();
  await expect.poll(async () => (await current(page)).revision, { timeout: 15_000 }).toBeGreaterThan(revision);
  session = await current(page);
  expect(session.options.map(option => option.stay.source_url).sort()).toEqual(sources);
  expect(session.intent.date_from).toBe('2026-12-31'); expect(session.intent.date_to).toBe('2027-01-07'); expect(session.intent.nights_min).toBe(7);
  const stay = session.options[0].demo_pricing!.components.find(item => item.kind === 'stay')!;
  expect(stay.value).toEqual(initialStay.value); expect(stay.example_amount_minor).toBe(initialStay.example_amount_minor);
  await page.getByRole('button', { name: /Посмотреть маршрут/ }).first().focus(); await page.keyboard.press('Enter');
  const itinerary = page.getByRole('region', { name: 'Выбранный маршрут' });
  await expect(brief).toContainText(/Вылет: 29\sдек.*30\sдек.*прибытие 31\sдек/);
  await expect(itinerary.locator('.checkout-summary')).toContainText('7 ноч.');
  if (session.options[0].flight.details?.departure_window) {
    await expect(itinerary.getByRole('region', { name: 'Даты поиска перелёта' })).toContainText(/29\sдек.*30\sдек/);
  }
  await itinerary.getByRole('button', { name: /Забронировать в демо/ }).click();
  const dialog = page.getByRole('dialog'); await expect(dialog).toContainText(/Все суммы.*вымышленные/);
  await dialog.getByRole('button', { name: /Оплатить .* в демо/ }).click();
  await expect(page.getByRole('heading', { name: 'Демо-оплата прошла' })).toBeFocused();
  const receiptsResponse = await page.request.get(`/api/sessions/${session.id}/receipts`);
  expect(receiptsResponse.status()).toBe(200); const receipt = (await receiptsResponse.json()).items[0];
  expect(receipt.demo_payment.paid_amount_minor).toBe(session.options[0].demo_pricing!.example_total_minor);
  expect(receipt.demo_payment.invoice.line_items.find((item: { key: string }) => item.key === 'stay').amount_minor).toBe(stay.example_amount_minor);
  expect(receipt.itinerary.intent.date_from).toBe('2026-12-31'); expect(receipt.itinerary.intent.date_to).toBe('2027-01-07');
  await page.keyboard.press('Escape');
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: test.info().outputPath('bora-window-itinerary.png'), fullPage: true });
});
