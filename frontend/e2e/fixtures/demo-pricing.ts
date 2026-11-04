import type { DemoInvoice, DemoPricing, Session } from '../../src/types';
import { invoice, sessionFixture } from './journey';

const range = (min: string, max: string) => ({ kind: 'closed_range' as const, min, max, currency: 'EUR' });
export const demoPricing: DemoPricing = {
  schema_version: 'demo-pricing.v1', basis: 'demo_estimate', currency: 'EUR',
  components: [
    { kind: 'flight', value: range('300', '500'), example_amount_minor: 40000, basis: 'synthetic_demo', source_url: null, source_observed_at: null, assumptions: ['Вымышленный перелёт туда и обратно для двух взрослых.'] },
    { kind: 'stay', value: range('400', '600'), example_amount_minor: 50000, basis: 'comparable_source', source_url: 'https://hotel.example/rates', source_observed_at: '2024-01-02T10:00:00Z', assumptions: ['Сопоставимая цена: даты и наличие для текущей группы не подтверждены.'] },
    { kind: 'transfer', value: range('60', '100'), example_amount_minor: 8000, basis: 'synthetic_demo', source_url: null, source_observed_at: null, assumptions: ['Вымышленный трансфер туда и обратно.'] },
  ],
  total: range('779', '1230'), service_fee: range('19', '30'), example_service_fee_minor: 2450, example_total_minor: 100450,
  scope_label: '2 взрослых · 5 ночей · вся поездка', budget_assessment: 'crosses_range',
  budget_explanation: 'Демо-диапазон пересекает бюджет 900 EUR.', assumptions: ['Иллюстрация, не предложение поставщика.'],
};
export const demoSession: Session = {
  ...sessionFixture, intent: { ...sessionFixture.intent, budget_total: 900 },
  options: sessionFixture.options.map(option => ({ ...option, demo_pricing: demoPricing,
    constraint_checks: [{ constraint: 'Перевозка питомца', status: 'unknown', detail: 'Правила перевозчика не подтверждены.' }],
  })),
};
export const alignedInvoice: DemoInvoice = { ...invoice,
  line_items: [
    { key: 'flight', label: 'Демо-перелёт', detail: 'Примерная сумма оценки', amount_minor: 40000 },
    { key: 'stay', label: 'Демо-проживание', detail: 'Примерная сумма оценки', amount_minor: 50000 },
    { key: 'transfer', label: 'Демо-трансфер', detail: 'Примерная сумма оценки', amount_minor: 8000 },
  ], subtotal_minor: 98000, service_fee_minor: 2450, total_minor: 100450,
};
