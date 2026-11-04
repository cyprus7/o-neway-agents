import { expect, test } from '@playwright/test';
import { installJourney, sessionFixture } from './fixtures/journey';
import type { Session } from '../src/types';

const alternatives: Session = { ...sessionFixture, accepted_plan_revision: 1, options: [
  { ...sessionFixture.options[0], stay: { ...sessionFixture.options[0].stay, title: 'Отель Альфа · демонстрационные данные' }, constraint_checks: [{ constraint: 'Размещение с питомцем', status: 'unknown', detail: 'Источник не подтвердил' }] },
  { ...sessionFixture.options[0], id: 'fixture-option-beta', stay: { ...sessionFixture.options[0].stay, title: 'Отель Бета · демонстрационные данные' }, constraint_checks: [{ constraint: 'Без хостелов', status: 'failed', detail: 'Тип размещения не подходит' }] },
] };

test('same-city alternatives compare saved evidence with unchanged revision', async ({ page }) => {
  const requests = await installJourney(page, alternatives); await page.goto('/');
  const workspace = page.getByRole('region', { name: 'Сравнить варианты' });
  await expect(workspace.getByRole('heading', { name: /Пафос · Отель Альфа/ })).toBeVisible();
  await expect(workspace.getByRole('heading', { name: /Пафос · Отель Бета/ })).toBeVisible();
  await expect(workspace.getByText('Проверено: 0. Не выполняется: 0. Неизвестно: 1.')).toBeVisible();
  await expect(workspace.getByText('Проверено: 0. Не выполняется: 1. Неизвестно: 0.')).toBeVisible();
  await workspace.getByRole('button', { name: 'Сравнить по источникам' }).focus(); await page.keyboard.press('Enter');
  await expect(page.getByText('Сравнение по сохранённым источникам. Реальные цены и наличие неизвестны.')).toBeVisible();
  const command = requests.find(request => (request.body as { action?: string })?.action === 'compare');
  expect(command?.body).toMatchObject({ action: 'compare', option_ids: ['fixture-option', 'fixture-option-beta'], expected_revision: 1 });
  expect(requests.filter(request => request.path.endsWith('/prices/refresh'))).toHaveLength(0);
  if (test.info().project.name === 'mobile' || test.info().project.name === 'narrow') await page.getByRole('button', { name: 'Закрыть чат' }).click();
  await workspace.getByRole('button', { name: /Объяснить вариант: Пафос · Отель Альфа/ }).click();
  await expect(page.getByText('Объяснение по сохранённым источникам. Условия требуют проверки.')).toBeVisible();
  expect(requests.find(request => (request.body as { action?: string })?.action === 'explain')?.body).toMatchObject({ option_ids: ['fixture-option'], expected_revision: 1 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
});

test('superseded plan remains readable without selection or payment actions', async ({ page }) => {
  await installJourney(page, { ...alternatives, revision: 2, options: [], accepted_plan_revision: null, previous_plan: { revision: 1, intent: alternatives.intent, options: alternatives.options } });
  await page.goto('/');
  const previous = page.getByRole('region', { name: 'Предыдущий план · версия 1' });
  await expect(previous).toBeVisible();
  await expect(previous.getByText(/Этот план устарел/)).toBeVisible();
  await expect(previous.getByRole('button')).toHaveCount(0);
  await expect(page.getByRole('button', { name: /Посмотреть маршрут|Забронировать в демо/ })).toHaveCount(0);
  await expect(previous.getByRole('heading', { name: /Отель Альфа/ })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
});
