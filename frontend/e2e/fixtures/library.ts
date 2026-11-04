import type { Page } from '@playwright/test';
import { installJourney, sessionFixture } from './journey';

export async function installLibrary(page: Page) {
  await installJourney(page);
  const requests: { path: string; method: string; body: any }[] = [];
  const one = { ...sessionFixture, id: 'fixture-trip', messages: [{ id: 'one', role: 'assistant' as const, content: 'Первая личная поездка', created_at: '2026-10-28T10:00:00Z' }] };
  const two = { ...sessionFixture, id: 'library-two', messages: [{ id: 'two', role: 'assistant' as const, content: 'Вторая личная поездка', created_at: '2026-10-28T10:00:00Z' }] };
  let current = one;
  let memory: any[] = [];
  const listed = (id: string, title: string) => ({ id, title, destination: 'Пафос', revision: 1, created_at: '2026-10-28T10:00:00Z', updated_at: '2026-10-28T10:00:00Z' });
  let trips = [listed(one.id, 'Первая поездка'), listed(two.id, 'Вторая поездка')];
  await page.route('**/api/**', async route => {
    const url = new URL(route.request().url()); const path = url.pathname; const method = route.request().method();
    let body: any; try { body = route.request().postDataJSON(); } catch { body = null; }
    requests.push({ path: path + url.search, method, body });
    let response: unknown;
    if (method !== 'GET' && !path.startsWith('/api/auth/') && route.request().headers()['x-csrf-token'] !== 'fixture-csrf') return route.fulfill({ status: 403, json: { detail: 'Missing CSRF' } });
    if (path === '/api/sessions' && method === 'GET') response = url.searchParams.has('cursor') ? { items: trips.slice(1), next_cursor: null } : { items: trips.slice(0, 1), next_cursor: 'fixture-next' };
    else if (path === '/api/sessions' && method === 'POST') { current = body.apply_memory === undefined ? one : { ...one, id: `fixture-created-${requests.length}`, preferences: { applied_memory_fields: body.apply_memory ? memory.map(item => item.field) : [] } }; response = current; }
    else if (path === `/api/sessions/${two.id}` && method === 'GET') { current = two; response = current; }
    else if (path === `/api/sessions/${one.id}` && method === 'GET') { current = one; response = current; }
    else if (path.startsWith('/api/sessions/') && method === 'DELETE') { trips = trips.filter(item => !path.endsWith(item.id)); return route.fulfill({ status: 204 }); }
    else if (path === '/api/fixture-snapshot') response = { session: current, sequence: 1, job: null, server_time: '2026-10-28T10:00:00Z' };
    else if (path === '/api/me/memory') response = { items: memory };
    else if (path.startsWith('/api/me/memory/') && method === 'PUT') { const field = path.split('/').at(-1)!; memory = [...memory.filter(item => item.field !== field), { field, ...body, consented_at: '2026-10-28T10:00:00Z', updated_at: '2026-10-28T10:00:00Z' }]; response = memory.at(-1); }
    else if (path.startsWith('/api/me/memory/') && method === 'DELETE') { memory = memory.filter(item => !path.endsWith(item.field)); return route.fulfill({ status: 204 }); }
    else if (path === '/api/me/export') response = { user: { id: 'fixture-user' }, trips, memory };
    else if (path === '/api/me' && method === 'DELETE') return route.fulfill({ status: 204 });
    else return route.fallback();
    return route.fulfill({ json: response });
  });
  return requests;
}
