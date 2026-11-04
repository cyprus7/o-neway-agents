import { expect, test } from '@playwright/test';
import type { Page } from '@playwright/test';
import type { Session } from '../src/types';

async function chat(page: Page, visible: boolean) {
  if (test.info().project.name !== 'actual-mobile') return;
  const close = page.getByRole('button', { name: 'Закрыть чат' });
  if (visible && !await close.isVisible()) await page.getByRole('button', { name: 'Обсудить поездку' }).click();
  if (!visible && await close.isVisible()) await close.click();
}
async function current(page: Page): Promise<Session> {
  const id = await page.evaluate(() => localStorage.getItem('one-request-session'));
  const response = await page.request.get(`/api/sessions/${id}`); expect(response.ok()).toBe(true); return response.json();
}

// Actual cookie/CSRF/worker/SSE/SQLite/gateway/catalog; only inference is a deterministic fixture.
test('actual application journey through persisted simulated receipt', async ({ page }) => {
  await page.goto('/'); await chat(page, true);
  const composer = page.getByRole('textbox', { name: 'Расскажите о путешествии' });
  await expect(composer).toBeEnabled(); await composer.fill('Хочу на неделю в Ларнаку из Варшавы'); await composer.press('Enter');
  await expect(page.getByRole('button', { name: /Посмотреть маршрут/ })).toHaveCount(3);
  const planned = await current(page); expect(planned.options).toHaveLength(3);
  const revision = planned.revision;
  expect(new Set(planned.options.map(option => option.stay.title)).size).toBe(3);
  expect(planned.options.every(option => option.stay.details?.discovery_mode === 'local_catalog')).toBe(true);
  expect(planned.options.every(option => option.demo_pricing?.schema_version === 'demo-pricing.v1')).toBe(true);
  await expect(page.locator('.option-card').getByText('Ориентировочно · демо', { exact: true })).toHaveCount(3);
  await chat(page, false);
  await page.getByRole('button', { name: 'Сравнить по источникам' }).focus(); await page.keyboard.press('Enter');
  await expect.poll(async () => (await current(page)).messages.length).toBeGreaterThan(planned.messages.length);
  let compared = await current(page); expect(compared.revision).toBe(revision);
  await chat(page, false);
  await page.getByRole('button', { name: /Объяснить вариант:/ }).first().click();
  await expect.poll(async () => (await current(page)).messages.length).toBeGreaterThan(compared.messages.length);
  expect((await current(page)).revision).toBe(revision);
  await chat(page, false);
  await page.getByRole('button', { name: /Посмотреть маршрут/ }).first().click();
  await expect(page.getByRole('heading', { name: /Ваш маршрут:/ })).toBeVisible();
  await page.getByRole('button', { name: /Забронировать в демо/ }).click();
  const dialog = page.getByRole('dialog');
  await expect(dialog.getByLabel('Номер карты')).toHaveValue('1234 5678 9012 3456');
  await page.getByRole('button', { name: /Оплатить .* в демо/ }).click();
  await expect(page.getByRole('heading', { name: 'Демо-оплата прошла' })).toBeFocused();
  await expect(dialog.getByText(/Деньги не списаны. Билеты, отель и трансфер не забронированы/)).toBeVisible();
  const receiptId = await dialog.locator('.demo-receipt-details dd').nth(1).innerText();
  await page.screenshot({ path: test.info().outputPath('actual-receipt.png') });
  await page.keyboard.press('Escape'); await expect(dialog).toBeHidden();
  await page.screenshot({ path: test.info().outputPath('actual-product.png'), fullPage: true });
  const receipts = await page.request.get(`/api/sessions/${planned.id}/receipts`); expect(receipts.status()).toBe(200);
  const savedReceipts = (await receipts.json()).items;
  expect(savedReceipts).toEqual(expect.arrayContaining([expect.objectContaining({ id: receiptId, status: 'simulated', ready_to_book: false })]));
  const paid = savedReceipts.find((item: { id: string }) => item.id === receiptId).demo_payment;
  const estimate = planned.options[0].demo_pricing!;
  expect(paid.paid_amount_minor).toBe(estimate.example_total_minor);
  expect(paid.currency).toBe(estimate.currency);
  expect(paid.invoice.service_fee_minor).toBe(estimate.example_service_fee_minor);
  for (const component of estimate.components) {
    expect(paid.invoice.line_items.find((line: { key: string }) => line.key === component.kind).amount_minor).toBe(component.example_amount_minor);
  }
  await page.reload(); await expect(page.getByRole('heading', { name: /Ваш маршрут:/ })).toBeVisible();
  const reloaded = await page.request.get(`/api/sessions/${planned.id}/receipts`);
  expect((await reloaded.json()).items.some((item: { id: string }) => item.id === receiptId)).toBe(true);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: test.info().outputPath('actual-product.png'), fullPage: true });
});

test('actual memory lifecycle, library deletion and foreign-owner denial', async ({ page, browser, baseURL }) => {
  await page.goto('/'); await chat(page, true);
  const composer = page.getByRole('textbox', { name: 'Расскажите о путешествии' });
  await expect(composer).toBeEnabled(); await composer.fill('Поездка из Варшавы в Ларнаку'); await composer.press('Enter');
  await expect(page.getByRole('button', { name: /Посмотреть маршрут/ })).toHaveCount(3);
  const planned = await current(page); await chat(page, false);
  await page.getByRole('button', { name: 'Мои поездки', exact: true }).click();
  const library = page.getByRole('region', { name: 'Мои поездки и предпочтения' });
  await expect(library.getByRole('checkbox')).not.toBeChecked();
  await library.getByRole('button', { name: 'Взять из текущей поездки' }).click();
  await library.getByRole('button', { name: 'Сохранить предпочтение с моего согласия' }).click();
  await expect(library.getByRole('button', { name: 'Забыть Город отправления' })).toBeVisible();
  const download = page.waitForEvent('download'); await library.getByRole('button', { name: 'Скачать мои данные JSON' }).click(); expect((await download).suggestedFilename()).toBe('one-request-export.json');
  await library.getByRole('checkbox').check(); await chat(page, true);
  await page.getByRole('button', { name: /^Новая поездка/ }).click(); await chat(page, false);
  await expect(page.getByText('Применены сохранённые предпочтения: Город отправления')).toBeVisible();
  const fresh = await current(page); expect(fresh.id).not.toBe(planned.id); expect(fresh.intent.origin).toBe('Warsaw');
  await page.getByRole('button', { name: 'Мои поездки', exact: true }).click();
  await expect(library.getByRole('button', { name: 'Забыть Город отправления' })).toBeVisible();
  await library.getByRole('button', { name: 'Забыть Город отправления' }).click();
  await expect(library.getByRole('button', { name: 'Забыть Город отправления' })).toHaveCount(0);
  const other = await browser.newContext({ baseURL }); const foreign = await other.newPage(); await foreign.goto('/');
  await expect.poll(() => foreign.evaluate(() => localStorage.getItem('one-request-session'))).not.toBeNull();
  const denied = await other.request.get(`/api/sessions/${planned.id}`); expect(denied.status()).toBe(404);
  await other.close();
  page.once('dialog', prompt => prompt.accept());
  await library.getByRole('button', { name: /^Удалить / }).filter({ hasText: /Новая поездка/ }).first().click();
  await expect(library).toHaveCount(0);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: test.info().outputPath('actual-product.png'), fullPage: true });
});

test('actual immutable receipt aftercare, unsent drafts and linked date/transfer search', async ({ page }) => {
  await page.goto('/'); await chat(page, true);
  const composer = page.getByRole('textbox', { name: 'Расскажите о путешествии' });
  await expect(composer).toBeEnabled(); await composer.fill('Спланируй поездку в Ларнаку'); await composer.press('Enter');
  await expect(page.getByRole('button', { name: /Посмотреть маршрут/ })).toHaveCount(3); await chat(page, false);
  const original = await current(page);
  await page.getByRole('button', { name: /Посмотреть маршрут/ }).first().click();
  await page.getByRole('button', { name: /Забронировать в демо/ }).click();
  await page.getByRole('button', { name: /Оплатить .* в демо/ }).click();
  await expect(page.getByRole('heading', { name: 'Демо-оплата прошла' })).toBeVisible(); await page.keyboard.press('Escape');
  const originalReceipts = (await (await page.request.get(`/api/sessions/${original.id}/receipts`)).json()).items;
  expect(originalReceipts).toHaveLength(1); expect(originalReceipts[0].itinerary.intent.date_from).toBe(original.intent.date_from);
  const receiptId = originalReceipts[0].id;
  await page.reload(); await page.getByRole('button', { name: 'Квитанции и заметки' }).click();
  const care = page.getByRole('region', { name: 'Демо-квитанции и заметки к поездке' });
  await expect(care.getByRole('article', { name: `Демонстрационная квитанция ${receiptId}` })).toContainText('Симуляция • реальной брони нет');
  await care.getByLabel('Текст неотправленного черновика').fill('Ранний заезд, только если отель подтвердит');
  await care.getByRole('button', { name: 'Сохранить неотправленный черновик' }).focus(); await page.keyboard.press('Enter');
  await expect(care.getByText('Ранний заезд • Черновик, не отправлено')).toBeVisible();
  await care.getByRole('button', { name: /В архив Ранний заезд,/ }).click(); await expect(care.getByText('Ранний заезд • Архив, не отправлено')).toBeVisible();
  await expect(care.getByText(/Напоминания доступны только при настроенной связи с Telegram/)).toBeVisible();
  await expect(care.getByRole('button', { name: 'Запланировать напоминание' })).toBeDisabled();
  await care.getByLabel('Направление пользователя').fill('Ларнака'); await care.getByLabel('Отель пользователя').fill('Мои непроверенные сведения об отеле');
  await care.getByLabel('Дата начала из моих сведений').fill(original.intent.date_from!); await care.getByLabel('Дата окончания из моих сведений').fill(original.intent.date_to!);
  await care.getByRole('button', { name: 'Сохранить непроверенные сведения' }).click(); await expect(care.getByText(/Сохранено пользователем • не проверено/)).toBeVisible();
  await care.getByRole('heading', { name: 'Демо-квитанции и заметки к поездке' }).scrollIntoViewIfNeeded();
  await page.screenshot({ path: test.info().outputPath('actual-aftercare.png') });
  const nextDay = (day: string) => { const value = new Date(`${day}T00:00:00Z`); value.setUTCDate(value.getUTCDate() + 1); return value.toISOString().slice(0, 10); };
  const from = nextDay(original.intent.date_from!), to = nextDay(original.intent.date_to!);
  await care.getByLabel('Контекст демо-квитанции').selectOption(receiptId); await care.getByLabel('Новая дата начала', { exact: true }).fill(from); await care.getByLabel('Новая дата окончания', { exact: true }).fill(to);
  await care.getByLabel('Пожелание для нового поиска, например трансфер').fill('Нужен трансфер с детским креслом');
  await care.getByRole('button', { name: 'Создать новую связанную поездку' }).click();
  await expect.poll(async () => (await current(page)).id).not.toBe(original.id);
  const linked = await current(page); expect(linked.intent.date_from).toBe(from); expect(linked.intent.date_to).toBe(to);
  const propertyId = original.options[0].stay.details?.catalog_property_id;
  expect(propertyId).toBeTruthy();
  await expect.poll(async () => (await current(page)).options[0]?.stay.details?.catalog_property_id).toBe(propertyId);
  const firstAmendment = await current(page);
  expect(firstAmendment.options).toHaveLength(1);
  await chat(page, true); await expect(composer).toBeEnabled();
  await composer.fill('Сохрани тот же отель и даты, уточни трансфер'); await composer.press('Enter');
  await expect.poll(async () => (await current(page)).revision).toBeGreaterThan(firstAmendment.revision);
  await expect.poll(async () => (await current(page)).options.length).toBe(1);
  const followup = await current(page);
  expect(followup.options[0].stay.details?.catalog_property_id).toBe(propertyId);
  expect(followup.intent.date_from).toBe(from); expect(followup.intent.date_to).toBe(to);
  await chat(page, false);
  const retained = (await (await page.request.get(`/api/sessions/${original.id}/receipts`)).json()).items;
  expect(retained).toEqual(originalReceipts);
  await expect(page.getByRole('heading', { name: /Демо-квитанции и заметки к поездке/ })).toHaveCount(0);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: test.info().outputPath('actual-linked-trip.png'), fullPage: true });
});

test('actual explicit pet memory retains qualifiers when opted into a new trip', async ({ page }) => {
  await page.goto('/'); await page.getByRole('button', { name: 'Мои поездки', exact: true }).click();
  const library = page.getByRole('region', { name: 'Мои поездки и предпочтения' });
  await expect(library.getByRole('combobox', { name: 'Предпочтение', exact: true })).toBeEnabled();
  await library.getByRole('combobox', { name: 'Предпочтение', exact: true }).selectOption('pets');
  const values = ['собака 8 кг, только в салоне', 'без передачи в багаж'];
  await library.getByLabel('Значение', { exact: true }).fill(values.join('\n'));
  await library.getByRole('button', { name: 'Сохранить предпочтение с моего согласия' }).click();
  await expect(library.getByText('Предпочтение сохранено с вашего согласия.')).toBeVisible();
  await library.getByRole('checkbox').check(); await chat(page, true);
  await page.getByRole('button', { name: /^Новая поездка/ }).click(); await chat(page, false);
  await expect(page.getByText(/Применены сохранённые предпочтения:/)).toBeVisible();
  expect((await current(page)).intent.pets).toEqual(values);
});
