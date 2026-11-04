import { expect, test } from '@playwright/test';
import { installJourney, sessionFixture } from './fixtures/journey';
import { alignedInvoice, demoSession } from './fixtures/demo-pricing';

test('demo ranges replace repeated missing prices and retain scope, source basis and server invoice', async ({ page }) => {
  await installJourney(page, demoSession);
  await page.route('**/checkout/quote', route => route.fulfill({ json: alignedInvoice }));
  await page.goto('/');
  const card = page.locator('.option-card').first();
  await expect(card.getByText('Ориентировочно · демо', { exact: true })).toBeVisible();
  await expect(card.locator('.price-headline')).toHaveText(/779.*1\s230/);
  await expect(card).toContainText('Демо-диапазон пересекает бюджет 900 EUR.');
  await expect(card.getByText('Цены пока не найдены', { exact: true })).toHaveCount(0);
  const realConditions = card.locator('.condition-disclosure');
  await expect(realConditions).not.toHaveAttribute('open');
  await expect(card.getByText('Правила перевозчика не подтверждены.', { exact: true })).toBeHidden();
  await card.getByRole('button', { name: 'Посмотреть маршрут' }).click();
  const itinerary = page.getByRole('region', { name: 'Выбранный маршрут' });
  await expect(itinerary.locator('.itinerary-part .part-description > .component-price > .price-value-line > strong')).toHaveCount(3);
  await expect(itinerary.getByText('Цена пока не найдена', { exact: true }).first()).toBeHidden();
  const stay = itinerary.locator('.itinerary-part').filter({ hasText: 'Где остановиться' });
  const description = stay.getByText('Демонстрационные данные. Условия и наличие неизвестны.', { exact: true });
  await expect(description).toBeHidden();
  await stay.getByText('Описание источника', { exact: true }).click(); await expect(description).toBeVisible();
  await expect(itinerary.getByText('Реальные цены не проверены', { exact: true })).toBeHidden();
  await itinerary.getByText('Условия реальной поездки', { exact: true }).click();
  await expect(itinerary.getByText('Реальные цены не проверены', { exact: true })).toBeVisible();
  await stay.getByText('Основание оценки', { exact: true }).click();
  await expect(stay).toContainText('Демо-оценка по сопоставимой цене источника');
  await expect(stay.getByRole('link', { name: 'Источник сравнения' })).toHaveAttribute('href', 'https://hotel.example/rates');
  await expect(stay).toContainText('2024');
  const breakdown = itinerary.locator('.checkout-summary').getByText('Состав оценки и допущения', { exact: true });
  await breakdown.click();
  await expect(itinerary.locator('.checkout-summary')).toContainText('Демо-сбор:');
  const pay = itinerary.getByRole('button', { name: /Забронировать в демо/ });
  await expect(pay).toContainText('1');
  await pay.focus(); await page.keyboard.press('Enter');
  await expect(page.getByRole('dialog')).toContainText(/1\s004,50/);
  await expect(page.getByRole('dialog')).toContainText('Все суммы вымышленные');
  await page.keyboard.press('Escape'); await expect(pay).toBeFocused();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: test.info().outputPath('demo-estimate.png') });
  await card.screenshot({ path: test.info().outputPath('demo-estimate-card.png') });
});

test('demo estimates keep factual failures and operational failures prominent without verifying unknowns', async ({ page }) => {
  const session = structuredClone(demoSession);
  session.options[0].constraint_checks.push({ constraint: 'Прямой рейс', status: 'failed', detail: 'Маршрут содержит пересадку.' });
  await installJourney(page, session); await page.goto('/');
  const card = page.locator('.option-card').first();
  await expect(card.getByText('Прямой рейс: Маршрут содержит пересадку.', { exact: true })).toBeVisible();
  await card.getByText('Перед реальной поездкой', { exact: true }).click();
  await expect(card.getByText('Правила перевозчика не подтверждены.', { exact: true })).toBeVisible();
  await expect(card.getByText('Требует проверки', { exact: true })).toBeVisible();
  await card.getByRole('button', { name: 'Проверить цены: Пафос', exact: true }).click();
  await expect(page.getByRole('alert')).toBeVisible();
  await expect(card.getByText('Требует проверки', { exact: true })).toBeVisible();
  await expect(card.getByText('Ориентировочно · демо', { exact: true })).toBeVisible();
});

test('completed refresh retains useful demo range without certifying unknown conditions', async ({ page }) => {
  await installJourney(page, demoSession);
  await page.route('**/prices/refresh', async route => {
    await page.evaluate(session => window.dispatchEvent(new CustomEvent('fixture-session', { detail: JSON.stringify({
      session, sequence: 2, server_time: '2026-10-28T10:01:00Z',
      job: { id: 'completed-demo-refresh', session_id: session.id, kind: 'prices_refresh', status: 'completed', captured_revision: session.revision, result_revision: session.revision, target_option_ids: ['fixture-option'], result_option_ids: ['fixture-option'] },
    }) })), demoSession);
    await route.fulfill({ json: { job_id: 'completed-demo-refresh', sequence: 2 } });
  });
  await page.goto('/');
  const card = page.locator('.option-card').first();
  await card.getByRole('button', { name: 'Проверить цены: Пафос', exact: true }).click();
  await expect(card.getByText('Обновление цен завершено', { exact: true })).toBeVisible();
  await expect(card.getByText('Ориентировочно · демо', { exact: true })).toBeVisible();
  await expect(card.getByText('Правила перевозчика не подтверждены.', { exact: true })).toBeHidden();
  await card.getByText('Перед реальной поездкой', { exact: true }).click();
  await expect(card.getByText('Требует проверки', { exact: true })).toBeVisible();
  await expect(card.getByText('Проверено', { exact: true })).toHaveCount(0);
});

test('factual full-trip total takes precedence and legacy options keep their existing unknown-price path', async ({ page }) => {
  const session = structuredClone(demoSession);
  session.options[0].pricing_summary = {
    display_kind: 'full_trip', full_trip: { kind: 'point', amount: '987', currency: 'EUR' },
    included_components: ['flight', 'stay', 'transfer'], missing_components: [], reference_components: [], missing_reasons: {},
    excluded_charges: [], unknown_charges: [], currency: 'EUR', scope_label: 'Фактическая область источника', assumptions: [],
    budget_assessment: 'within_budget', budget_explanation: 'По фактическим данным.', hard_constraint_status: 'passed',
  };
  await installJourney(page, session); await page.goto('/');
  const card = page.locator('.option-card').first();
  await expect(card.getByText('Полная стоимость поездки', { exact: true })).toBeVisible();
  await expect(card.locator('.price-headline')).toContainText('987');
  await expect(card.getByText('Ориентировочно · демо', { exact: true })).toHaveCount(0);
  await expect(card.getByText('Перед реальной поездкой', { exact: true })).toHaveCount(0);
  expect(sessionFixture.options[0].demo_pricing).toBeUndefined();
});
