import { expect, test } from '@playwright/test';
import { installAftercare } from './fixtures/aftercare';
import { installLibrary } from './fixtures/library';

test('persisted simulated receipts reload; drafts remain unsent and can be archived', async ({ page }) => {
  const { requests } = await installAftercare(page); await page.goto('/'); await page.getByRole('button', { name: 'Квитанции и заметки' }).click();
  const care = page.getByRole('region', { name: 'Демо-квитанции и заметки к поездке' });
  await expect(care.getByRole('article', { name: 'Демонстрационная квитанция persisted-fixture-receipt' })).toContainText('реальной брони нет');
  await expect(care.getByText(/Снимок маршрута этой старой квитанции отсутствует/)).toBeVisible();
  await care.getByLabel('Текст неотправленного черновика').fill('Просьба о раннем заезде, если возможно');
  await care.getByRole('button', { name: 'Сохранить неотправленный черновик' }).focus(); await page.keyboard.press('Enter');
  await expect(care.getByText('Ранний заезд • Черновик, не отправлено')).toBeVisible();
  await page.reload(); await page.getByRole('button', { name: 'Квитанции и заметки' }).click();
  await expect(care.getByText('Ранний заезд • Черновик, не отправлено')).toBeVisible();
  await care.getByRole('button', { name: /В архив Просьба/ }).click(); await expect(care.getByText('Ранний заезд • Архив, не отправлено')).toBeVisible();
  expect(requests.some(request => request.method === 'PATCH' && request.body.status === 'archived')).toBe(true);
  await expect(care.getByRole('button', { name: /^Отправить/ })).toHaveCount(0);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await care.getByRole('heading', { name: 'Демо-квитанции и заметки к поездке' }).scrollIntoViewIfNeeded();
  await page.screenshot({ path: test.info().outputPath('aftercare.png') });
});

test('manual context stays unverified and failed amendment preserves the immutable receipt', async ({ page }) => {
  const { data, requests } = await installAftercare(page); const original = JSON.stringify(data.receipts);
  await page.route('**/amend', route => route.fulfill({ status: 409, json: { detail: 'Контекст квитанции устарел. Проверьте выбранную поездку.' } }));
  await page.goto('/'); await page.getByRole('button', { name: 'Квитанции и заметки' }).click();
  const care = page.getByRole('region', { name: 'Демо-квитанции и заметки к поездке' });
  await care.getByLabel('Направление пользователя').fill('Пафос'); await care.getByLabel('Отель пользователя').fill('Название введено пользователем');
  await care.getByLabel('Дата начала из моих сведений').fill('2026-12-12'); await care.getByLabel('Дата окончания из моих сведений').fill('2026-12-18');
  await care.getByRole('button', { name: 'Сохранить непроверенные сведения' }).click(); await expect(care.getByText(/Сохранено пользователем • не проверено:/)).toBeVisible();
  await care.getByLabel('Контекст демо-квитанции').selectOption('persisted-fixture-receipt');
  await care.getByLabel('Новая дата начала', { exact: true }).fill('2026-12-12'); await care.getByLabel('Пожелание для нового поиска, например трансфер').fill('Нужен трансфер с детским креслом');
  await care.getByRole('button', { name: 'Создать новую связанную поездку' }).click();
  await expect(care.getByRole('alert')).toContainText('Контекст квитанции устарел');
  await expect(care.getByRole('article', { name: 'Демонстрационная квитанция persisted-fixture-receipt' })).toBeVisible();
  expect(JSON.stringify(data.receipts)).toBe(original); expect(requests.find(request => request.path.endsWith('/manual-context'))?.body).toMatchObject({ destination: 'Пафос', hotel: 'Название введено пользователем' });
});

test('date and transfer changes create a separate linked trip without rewriting receipts', async ({ page }) => {
  const { data, requests } = await installAftercare(page); const original = JSON.stringify(data.receipts);
  await page.goto('/'); await page.getByRole('button', { name: 'Квитанции и заметки' }).click();
  const care = page.getByRole('region', { name: 'Демо-квитанции и заметки к поездке' });
  await care.getByLabel('Контекст демо-квитанции').selectOption('persisted-fixture-receipt');
  await care.getByLabel('Новая дата начала', { exact: true }).fill('2026-12-12'); await care.getByLabel('Новая дата окончания', { exact: true }).fill('2026-12-18');
  await care.getByLabel('Пожелание для нового поиска, например трансфер').fill('Трансфер с детским креслом');
  await care.getByRole('button', { name: 'Создать новую связанную поездку' }).click();
  await expect.poll(() => page.evaluate(() => localStorage.getItem('one-request-session'))).toBe('linked-fixture-trip');
  expect(requests.find(request => request.path.endsWith('/amend'))?.body).toEqual({ receipt_id: 'persisted-fixture-receipt', date_from: '2026-12-12', date_to: '2026-12-18', request: 'Трансфер с детским креслом' });
  expect(JSON.stringify(data.receipts)).toBe(original);
});

test('reminders require consent and timezone; unknown delivery is never shown as sent', async ({ page }) => {
  const { requests } = await installAftercare(page); await page.goto('/'); await page.getByRole('button', { name: 'Квитанции и заметки' }).click();
  const care = page.getByRole('region', { name: 'Демо-квитанции и заметки к поездке' });
  await expect(care.getByText('Доставка не подтверждена', { exact: true })).toBeVisible(); await expect(care.getByText('Отправлено в Telegram', { exact: true })).toHaveCount(0);
  await care.getByLabel('Местное время напоминания').fill('2026-12-10T10:30'); await care.getByLabel('Часовой пояс IANA').fill('Asia/Nicosia'); await care.getByLabel('Текст напоминания').fill('Проверить документы');
  const submit = care.getByRole('button', { name: 'Запланировать напоминание' }); await expect(submit).toBeDisabled();
  await care.getByRole('checkbox', { name: 'Согласен получить это напоминание в Telegram' }).check(); await submit.click();
  await expect(care.getByText('Запланировано', { exact: true })).toBeVisible();
  expect(requests.find(request => request.path.endsWith('/reminders') && request.method === 'POST')?.body).toEqual({ local_time: '2026-12-10T10:30', timezone: 'Asia/Nicosia', consent: true, text: 'Проверить документы' });
  await expect(care.getByRole('checkbox')).not.toBeChecked();
  await care.getByRole('button', { name: 'Отменить напоминание Проверить документы' }).click(); await expect(care.getByText('Отменено', { exact: true })).toBeVisible();
});

test('unavailable Telegram reminder capability remains visibly disabled', async ({ page }) => {
  await installAftercare(page, false); await page.goto('/'); await page.getByRole('button', { name: 'Квитанции и заметки' }).click();
  const care = page.getByRole('region', { name: 'Демо-квитанции и заметки к поездке' });
  await expect(care.getByText(/Напоминания доступны только при настроенной связи с Telegram/)).toBeVisible();
  await expect(care.getByRole('checkbox')).toBeDisabled(); await expect(care.getByRole('button', { name: 'Запланировать напоминание' })).toBeDisabled();
});

test('canonical reminder state renders all six labels and only cancellable actions after reopening', async ({ page }) => {
  const { data, requests } = await installAftercare(page);
  const cases = [
    { state: 'scheduled', label: 'Запланировано', cancellable: true },
    { state: 'sending', label: 'Отправка выполняется', cancellable: true },
    { state: 'sent', label: 'Отправлено в Telegram', cancellable: false },
    { state: 'canceled', label: 'Отменено', cancellable: false },
    { state: 'failed', label: 'Не удалось отправить', cancellable: false },
    { state: 'delivery_unknown', label: 'Доставка не подтверждена', cancellable: false },
  ] as const;
  data.reminders = cases.map(item => ({ id: `canonical-${item.state}`, text: `Проверка состояния ${item.state}`, state: item.state, local_time: '2026-12-10T10:30', timezone: 'Asia/Nicosia' }));
  await page.goto('/');
  const care = page.getByRole('region', { name: 'Демо-квитанции и заметки к поездке' });
  for (let opening = 0; opening < 2; opening++) {
    if (opening) await page.reload();
    await page.getByRole('button', { name: 'Квитанции и заметки' }).click();
    for (const item of cases) {
      const row = care.getByRole('listitem').filter({ has: page.getByText(`Проверка состояния ${item.state}`, { exact: true }) });
      await expect(row.locator('strong')).toHaveText(item.label);
      await expect(row.getByRole('button', { name: /Отменить напоминание/ })).toHaveCount(item.cancellable ? 1 : 0);
      if (item.state === 'delivery_unknown') await expect(row).not.toContainText('Отправлено в Telegram');
    }
  }
  const cancel = care.getByRole('button', { name: 'Отменить напоминание Проверка состояния scheduled', exact: true });
  await cancel.focus(); await page.keyboard.press('Enter');
  const canceled = care.getByRole('listitem').filter({ has: page.getByText('Проверка состояния scheduled', { exact: true }) });
  await expect(canceled.locator('strong')).toHaveText('Отменено');
  await expect(canceled.getByRole('button')).toHaveCount(0);
  expect(requests.some(request => request.method === 'DELETE' && request.path.endsWith('/reminders/canonical-scheduled'))).toBe(true);
  await care.getByRole('button', { name: 'Отменить напоминание Проверка состояния sending', exact: true }).click();
  const uncertain = care.getByRole('listitem').filter({ has: page.getByText('Проверка состояния sending', { exact: true }) });
  await expect(uncertain.locator('strong')).toHaveText('Доставка не подтверждена');
  await expect(uncertain.getByRole('button')).toHaveCount(0);
});

test('ambiguous local reminder time surfaces actionable DST error', async ({ page }) => {
  await installAftercare(page); await page.route('**/reminders', route => route.fulfill({ status: 422, json: { detail: 'Указанное местное время неоднозначно.' } }));
  await page.goto('/'); await page.getByRole('button', { name: 'Квитанции и заметки' }).click();
  const care = page.getByRole('region', { name: 'Демо-квитанции и заметки к поездке' });
  await care.getByLabel('Местное время напоминания').fill('2026-11-01T01:30'); await care.getByLabel('Текст напоминания').fill('Напомнить');
  await care.getByRole('checkbox').check(); await care.getByRole('button', { name: 'Запланировать напоминание' }).click();
  await expect(care.getByRole('alert')).toContainText('Проверьте даты, местное время и часовой пояс');
});

test('explicit pet memory preserves comma qualifiers inside each of at most five entries', async ({ page }) => {
  const requests = await installLibrary(page); await page.goto('/'); await page.getByRole('button', { name: 'Мои поездки', exact: true }).click();
  const library = page.getByRole('region', { name: 'Мои поездки и предпочтения' }); await expect(library.getByRole('combobox', { name: 'Предпочтение', exact: true })).toBeEnabled();
  await library.getByRole('combobox', { name: 'Предпочтение', exact: true }).selectOption('pets');
  await library.getByLabel('Значение', { exact: true }).fill('собака 8 кг, только в салоне\nкошка в переноске');
  await library.getByRole('button', { name: 'Сохранить предпочтение с моего согласия' }).click();
  await expect(library.getByText('Предпочтение сохранено с вашего согласия.')).toBeVisible();
  expect(requests.find(request => request.path.endsWith('/memory/pets'))?.body.value).toEqual(['собака 8 кг, только в салоне', 'кошка в переноске']);
  await library.getByRole('button', { name: 'Изменить Питомцы и условия перевозки' }).click();
  await expect(library.getByLabel('Значение', { exact: true })).toHaveValue('собака 8 кг, только в салоне\nкошка в переноске');
  await library.getByLabel('Значение', { exact: true }).fill('1\n2\n3\n4\n5\n6'); await library.getByRole('button', { name: 'Сохранить предпочтение с моего согласия' }).click();
  await expect(library.getByRole('alert')).toContainText('не больше пяти');
});
