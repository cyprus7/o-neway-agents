import type { Page } from '@playwright/test';
import { installJourney, invoice, sessionFixture } from './journey';

type FixtureReminder = { id: string; text: string; state: 'scheduled' | 'sending' | 'sent' | 'canceled' | 'failed' | 'delivery_unknown'; local_time: string; timezone: string };

export async function installAftercare(page: Page, remindersAvailable = true) {
  await installJourney(page);
  const receipt = { id: 'persisted-fixture-receipt', message: 'Симуляция', created_at: '2026-10-28T10:00:00Z', status: 'simulated', ready_to_book: false, demo_payment: { status: 'simulated_paid', invoice, paid_amount_minor: invoice.total_minor, currency: invoice.currency, card_label: 'Вымышленная карта' }, itinerary: { intent: sessionFixture.intent, option: sessionFixture.options[0] } };
  const data = { receipts: [receipt, { ...receipt, id: 'legacy-fixture-receipt', itinerary: null }], drafts: [] as any[], manual_context: null as any, reminders_available: remindersAvailable,
    reminders: remindersAvailable ? [{ id: 'unknown-delivery', text: 'Предыдущее напоминание с неизвестной доставкой', state: 'delivery_unknown', local_time: '2026-11-01T10:00', timezone: 'Asia/Nicosia' }] as FixtureReminder[] : [] };
  const requests: { path: string; method: string; body: any }[] = [];
  await page.route('**/api/sessions/**', async route => {
    const path = new URL(route.request().url()).pathname; const method = route.request().method();
    let body: any; try { body = route.request().postDataJSON(); } catch { body = null; }
    requests.push({ path, method, body });
    if (method !== 'GET' && route.request().headers()['x-csrf-token'] !== 'fixture-csrf') return route.fulfill({ status: 403, json: { detail: 'Missing CSRF' } });
    let result: unknown;
    if (path.endsWith('/aftercare')) result = data;
    else if (path.endsWith('/drafts') && method === 'POST') { const draft = { id: `draft-${data.drafts.length}`, ...body, status: 'draft', created_at: '2026-10-28T10:00:00Z' }; data.drafts.push(draft); result = draft; }
    else if (path.includes('/drafts/') && method === 'PATCH') { const draft = data.drafts.find(item => path.endsWith(item.id)); draft.status = body.status; result = draft; }
    else if (path.endsWith('/manual-context')) { data.manual_context = { ...body, provenance: 'user_provided_unverified' }; result = data.manual_context; }
    else if (path.endsWith('/amend')) result = { session: { ...sessionFixture, id: 'linked-fixture-trip', options: [], messages: [] }, job_id: 'fixture-amend-job' };
    else if (path.endsWith('/reminders') && method === 'POST') { if (!body.consent || !remindersAvailable) return route.fulfill({ status: 422, json: { detail: 'Необходимо согласие и доступный канал' } }); const reminder = { id: `reminder-${data.reminders.length}`, ...body, state: 'scheduled' }; data.reminders.push(reminder); result = reminder; }
    else if (path.includes('/reminders/') && method === 'DELETE') { const reminder = data.reminders.find(item => path.endsWith(item.id)); if (reminder.state === 'scheduled') reminder.state = 'canceled'; else if (reminder.state === 'sending') reminder.state = 'delivery_unknown'; return route.fulfill({ status: 204 }); }
    else return route.fallback();
    return route.fulfill({ json: result });
  });
  return { data, requests };
}
