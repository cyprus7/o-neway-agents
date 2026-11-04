import { expect, test } from '@playwright/test';
import type { Page } from '@playwright/test';
import { installJourney } from './fixtures/journey';

async function telegramFixture(page: Page, denied = false, queryHint = false, closeSupported = true) {
  const rawInitData = queryHint ? 'auth_date=fixture&user=fixture&hash=fixture' : 'auth_date=fixture&user=fixture&start_param=trip_fixture-trip&hash=fixture';
  const calls: string[] = [];
  page.on('request', request => { const path = new URL(request.url()).pathname; if (path.startsWith('/api/')) calls.push(path); });
  await page.addInitScript(({ initData, closeSupported }) => {
    const events = new Map<string, Set<() => void>>();
    const backs = new Set<() => void>();
    const stats = { ready: 0, closed: 0, removed: 0, usableOnReady: false, errorOnReady: false, backVisible: false };
    const app = {
      initData,
      colorScheme: 'dark', themeParams: { bg_color: '#161c19', secondary_bg_color: '#202923', text_color: '#f3f5f2', hint_color: '#b9c7bd', button_color: '#80bba0' },
      viewportStableHeight: 700, viewportHeight: 700, safeAreaInset: { top: 8, bottom: 12, left: 0, right: 0 }, contentSafeAreaInset: { top: 10, bottom: 4, left: 0, right: 0 },
      ready() { stats.ready++; stats.usableOnReady = !!document.querySelector('textarea:not(:disabled)'); stats.errorOnReady = !!document.querySelector('[role="alert"]'); },
      close() { stats.closed++; },
      onEvent(name: string, handler: () => void) { if (!events.has(name)) events.set(name, new Set()); events.get(name)!.add(handler); },
      offEvent(name: string, handler: () => void) { if (events.get(name)?.delete(handler)) stats.removed++; },
      BackButton: { show() { stats.backVisible = true; }, hide() { stats.backVisible = false; }, onClick(handler: () => void) { backs.add(handler); }, offClick(handler: () => void) { backs.delete(handler); } },
    };
    if (!closeSupported) Object.assign(app, { close: undefined });
    Object.assign(window, { Telegram: { WebApp: app }, miniappTest: { stats, emit(name: string) { events.get(name)?.forEach(handler => handler()); }, back() { backs.forEach(handler => handler()); }, listeners() { return [...events.values()].reduce((sum, values) => sum + values.size, 0); }, backListeners() { return backs.size; } } });
  }, { initData: rawInitData, closeSupported });
  await installJourney(page);
  await page.route('**/api/auth/session', route => route.fulfill({ json: { user: { id: 'telegram-fixture-user', kind: 'telegram' } } }));
  await page.route('**/api/auth/telegram', async route => {
    expect(route.request().postDataJSON()).toEqual({ init_data: rawInitData });
    if (denied) return route.fulfill({ status: 401, json: { detail: { code: 'telegram_invalid', message: 'Данные входа Telegram недействительны. Откройте приложение заново.' } } });
    await page.context().addCookies([{ name: 'one_request_csrf', value: 'fixture-csrf', url: 'http://127.0.0.1:4179', sameSite: 'Lax' }]);
    await route.fulfill({ json: { user: { id: 'telegram-fixture-user', kind: 'telegram' }, csrf_token: 'fixture-csrf' } });
  });
  return calls;
}

test('verified mock launch authenticates before navigation and reopens the same trip', async ({ page }) => {
  const calls = await telegramFixture(page); await page.goto('/');
  await expect(page.getByRole('heading', { name: 'Сравнить варианты' })).toBeVisible();
  await expect.poll(() => page.evaluate(() => (window as any).miniappTest.stats.ready)).toBe(1);
  expect(await page.evaluate(() => (window as any).miniappTest.stats.usableOnReady)).toBe(true);
  expect(calls[0]).toBe('/api/auth/telegram'); expect(calls).not.toContain('/api/auth/guest');
  expect(calls).toContain('/api/sessions/fixture-trip');
  expect(await page.evaluate(() => JSON.stringify(localStorage))).not.toContain('auth_date');
  await page.reload(); await expect(page.getByRole('heading', { name: 'Сравнить варианты' })).toBeVisible();
  expect(calls.filter(path => path === '/api/auth/telegram')).toHaveLength(2);
  expect(await page.evaluate(() => localStorage.getItem('one-request-session'))).toBe('fixture-trip');
});

test('tampered launch blocks trips and never silently creates a guest', async ({ page }) => {
  const calls = await telegramFixture(page, true); await page.goto('/');
  await expect(page.getByRole('alert')).toContainText('Данные входа Telegram недействительны');
  expect(calls).toEqual(['/api/auth/telegram']);
  await expect.poll(() => page.evaluate(() => (window as any).miniappTest.stats.ready)).toBe(1);
  expect(await page.evaluate(() => (window as any).miniappTest.stats.errorOnReady)).toBe(true);
  expect(await page.evaluate(() => (window as any).miniappTest.stats.backVisible)).toBe(false);
  await expect(page.getByRole('textbox', { name: 'Расскажите о путешествии', includeHidden: true })).toBeDisabled();
  await expect(page.getByRole('button', { name: 'Повторить вход' })).toHaveCount(0);
  await page.getByRole('button', { name: 'Закрыть приложение', exact: true }).click();
  expect(await page.evaluate(() => (window as any).miniappTest.stats.closed)).toBe(1);
  expect(await page.evaluate(() => localStorage.getItem('one-request-session'))).toBeNull();
});

test('unusable login cookies fail confirmation once before any trip or stream request', async ({ page }) => {
  const calls = await telegramFixture(page);
  await page.route('**/api/auth/session', route => route.fulfill({ status: 401, json: { detail: 'Сеанс завершён.' } }));
  await page.goto('/');
  await expect(page.getByRole('alert')).toContainText('Откройте Mini App заново в Telegram');
  await expect(page.getByRole('button', { name: 'Закрыть приложение', exact: true })).toBeVisible();
  expect(calls).toEqual(['/api/auth/telegram', '/api/auth/session']);
  expect(await page.evaluate(() => localStorage.getItem('one-request-session'))).toBeNull();
});

test('expired Telegram launch shows close and reopen guidance instead of reloading old data', async ({ page }) => {
  const calls = await telegramFixture(page);
  await page.route('**/api/auth/telegram', route => route.fulfill({ status: 401, json: { detail: { code: 'telegram_launch_expired', message: 'Launch expired' } } }));
  await page.goto('/');
  await expect(page.getByRole('alert')).toContainText('Срок действия запуска Telegram истёк');
  await expect.poll(() => page.evaluate(() => (window as any).miniappTest.stats.ready)).toBe(1);
  expect(calls).toEqual(['/api/auth/telegram']);
  await expect(page.getByRole('button', { name: 'Новая поездка', includeHidden: true })).toBeDisabled();
  await expect(page.getByRole('button', { name: 'Мои поездки' })).toBeDisabled();
  await page.getByRole('button', { name: 'Закрыть приложение', exact: true }).click();
  expect(await page.evaluate(() => (window as any).miniappTest.stats.closed)).toBe(1);
  expect(calls).toEqual(['/api/auth/telegram']);
});

test('Telegram without close support keeps actionable instructions and safe ready state', async ({ page }) => {
  const calls = await telegramFixture(page, true, false, false);
  await page.goto('/');
  await expect(page.getByRole('alert')).toContainText('Закройте Mini App и откройте его заново через кнопку в Telegram');
  await expect(page.getByRole('button', { name: 'Закрыть приложение', exact: true })).toHaveCount(0);
  await expect(page.getByRole('button', { name: 'Повторить вход' })).toHaveCount(0);
  await expect.poll(() => page.evaluate(() => (window as any).miniappTest.stats.ready)).toBe(1);
  expect(calls).toEqual(['/api/auth/telegram']);
});

test('private 401 during initial trip load is bounded without login remount', async ({ page }) => {
  const calls = await telegramFixture(page);
  await page.route('**/api/sessions/fixture-trip', route => route.fulfill({ status: 401, json: { detail: 'Сеанс завершён.' } }));
  await page.goto('/');
  await expect(page.getByRole('alert')).toContainText('Не удалось подтвердить вход');
  expect(calls.filter(path => path === '/api/auth/telegram')).toHaveLength(1);
  expect(calls.filter(path => path === '/api/auth/session')).toHaveLength(1);
  expect(calls.filter(path => path.includes('/events'))).toHaveLength(0);
  expect(calls).not.toContain('/api/auth/guest');
});

test('query start hint navigates only after authenticated owner checks', async ({ page }) => {
  const calls = await telegramFixture(page, false, true); await page.goto('/?start_param=trip_fixture-trip');
  await expect(page.getByRole('heading', { name: 'Сравнить варианты' })).toBeVisible();
  expect(calls[0]).toBe('/api/auth/telegram'); expect(calls).toContain('/api/sessions/fixture-trip');
  expect(calls).not.toContain('/api/auth/guest'); expect(calls).not.toContain('/api/sessions');
});

test('mock Back preserves drafts, stable viewport and safe area without overflow', async ({ page }) => {
  await telegramFixture(page); await page.goto('/');
  await expect.poll(() => page.evaluate(() => (window as any).miniappTest.stats.ready)).toBe(1);
  const narrow = ['mobile', 'narrow'].includes(test.info().project.name);
  if (narrow) await page.getByRole('button', { name: 'Обсудить поездку' }).click();
  const composer = page.getByRole('textbox', { name: 'Расскажите о путешествии' });
  await composer.fill('Несохранённое уточнение');
  await page.evaluate(() => (window as any).miniappTest.back());
  if (narrow) { await expect(composer).toBeHidden(); await page.getByRole('button', { name: 'Обсудить поездку' }).click(); }
  await expect(composer).toHaveValue('Несохранённое уточнение');
  await page.evaluate(() => { window.Telegram!.WebApp!.viewportStableHeight = 580; (window as any).miniappTest.emit('viewportChanged'); });
  expect(await page.evaluate(() => document.documentElement.style.getPropertyValue('--miniapp-height'))).toBe('580px');
  expect(await page.evaluate(() => document.documentElement.style.getPropertyValue('--miniapp-safe-top'))).toBe('18px');
  expect(await page.evaluate(() => document.documentElement.style.getPropertyValue('--miniapp-safe-bottom'))).toBe('16px');
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  expect(await composer.boundingBox()).not.toBeNull();
  if (narrow) expect((await page.getByRole('button', { name: 'Отправить запрос' }).boundingBox())!.y + (await page.getByRole('button', { name: 'Отправить запрос' }).boundingBox())!.height).toBeLessThanOrEqual(580);
  await page.screenshot({ path: test.info().outputPath('miniapp.png'), fullPage: true });
  await page.evaluate(() => { window.Telegram!.WebApp!.colorScheme = 'light'; window.Telegram!.WebApp!.themeParams = {}; (window as any).miniappTest.emit('themeChanged'); });
  expect(await page.evaluate(() => getComputedStyle(document.documentElement).getPropertyValue('--color-text').trim())).toBe('#31403b');
});

test('another-owner launch hint remains denied after successful authentication', async ({ page }) => {
  const calls = await telegramFixture(page);
  await page.route('**/api/sessions/fixture-trip', route => route.fulfill({ status: 404, json: { detail: 'Not found' } }));
  await page.goto('/');
  await expect(page.getByRole('alert')).toContainText('Поездка недоступна для текущего пользователя');
  expect(calls).not.toContain('/api/auth/guest'); expect(calls).not.toContain('/api/sessions');
  await expect.poll(() => page.evaluate(() => (window as any).miniappTest.stats.ready)).toBe(1);
});

test('mock bridge cleanup removes handlers before remount', async ({ page }) => {
  await telegramFixture(page); await page.goto('/');
  await expect.poll(() => page.evaluate(() => (window as any).miniappTest.stats.ready)).toBe(1);
  expect(await page.evaluate(() => (window as any).miniappTest.listeners())).toBe(4);
  await page.evaluate(() => window.dispatchEvent(new Event('one-request-auth-changed')));
  await expect.poll(() => page.evaluate(() => (window as any).miniappTest.stats.ready)).toBe(2);
  expect(await page.evaluate(() => (window as any).miniappTest.stats.removed)).toBeGreaterThanOrEqual(4);
  expect(await page.evaluate(() => (window as any).miniappTest.listeners())).toBe(4);
  expect(await page.evaluate(() => (window as any).miniappTest.backListeners())).toBe(1);
});

test('logout cleans SDK state and requires explicit re-entry', async ({ page }) => {
  const calls = await telegramFixture(page); await page.goto('/');
  await expect.poll(() => page.evaluate(() => (window as any).miniappTest.stats.ready)).toBe(1);
  if (['mobile', 'narrow'].includes(test.info().project.name)) await page.getByRole('button', { name: 'Обсудить поездку' }).click();
  await page.getByRole('button', { name: 'Завершить сеанс' }).click();
  await expect(page.getByRole('heading', { name: 'Сеанс завершён' })).toBeVisible();
  expect(calls.filter(path => path === '/api/auth/telegram')).toHaveLength(1);
  expect(await page.evaluate(() => (window as any).miniappTest.listeners())).toBe(0);
  expect(await page.evaluate(() => (window as any).miniappTest.backListeners())).toBe(0);
  expect(await page.evaluate(() => document.documentElement.dataset.telegramTheme)).toBeUndefined();
  expect(await page.evaluate(() => document.documentElement.style.getPropertyValue('--miniapp-height'))).toBe('');
  await page.getByRole('button', { name: 'Начать новый сеанс' }).click();
  await expect.poll(() => page.evaluate(() => (window as any).miniappTest.stats.ready)).toBe(2);
  expect(calls.filter(path => path === '/api/auth/telegram')).toHaveLength(2);
});

const officialSdk = 'https://telegram.org/js/telegram-web-app.js?64';
const sdkInitData = 'auth_date=fixture&user=fixture&hash=fixture-from-loaded-sdk';
const sdkCode = `window.Telegram = { WebApp: { initData: ${JSON.stringify(sdkInitData)}, ready() { window.sdkReadyCalls = (window.sdkReadyCalls || 0) + 1; } } };`;

async function sdkBootstrapFixture(page: Page) {
  // Deliberately no addInitScript installing Telegram.WebApp. The script must do it.
  const calls: string[] = [];
  page.on('request', request => { const path = new URL(request.url()).pathname; if (path.startsWith('/api/')) calls.push(path); });
  await installJourney(page);
  await page.route('**/api/auth/session', route => route.fulfill({ json: { user: { id: 'sdk-fixture-owner', kind: 'telegram' } } }));
  await page.route('**/api/auth/telegram', async route => {
    expect(route.request().postDataJSON()).toEqual({ init_data: sdkInitData });
    expect(await page.evaluate(() => !!window.Telegram?.WebApp?.initData)).toBe(true);
    await page.context().addCookies([{ name: 'one_request_csrf', value: 'fixture-csrf', url: 'http://127.0.0.1:4179', sameSite: 'Lax' }]);
    await route.fulfill({ json: { user: { id: 'sdk-fixture-owner', kind: 'telegram' }, csrf_token: 'fixture-csrf' } });
  });
  return calls;
}

test('SDK bootstrap loads official script before auth when WebApp was not injected', async ({ page }) => {
  const calls = await sdkBootstrapFixture(page);
  let release!: () => void;
  const held = new Promise<void>(resolve => { release = resolve; });
  await page.route(officialSdk, async route => { await held; await route.fulfill({ contentType: 'application/javascript', body: sdkCode }); });
  const sdkRequest = page.waitForRequest(officialSdk);
  await page.goto('/#tgWebAppData=untrusted-hash-is-only-a-launch-hint', { waitUntil: 'domcontentloaded' });
  const request = await sdkRequest;
  expect(request.url()).toBe(officialSdk); expect(request.headers()['referer']).toBeUndefined();
  expect(await page.evaluate(() => window.Telegram?.WebApp)).toBeUndefined();
  await expect(page.getByRole('status')).toHaveText('Открываем приложение…');
  expect(calls).toEqual([]);
  release();
  await expect(page.getByRole('heading', { name: 'Сравнить варианты' })).toBeVisible();
  expect(calls[0]).toBe('/api/auth/telegram'); expect(calls).not.toContain('/api/auth/guest');
  await expect.poll(() => page.evaluate(() => (window as any).sdkReadyCalls)).toBe(1);
  expect(await page.evaluate(() => JSON.stringify(localStorage))).not.toContain(sdkInitData);
  expect(await page.evaluate(() => JSON.stringify(localStorage))).not.toContain('untrusted-hash');
});

test('SDK bootstrap ordinary Web makes no SDK request and still uses guest auth', async ({ page }) => {
  const requests: string[] = [];
  page.on('request', request => requests.push(request.url()));
  await installJourney(page); await page.goto('/');
  await expect(page.getByRole('heading', { name: 'Сравнить варианты' })).toBeVisible();
  expect(requests.some(url => url.startsWith('https://telegram.org/'))).toBe(false);
  expect(requests.some(url => url.endsWith('/api/auth/guest'))).toBe(true);
  expect(await page.evaluate(() => window.Telegram?.WebApp)).toBeUndefined();
});

test('SDK bootstrap existing signed WebApp skips the external script even with launch hash', async ({ page }) => {
  const calls = await telegramFixture(page); const sdkRequests: string[] = [];
  page.on('request', request => { if (request.url().startsWith('https://telegram.org/')) sdkRequests.push(request.url()); });
  await page.goto('/#tgWebAppData=fixture-launch-hint');
  await expect(page.getByRole('heading', { name: 'Сравнить варианты' })).toBeVisible();
  expect(sdkRequests).toEqual([]); expect(calls[0]).toBe('/api/auth/telegram');
});

test('SDK bootstrap network failure never creates a guest and retry reloads the SDK', async ({ page }) => {
  const calls = await sdkBootstrapFixture(page); let attempts = 0;
  await page.route(officialSdk, route => ++attempts === 1 ? route.abort('failed') : route.fulfill({ contentType: 'application/javascript', body: sdkCode }));
  await page.goto('/#tgWebAppData=fixture-launch-hint');
  await expect(page.getByRole('alert')).toHaveText('Откройте Mini App заново или повторите загрузку.');
  expect(calls).toEqual([]); expect(await page.evaluate(() => localStorage.getItem('one-request-session'))).toBeNull();
  await page.getByRole('button', { name: 'Повторить загрузку' }).click();
  await expect(page.getByRole('heading', { name: 'Сравнить варианты' })).toBeVisible();
  expect(attempts).toBe(2); expect(calls[0]).toBe('/api/auth/telegram'); expect(calls).not.toContain('/api/auth/guest');
});

test('SDK bootstrap onload without nonempty initData blocks every API', async ({ page }) => {
  const calls = await sdkBootstrapFixture(page);
  await page.route(officialSdk, route => route.fulfill({ contentType: 'application/javascript', body: 'window.Telegram = { WebApp: { initData: "" } };' }));
  await page.goto('/#tgWebAppData=fixture-launch-hint');
  await expect(page.getByRole('heading', { name: 'Не удалось открыть Telegram' })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Повторить загрузку' })).toBeVisible();
  expect(calls).toEqual([]);
});

test('SDK bootstrap eight second timeout stays blocked even if the script arrives late', async ({ page }) => {
  const calls = await sdkBootstrapFixture(page);
  await page.clock.install();
  let release!: () => void, finished!: () => void;
  const held = new Promise<void>(resolve => { release = resolve; });
  const completed = new Promise<void>(resolve => { finished = resolve; });
  await page.route(officialSdk, async route => {
    await held;
    await route.fulfill({ contentType: 'application/javascript', body: sdkCode }).catch(() => {});
    finished();
  });
  const sdkRequest = page.waitForRequest(officialSdk);
  await page.goto('/#tgWebAppData=fixture-launch-hint', { waitUntil: 'domcontentloaded' });
  await sdkRequest; await page.clock.fastForward(8_001);
  await expect(page.getByRole('heading', { name: 'Не удалось открыть Telegram' })).toBeVisible();
  expect(calls).toEqual([]);
  release(); await completed;
  await expect(page.getByRole('heading', { name: 'Не удалось открыть Telegram' })).toBeVisible();
  expect(calls).toEqual([]);
});
