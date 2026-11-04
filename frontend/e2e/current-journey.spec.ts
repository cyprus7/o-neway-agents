import { expect, test } from '@playwright/test';
import { installJourney, sessionFixture } from './fixtures/journey';

test.beforeEach(async ({ page }) => { await installJourney(page); await page.goto('/'); });

test('fixture itinerary keeps real prices unknown and fits viewport', async ({ page }) => {
  await expect(page.getByText('Демонстрационные данные', { exact: true }).first()).toBeVisible();
  await page.getByRole('button', { name: /Посмотреть маршрут/ }).click();
  await expect(page.getByRole('heading', { name: 'Ваш маршрут: Пафос' })).toBeVisible();
  await expect(page.getByRole('region', { name: 'Выбранный маршрут' }).getByText('Цена пока не найдена').first()).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
});

test('keyboard dialog traps focus and Escape restores trigger', async ({ page }) => {
  const trigger = page.getByRole('button', { name: 'Как это работает' });
  await trigger.focus(); await page.keyboard.press('Enter');
  const dialog = page.getByRole('dialog'); await expect(dialog).toBeVisible();
  await expect(dialog.getByRole('button', { name: 'Закрыть', exact: true })).toBeFocused();
  await page.keyboard.press('Shift+Tab'); await expect(dialog.getByRole('button', { name: 'Давайте начнём' })).toBeFocused();
  await page.keyboard.press('Tab'); await expect(dialog.getByRole('button', { name: 'Закрыть', exact: true })).toBeFocused();
  await page.keyboard.press('Escape'); await expect(dialog).toBeHidden(); await expect(trigger).toBeFocused();
});

test('explicit fake payment produces a simulated receipt', async ({ page }) => {
  await page.getByRole('button', { name: /Посмотреть маршрут/ }).click();
  await page.getByRole('button', { name: /Забронировать в демо/ }).click();
  const dialog = page.getByRole('dialog'); await expect(dialog).toBeVisible();
  await expect(dialog.getByLabel('Номер карты')).toHaveValue('1234 5678 9012 3456');
  await expect(dialog.getByLabel('Номер карты')).toHaveAttribute('readonly', '');
  const checkout = page.waitForRequest(request => request.url().endsWith('/checkout'));
  await page.getByRole('button', { name: /Оплатить .* в демо/ }).click();
  expect((await checkout).postDataJSON()).toEqual({ option_id: 'fixture-option', expected_revision: 1, quote_id: 'fixture-quote' });
  await expect(page.getByRole('heading', { name: 'Демо-оплата прошла' })).toBeFocused();
  await expect(dialog.getByText(/Деньги не списаны. Билеты, отель и трансфер не забронированы/)).toBeVisible();
  await expect(dialog.getByText('fixture-receipt')).toBeVisible();
});

test('keyboard composer accepts a fixture refinement and retains itinerary options', async ({ page }) => {
  if (test.info().project.name === 'mobile' || test.info().project.name === 'narrow') {
    await page.getByRole('button', { name: 'Обсудить поездку' }).click();
  }
  const composer = page.getByRole('textbox', { name: 'Расскажите о путешествии' });
  await expect(composer).toBeEnabled();
  await composer.fill('Без хостелов, прежние даты');
  await composer.press('Shift+Enter');
  await expect(composer).toHaveValue('Без хостелов, прежние даты\n');
  await composer.press('Enter');
  await expect(page.getByText('Без хостелов, прежние даты', { exact: true })).toBeVisible();
  await expect(composer).toBeEnabled();
  await expect(composer).toHaveValue('');
  await expect(composer).toBeFocused();
});

test('failed refinement preserves draft and retry uses the same command key', async ({ page }) => {
  const keys: string[] = [];
  await page.route('**/messages', async route => {
    keys.push(route.request().headers()['idempotency-key']);
    expect(route.request().headers()['x-csrf-token']).toBe('fixture-csrf');
    expect(route.request().postDataJSON()).toEqual({ content: 'Сохранить прежние даты', expected_revision: 1 });
    if (keys.length === 1) await route.fulfill({ status: 503, json: { detail: 'Тестовый временный сбой' } });
    else await route.fallback();
  });
  if (test.info().project.name === 'mobile' || test.info().project.name === 'narrow') await page.getByRole('button', { name: 'Обсудить поездку' }).click();
  const composer = page.getByRole('textbox', { name: 'Расскажите о путешествии' });
  await expect(composer).toBeEnabled();
  await composer.fill('Сохранить прежние даты'); await composer.press('Enter');
  await expect(composer).toHaveValue('Сохранить прежние даты'); await expect(composer).toBeFocused();
  await expect(page.getByRole('alert')).toContainText('Тестовый временный сбой');
  await composer.press('Enter'); await expect(composer).toHaveValue('');
  await expect(page.getByText('Сохранить прежние даты', { exact: true })).toBeVisible();
  expect(keys).toHaveLength(2); expect(keys[0]).toBe(keys[1]); expect(keys[0].length).toBeGreaterThanOrEqual(8);
});

test('authentication expiry discards private state and requires explicit re-entry', async ({ page }) => {
  await expect(page.getByRole('button', { name: /Посмотреть маршрут/ })).toBeEnabled();
  const fresh = { ...sessionFixture, id: 'fixture-new-owner-trip', options: [], messages: [] };
  await page.route('**/api/sessions', route => route.fulfill({ json: fresh }));
  await page.route('**/api/fixture-snapshot', route => route.fulfill({ json: { session: fresh, sequence: 1, job: null, server_time: '2026-10-28T10:00:00Z' } }));
  await page.route('**/messages', route => route.fulfill({ status: 401, json: { detail: { code: 'authentication_required', message: 'Сеанс завершён' } } }));
  if (test.info().project.name === 'mobile' || test.info().project.name === 'narrow') await page.getByRole('button', { name: 'Обсудить поездку' }).click();
  const composer = page.getByRole('textbox', { name: 'Расскажите о путешествии' });
  await composer.fill('Старый личный запрос'); await composer.press('Enter');
  await expect(page.getByRole('heading', { name: 'Сеанс завершён' })).toBeVisible();
  await expect(page.getByRole('button', { name: /Посмотреть маршрут/ })).toHaveCount(0);
  await expect(page.getByText('Демонстрационные данные: маршрут для проверки интерфейса.')).toHaveCount(0);
  expect(await page.evaluate(() => localStorage.getItem('one-request-session'))).toBeNull();
  expect(await page.evaluate(() => new URL(location.href).searchParams.get('trip'))).toBeNull();
  await page.getByRole('button', { name: 'Начать новый сеанс' }).click();
  await expect.poll(() => page.evaluate(() => localStorage.getItem('one-request-session'))).toBe(fresh.id);
});
