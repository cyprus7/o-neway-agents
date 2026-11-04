import { expect, test, type Browser, type BrowserContext, type Frame, type Page } from '@playwright/test';
import { spawn, type ChildProcess } from 'node:child_process';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

// Real loopback HTTPS, real API and native EventSource. No route mocks. The
// disposable self-signed fixture certificate is the only ignored TLS error;
// production browser tests never inherit this setting.
test.describe('cross-site HTTPS Telegram iframe', () => {
  test.skip(process.env.PRODUCT_IFRAME_INTEGRATION !== '1', 'Explicit isolated iframe fixture opt-in required');
  test.describe.configure({ mode: 'serial' });
  let server: ChildProcess;
  let context: BrowserContext;
  test.afterEach(async () => {
    await context?.close();
    if (server && server.exitCode === null) {
      const stopped = new Promise(resolve => server.once('exit', resolve));
      server.kill(); await stopped;
    }
  });

  async function start(browser: Browser, legacy: boolean, seedReminders = false) {
    const root = fileURLToPath(new URL('../../', import.meta.url));
    const python = process.env.PRODUCT_TEST_PYTHON || path.join(root, process.platform === 'win32' ? '.venv/Scripts/python.exe' : '.venv/bin/python');
    server = spawn(python, [path.join(root, 'scripts/product_iframe_fixture.py'), ...(legacy ? ['--legacy-lax'] : []), ...(seedReminders ? ['--seed-reminders'] : [])], { cwd: root, stdio: 'ignore' });
    context = await browser.newContext({ ignoreHTTPSErrors: true, viewport: test.info().project.use.viewport });
    await context.addCookies([{ name: 'iframe_unpartitioned_control', value: 'synthetic', url: 'https://localhost:18443', secure: true, sameSite: 'None' }]);
    await expect.poll(async () => {
      try { return (await context.request.get('https://localhost:18443/health', { timeout: 500 })).status(); } catch { return 0; }
    }, { timeout: 20_000 }).toBe(200);
    const page = await restrictedPage(context);
    return page;
  }
  async function restrictedPage(owner: BrowserContext) {
    const page = await owner.newPage();
    const cdp = await owner.newCDPSession(page);
    await cdp.send('Network.enable');
    await cdp.send('Network.setCookieControls', { enableThirdPartyCookieRestriction: true, disableThirdPartyCookieMetadata: true, disableThirdPartyCookieHeuristics: true });
    return page;
  }
  async function child(page: Page, user = 'alice', expired = false) {
    await page.goto(`https://127.0.0.1:18444/?user=${user}&expired=${Number(expired)}`);
    await expect(page.locator('iframe')).toBeVisible();
    await expect.poll(() => page.frames().find(frame => frame.url().startsWith('https://localhost:18443/app/')) !== undefined).toBe(true);
    return page.frames().find(frame => frame.url().startsWith('https://localhost:18443/app/'))!;
  }
  async function status(frame: Frame, route: string, write = false, csrf = true) {
    return frame.evaluate(async ({ route, write, csrf }) => {
      const token = document.cookie.split('; ').find(value => value.startsWith('one_request_csrf='))?.split('=').slice(1).join('=') || '';
      const response = await fetch(route, write ? { method: 'POST', headers: { 'Content-Type': 'application/json', ...(csrf ? { 'X-CSRF-Token': decodeURIComponent(token) } : {}) }, body: '{}' } : {});
      return response.status;
    }, { route, write, csrf });
  }

  test('canonical reminder API state survives real aggregate and reopened UI without false cancel actions', async ({ browser }) => {
    const page = await start(browser, false, true);
    let frame = await child(page);
    await expect(frame.locator('textarea')).toBeEnabled();
    const trip = await frame.evaluate(() => localStorage.getItem('one-request-session'));
    const projection = await frame.evaluate(async trip => {
      const response = await fetch(`/api/sessions/${trip}/aftercare`);
      const data = await response.json();
      return { status: response.status, reminders: data.reminders.map((item: Record<string, unknown>) => ({ state: item.state, hasStatus: 'status' in item })) };
    }, trip);
    expect(projection.status).toBe(200);
    expect(projection.reminders.map((item: { state: string }) => item.state).sort()).toEqual(['canceled', 'delivery_unknown', 'failed', 'scheduled', 'sending', 'sent']);
    expect(projection.reminders.every((item: { hasStatus: boolean }) => !item.hasStatus)).toBe(true);
    const cases = [
      ['scheduled', 'Запланировано', true], ['sending', 'Отправка выполняется', true],
      ['sent', 'Отправлено в Telegram', false], ['canceled', 'Отменено', false],
      ['failed', 'Не удалось отправить', false], ['delivery_unknown', 'Доставка не подтверждена', false],
    ] as const;
    for (let opening = 0; opening < 2; opening++) {
      if (opening) {
        await page.reload();
        frame = page.frames().find(value => value.url().startsWith('https://localhost:18443/app/'))!;
        await expect(frame.locator('textarea')).toBeEnabled();
      }
      await frame.getByRole('button', { name: 'Квитанции и заметки' }).click();
      const care = frame.getByRole('region', { name: 'Демо-квитанции и заметки к поездке' });
      for (const [state, label, cancellable] of cases) {
        const row = care.getByRole('listitem').filter({ has: frame.getByText(`Canonical reminder ${state}`, { exact: true }) });
        await expect(row.locator('strong')).toHaveText(label);
        await expect(row.getByRole('button', { name: /Отменить напоминание/ })).toHaveCount(cancellable ? 1 : 0);
        if (state === 'delivery_unknown') await expect(row).not.toContainText('Отправлено в Telegram');
      }
    }
    const care = frame.getByRole('region', { name: 'Демо-квитанции и заметки к поездке' });
    for (const [state, label] of [['scheduled', 'Отменено'], ['sending', 'Доставка не подтверждена']] as const) {
      await care.getByRole('button', { name: `Отменить напоминание Canonical reminder ${state}`, exact: true }).click();
      const row = care.getByRole('listitem').filter({ has: frame.getByText(`Canonical reminder ${state}`, { exact: true }) });
      await expect(row.locator('strong')).toHaveText(label);
      await expect(row.getByRole('button')).toHaveCount(0);
    }
  });

  test('historical Lax fails once, without guest fallback or a private request loop', async ({ browser }) => {
    const page = await start(browser, true);
    const calls: string[] = [];
    page.on('request', request => { const url = new URL(request.url()); if (url.pathname.startsWith('/api/')) calls.push(url.pathname); });
    const frame = await child(page);
    await expect(frame.getByRole('alert')).toContainText('Не удалось подтвердить вход');
    await expect(frame.getByRole('button', { name: 'Закрыть приложение', exact: true })).toBeVisible();
    await page.waitForTimeout(700);
    expect(calls).toEqual(['/api/auth/telegram', '/api/auth/session']);
    expect(await frame.evaluate(() => localStorage.getItem('one-request-session'))).toBeNull();
  });

  test('signed expired launch without credentials displays terminal ready error and SDK close', async ({ browser }) => {
    const page = await start(browser, false);
    const calls: string[] = [];
    page.on('request', request => { const route = new URL(request.url()).pathname; if (route.startsWith('/api/')) calls.push(route); });
    const frame = await child(page, 'alice', true);
    await expect(frame.getByRole('alert')).toContainText('Срок действия запуска Telegram истёк');
    await expect.poll(() => frame.evaluate(() => (window as any).iframeStats.ready)).toBe(1);
    expect(await frame.evaluate(() => (window as any).iframeStats.errorOnReady)).toBe(true);
    await expect(frame.locator('textarea')).toBeDisabled();
    await expect(frame.getByRole('button', { name: 'Повторить вход' })).toHaveCount(0);
    await frame.getByRole('button', { name: 'Закрыть приложение', exact: true }).click();
    expect(await frame.evaluate(() => (window as any).iframeStats.closed)).toBe(1);
    expect(calls).toEqual(['/api/auth/telegram']);
    expect((await context.cookies()).filter(cookie => cookie.name.startsWith('one_request_'))).toHaveLength(0);
  });

  test('expired signed reopen resumes same valid cookie with CSRF without renewing credentials', async ({ browser }) => {
    const page = await start(browser, false);
    let frame = await child(page);
    await expect(frame.locator('textarea')).toBeEnabled();
    await expect(frame.getByRole('status').filter({ hasText: 'подключены' })).toBeVisible();
    const trip = await frame.evaluate(() => localStorage.getItem('one-request-session'));
    const before = (await context.cookies()).filter(cookie => cookie.name.startsWith('one_request_'));
    const resumed = page.waitForResponse(response => new URL(response.url()).pathname === '/api/auth/telegram');
    frame = await child(page, 'alice', true);
    const response = await resumed;
    expect(response.status()).toBe(200);
    expect(response.headers()['set-cookie']).toBeUndefined();
    await expect(frame.locator('textarea')).toBeEnabled();
    await expect(frame.getByRole('status').filter({ hasText: 'подключены' })).toBeVisible();
    expect(await frame.evaluate(() => localStorage.getItem('one-request-session'))).toBe(trip);
    const after = (await context.cookies()).filter(cookie => cookie.name.startsWith('one_request_'));
    expect(after.length === 2 && before.every(old => after.some(current => old.name === current.name && old.value === current.value && old.expires === current.expires))).toBe(true);
    expect(await status(frame, '/api/auth/session')).toBe(200);
    expect(await status(frame, '/api/sessions', true)).toBe(201);
    expect(await frame.evaluate(() => JSON.stringify(localStorage).includes('auth_date'))).toBe(false);
  });

  test('partitioned credentials support GET, CSRF writes, SSE, reopen, logout and owner isolation', async ({ browser }) => {
    const page = await start(browser, false);
    let frame = await child(page);
    await expect(frame.locator('textarea')).toBeEnabled();
    await expect(frame.getByRole('status').filter({ hasText: 'подключены' })).toBeVisible();
    // Prove third-party blocking is active, not merely accepted by CDP: a
    // SameSite=None cookie without Partitioned must remain inaccessible here.
    expect(await frame.evaluate(() => document.cookie.includes('iframe_unpartitioned_control='))).toBe(false);
    const trip = await frame.evaluate(() => localStorage.getItem('one-request-session'));
    expect(!!trip).toBe(true);
    const cookies = (await context.cookies()).filter(cookie => cookie.name.startsWith('one_request_'));
    expect(cookies).toHaveLength(2);
    expect(cookies.every(cookie => cookie.secure && cookie.sameSite === 'None' && cookie.partitionKey === 'https://127.0.0.1')).toBe(true);
    expect(cookies.find(cookie => cookie.name === 'one_request_auth')?.httpOnly).toBe(true);
    expect(cookies.find(cookie => cookie.name === 'one_request_csrf')?.httpOnly).toBe(false);
    expect(await status(frame, '/api/auth/session')).toBe(200);
    expect(await status(frame, '/api/sessions', true, false)).toBe(403);
    expect(await status(frame, '/api/sessions', true)).toBe(201);
    expect(await frame.evaluate(() => Object.keys(localStorage).every(key => ['one-request-session', 'one-request-job'].includes(key)))).toBe(true);
    await page.reload(); frame = page.frames().find(value => value.url().startsWith('https://localhost:18443/app/'))!;
    await expect(frame.locator('textarea')).toBeEnabled();
    expect(await frame.evaluate(() => localStorage.getItem('one-request-session'))).toBe(trip);
    if (!(await frame.getByRole('button', { name: 'Завершить сеанс' }).isVisible())) {
      await frame.getByRole('button', { name: 'Обсудить поездку' }).click();
    }
    await frame.getByRole('button', { name: 'Завершить сеанс' }).click();
    await expect(frame.getByRole('heading', { name: 'Сеанс завершён' })).toBeVisible();
    expect(await status(frame, '/api/auth/session')).toBe(401);
    await frame.getByRole('button', { name: 'Начать новый сеанс' }).click();
    await expect(frame.locator('textarea')).toBeEnabled();
    expect(await status(frame, `/api/sessions/${trip}`)).toBe(200);
    const other = await browser.newContext({ ignoreHTTPSErrors: true, viewport: test.info().project.use.viewport });
    try {
      const otherPage = await restrictedPage(other);
      const otherFrame = await child(otherPage, 'bob');
      await expect(otherFrame.locator('textarea')).toBeEnabled();
      expect(await status(otherFrame, `/api/sessions/${trip}`)).toBe(404);
      expect(await status(otherFrame, `/api/sessions/${trip}/receipts`)).toBe(404);
    } finally { await other.close(); }
  });
});
