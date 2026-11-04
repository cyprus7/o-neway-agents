import { expect, test } from '@playwright/test';
import { installLibrary } from './fixtures/library';
import { sessionFixture } from './fixtures/journey';

test('library paginates and preserves per-trip drafts without applying memory by default', async ({ page }) => {
  const requests = await installLibrary(page); await page.goto('/');
  if (['mobile', 'narrow'].includes(test.info().project.name)) await page.getByRole('button', { name: 'Обсудить поездку' }).click();
  const composer = page.getByRole('textbox', { name: 'Расскажите о путешествии' }); await expect(composer).toBeEnabled(); await composer.fill('Черновик первой поездки');
  if (['mobile', 'narrow'].includes(test.info().project.name)) await page.getByRole('button', { name: 'Закрыть чат' }).click();
  await page.getByRole('button', { name: 'Мои поездки', exact: true }).click();
  const library = page.getByRole('region', { name: 'Мои поездки и предпочтения' });
  await expect(library.getByRole('button', { name: 'Открыть Первая поездка' })).toBeVisible();
  await expect(library.getByRole('checkbox')).not.toBeChecked();
  await library.getByRole('button', { name: 'Загрузить ещё поездки' }).click();
  await library.getByRole('button', { name: 'Открыть Вторая поездка' }).click();
  await expect.poll(() => page.evaluate(() => localStorage.getItem('one-request-session'))).toBe('library-two');
  await library.getByRole('button', { name: 'Открыть Первая поездка' }).click();
  if (['mobile', 'narrow'].includes(test.info().project.name)) await page.getByRole('button', { name: 'Обсудить поездку' }).click();
  await expect(composer).toHaveValue('Черновик первой поездки');
  expect(requests.some(request => request.path.includes('cursor=fixture-next'))).toBe(true);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
});

test('memory save is explicit, allowlisted, editable and forgettable; export stays local', async ({ page }) => {
  const requests = await installLibrary(page); await page.goto('/'); await page.getByRole('button', { name: 'Мои поездки', exact: true }).click();
  const library = page.getByRole('region', { name: 'Мои поездки и предпочтения' });
  await expect(library.getByRole('button', { name: 'Взять из текущей поездки' })).toBeEnabled();
  await library.getByRole('button', { name: 'Взять из текущей поездки' }).click();
  await expect(library.getByLabel('Значение', { exact: true })).toHaveValue('Варшава');
  expect(requests.filter(request => request.method === 'PUT')).toHaveLength(0);
  await library.getByRole('button', { name: 'Сохранить предпочтение с моего согласия' }).click();
  await expect(library.getByText('Предпочтение сохранено с вашего согласия.')).toBeVisible();
  expect(requests.find(request => request.method === 'PUT')?.body).toEqual({ value: 'Варшава', source_trip_id: 'fixture-trip' });
  expect(await library.getByRole('combobox', { name: 'Предпочтение', exact: true }).locator('option').allTextContents()).not.toContain('Бюджет');
  await library.getByRole('button', { name: 'Изменить Город отправления' }).click(); await library.getByLabel('Значение', { exact: true }).fill('Берлин');
  await library.getByRole('button', { name: 'Сохранить предпочтение с моего согласия' }).click(); await expect(library.getByText('Берлин', { exact: true })).toBeVisible();
  const download = page.waitForEvent('download'); await library.getByRole('button', { name: 'Скачать мои данные JSON' }).click(); expect((await download).suggestedFilename()).toBe('one-request-export.json');
  await library.getByRole('button', { name: 'Забыть Город отправления' }).click(); await expect(library.getByRole('button', { name: 'Забыть Город отправления' })).toHaveCount(0);
  expect(requests.some(request => request.method === 'DELETE' && request.path === '/api/me/memory/origin')).toBe(true);
});

test('opt-in memory applies only to the next explicitly created trip', async ({ page }) => {
  const requests = await installLibrary(page); await page.goto('/'); await page.getByRole('button', { name: 'Мои поездки', exact: true }).click();
  const library = page.getByRole('region', { name: 'Мои поездки и предпочтения' });
  await expect(library.getByRole('checkbox')).toBeEnabled();
  await library.getByRole('button', { name: 'Взять из текущей поездки' }).click(); await library.getByRole('button', { name: 'Сохранить предпочтение с моего согласия' }).click();
  await expect(library.getByText('Предпочтение сохранено с вашего согласия.')).toBeVisible(); await library.getByRole('checkbox').check();
  if (['mobile', 'narrow'].includes(test.info().project.name)) await page.getByRole('button', { name: 'Обсудить поездку' }).click();
  await page.getByRole('button', { name: /^Новая поездка/ }).click();
  await expect(page.getByText('Применены сохранённые предпочтения: Город отправления')).toBeVisible();
  expect(requests.filter(request => request.path === '/api/sessions' && request.method === 'POST').at(-1)?.body).toEqual({ apply_memory: true });
});

test('trip deletion requires explicit confirmation; errors retain the current trip', async ({ page }) => {
  const requests = await installLibrary(page); await page.goto('/'); await page.getByRole('button', { name: 'Мои поездки', exact: true }).click();
  const library = page.getByRole('region', { name: 'Мои поездки и предпочтения' }); await expect(library.getByRole('button', { name: 'Удалить Первая поездка' })).toBeEnabled();
  page.once('dialog', dialog => dialog.dismiss()); await library.getByRole('button', { name: 'Удалить Первая поездка' }).click();
  expect(requests.filter(request => request.method === 'DELETE')).toHaveLength(0);
  await page.route('**/api/sessions/fixture-trip', route => route.request().method() === 'DELETE' ? route.fulfill({ status: 503, json: { detail: 'Удаление временно недоступно' } }) : route.fallback());
  page.once('dialog', dialog => dialog.accept()); await library.getByRole('button', { name: 'Удалить Первая поездка' }).click();
  await expect(library.getByRole('alert')).toContainText('Удаление временно недоступно'); expect(await page.evaluate(() => localStorage.getItem('one-request-session'))).toBe('fixture-trip');
});

test('owner switch unmounts library and ignores delayed private data', async ({ page }) => {
  await installLibrary(page); let release!: () => void; const held = new Promise<void>(resolve => { release = resolve; });
  await page.route('**/api/me/memory', async route => { await held; await route.fulfill({ json: { items: [{ field: 'origin', value: 'ПРИВАТНЫЕ СТАРЫЕ ДАННЫЕ', source_trip_id: 'fixture-trip', consented_at: '2026-10-28', updated_at: '2026-10-28' }] } }); });
  await page.goto('/'); await page.getByRole('button', { name: 'Мои поездки', exact: true }).click();
  await expect(page.getByRole('region', { name: 'Мои поездки и предпочтения' })).toBeVisible();
  const next = { ...sessionFixture, id: 'new-owner-trip', messages: [], options: [] };
  await page.route('**/api/sessions', route => route.fulfill({ json: next })); await page.route('**/api/fixture-snapshot', route => route.fulfill({ json: { session: next, sequence: 1, job: null, server_time: '2026-10-28T10:00:00Z' } }));
  await page.evaluate(() => { localStorage.removeItem('one-request-session'); window.dispatchEvent(new Event('one-request-auth-changed')); });
  release();
  await expect.poll(() => page.evaluate(() => localStorage.getItem('one-request-session'))).toBe('new-owner-trip');
  await expect(page.getByText('ПРИВАТНЫЕ СТАРЫЕ ДАННЫЕ')).toHaveCount(0); await expect(page.getByRole('region', { name: 'Мои поездки и предпочтения' })).toHaveCount(0);
});

test('confirmed deletion replaces the current trip with a fresh session', async ({ page }) => {
  const requests = await installLibrary(page); await page.goto('/'); await page.getByRole('button', { name: 'Мои поездки', exact: true }).click();
  const library = page.getByRole('region', { name: 'Мои поездки и предпочтения' }); await expect(library.getByRole('button', { name: 'Удалить Первая поездка' })).toBeEnabled();
  page.once('dialog', dialog => dialog.accept()); await library.getByRole('button', { name: 'Удалить Первая поездка' }).click();
  await expect(library).toHaveCount(0);
  await expect.poll(() => page.evaluate(() => localStorage.getItem('one-request-session'))).toMatch(/^fixture-created-/);
  expect(requests.some(request => request.path === '/api/sessions/fixture-trip' && request.method === 'DELETE')).toBe(true);
  expect(requests.filter(request => request.path === '/api/sessions' && request.method === 'POST').at(-1)?.body).toEqual({ apply_memory: false });
});

test('confirmed account deletion clears saved navigation and private workspace', async ({ page }) => {
  const requests = await installLibrary(page); await page.goto('/'); await page.getByRole('button', { name: 'Мои поездки', exact: true }).click();
  const library = page.getByRole('region', { name: 'Мои поездки и предпочтения' }); await expect(library.getByRole('button', { name: 'Удалить все мои данные' })).toBeEnabled();
  page.once('dialog', dialog => { expect(dialog.message()).toContain('нельзя отменить'); return dialog.accept(); });
  await library.getByRole('button', { name: 'Удалить все мои данные' }).click();
  await expect(page.getByRole('heading', { name: 'Сеанс завершён' })).toBeVisible();
  expect(requests.some(request => request.path === '/api/me' && request.method === 'DELETE')).toBe(true);
  expect(await page.evaluate(() => localStorage.getItem('one-request-session'))).toBeNull();
  await expect(page.getByRole('region', { name: 'Мои поездки и предпочтения' })).toHaveCount(0);
});
