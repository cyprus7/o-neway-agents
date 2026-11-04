import { expect, test } from '@playwright/test';
import { installJourney, sessionFixture } from './fixtures/journey';
import { demoSession } from './fixtures/demo-pricing';

test('stay nights and departure search window are separate without promising flight arrival', async ({ page }) => {
  const session = structuredClone(demoSession);
  session.intent = { ...session.intent, date_from: '2026-12-31', date_to: '2027-01-07', nights_min: 7, nights_max: 7, flight_departure_from: '2026-12-28', flight_departure_to: '2026-12-30' };
  session.options[0].demo_pricing!.scope_label = '2 взрослых · 7 ночей';
  session.options[0].flight.details = { departure_window: { date_from: '2026-12-28', date_to: '2026-12-30', arrival_date: '2026-12-31' } };
  await installJourney(page, session); await page.goto('/');
  const brief = page.getByRole('region', { name: 'Параметры поездки' });
  await expect(brief).toContainText(/Проживание: 31\sдек.*7\sянв.*, 7 ночей/);
  await expect(brief).toContainText(/Вылет: 28\sдек.*30\sдек.*прибытие 31\sдек/);
  await expect(brief).toContainText('дата поиска, не подтверждённый рейс');
  await expect(brief).not.toContainText('10 ночей');
  await page.getByRole('button', { name: /Посмотреть маршрут/ }).focus(); await page.keyboard.press('Enter');
  const itinerary = page.getByRole('region', { name: 'Выбранный маршрут' });
  await expect(itinerary.getByRole('region', { name: 'Даты поиска перелёта' })).toContainText('параметры поиска, не подтверждение рейса');
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await brief.screenshot({ path: test.info().outputPath('separate-stay-flight-dates.png') });
});

test('sourced selected departure remains distinct from arrival search date and preserves source description', async ({ page }) => {
  const session = structuredClone(demoSession);
  session.options[0].flight.description = 'Наблюдение источника: вылет 29 декабря 12:00, прибытие 31 декабря 08:00, две пересадки, 2640 минут.';
  session.options[0].flight.details = { selected_departure_date: '2026-12-29', departure_window: { date_from: '2026-12-28', date_to: '2026-12-30', arrival_date: '2026-12-31' }, flight_route: { outbound: { departure_date: '2026-12-29', arrival_date: '2026-12-31', stops: 2, duration_minutes: 2640 } } };
  await installJourney(page, session); await page.goto('/'); await page.getByRole('button', { name: /Посмотреть маршрут/ }).click();
  const flight = page.getByRole('region', { name: 'Даты поиска перелёта' });
  await expect(flight).toContainText(/Дата вылета в источнике: 29\sдек/);
  await expect(flight).toContainText(/прибытие 31\sдек/);
  const part = page.locator('.itinerary-part').filter({ has: flight });
  await part.getByText('Описание источника', { exact: true }).click();
  await expect(part).toContainText('две пересадки, 2640 минут');
});

test('legacy null departure window retains the existing date display', async ({ page }) => {
  const session = structuredClone(sessionFixture);
  session.intent.flight_departure_from = null; session.intent.flight_departure_to = null;
  await installJourney(page, session); await page.goto('/');
  const brief = page.getByRole('region', { name: 'Параметры поездки' });
  await expect(brief).toContainText(/10\sнояб.*15\sнояб/);
  await expect(brief.getByText(/Проживание:|Вылет:/)).toHaveCount(0);
});
