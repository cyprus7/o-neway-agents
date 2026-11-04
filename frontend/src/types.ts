export type AuthSession = { user: { id: string; kind: string }; csrf_token?: string };
export type Evidence = { provider: string; source_url: string | null; retrieved_at: string; freshness: string; confidence: string; label: string };
export type PriceValue =
  | { kind: 'point'; amount: string; currency: string }
  | { kind: 'closed_range'; min: string; max: string; currency: string }
  | { kind: 'lower_bound'; min: string; max?: null; currency: string }
  | { kind: 'unknown'; reason: string };
export type QuoteScope = {
  component: string; entity_level: string; entity_id?: string | null;
  origin?: string | null; destination?: string | null; destination_city?: string | null;
  date_from?: string | null; date_to?: string | null; adults?: number | null;
  children_ages?: number[] | null; rooms?: number | null;
  trip_type: 'roundtrip' | 'one_way' | 'not_applicable';
  unit: 'group_trip' | 'group_stay' | 'person_trip' | 'room_night' | 'group_night' | 'unknown';
  currency?: string | null; context_verified: boolean; unverified_fields?: string[];
  assumptions?: string[]; inclusions?: string[]; exclusions?: string[];
};
export type PriceObservation = {
  id: string; provider_id: string; scope?: QuoteScope | null; value: PriceValue;
  normalized_group?: PriceValue | null; source_url?: string | null;
  observed_at?: string | null; retrieved_at: string; expires_at?: string | null;
  outcome: string; freshness: 'live' | 'cached' | 'stale' | 'unavailable';
  reason?: string | null; unit_label?: string | null; included_charges: string[];
  excluded_charges: string[]; unknown_charges: string[]; component_complete: boolean;
  availability_verified: boolean; normalization_note?: string | null;
};
export type PricingSummary = {
  display_kind: string; transport_stay?: PriceValue | null; known_partial?: PriceValue | null;
  full_trip?: PriceValue | null; included_components: string[]; missing_components: string[];
  reference_components: string[]; missing_reasons: Record<string, string>;
  excluded_charges: string[]; unknown_charges: string[]; currency: string; scope_label: string;
  as_of?: string | null; expires_at?: string | null; assumptions: string[];
  budget_assessment: string; budget_explanation: string; hard_constraint_status: string;
};
export type Component = { title: string; description: string; price: number | null; currency: string; source_url: string | null; status: string; details?: Record<string, unknown>; quote?: PriceObservation | null };
export type RegionalEventSource = { provider: string; source_url: string; retrieved_at: string; freshness: 'live' | 'cached' };
export type RegionalEvent = {
  id: string; title: string; start_date: string; end_date: string;
  location_name: string; location_city: string;
  category: 'culture' | 'music' | 'sport' | 'exhibition' | 'other';
  significance: 'experience' | 'plan_ahead'; reason: string;
  source: RegionalEventSource; verification: 'source_reported';
};
export type RegionalEventResult = {
  status: 'ready' | 'empty' | 'unavailable' | 'dates_required' | 'not_checked';
  items: RegionalEvent[]; checked_at?: string | null;
};
export type DemoComponentPrice = {
  kind: 'flight' | 'stay' | 'transfer'; value: Extract<PriceValue, { kind: 'closed_range' }>;
  example_amount_minor: number; basis: 'comparable_source' | 'synthetic_demo';
  source_url: string | null; source_observed_at: string | null; assumptions: string[];
};
export type DemoPricing = {
  schema_version: 'demo-pricing.v1'; basis: 'demo_estimate'; currency: string;
  components: DemoComponentPrice[]; total: Extract<PriceValue, { kind: 'closed_range' }>;
  service_fee: Extract<PriceValue, { kind: 'closed_range' }>; example_service_fee_minor: number;
  example_total_minor: number; scope_label: string;
  budget_assessment: 'within_range' | 'crosses_range' | 'over_range' | 'not_requested' | 'currency_mismatch';
  budget_explanation: string; assumptions: string[];
};
export type Option = { id: string; destination: string; country: string; country_code?: string | null; destination_scope?: 'locality' | 'country'; destination_aliases?: string[]; subtitle: string; score: number; estimated_total: number | null; currency: string; price_status: string; flight: Component; stay: Component; transfer: Component; weather: Record<string, unknown> | null; reasons: string[]; tradeoffs: string[]; risks: string[]; evidence: Evidence[]; constraint_checks: Array<Record<string, unknown>>; booking_url?: string; pricing_summary?: PricingSummary | null; regional_events?: RegionalEventResult | null; demo_pricing?: DemoPricing | null };
export type Intent = { origin: string | null; destination: string | null; date_from: string | null; date_to: string | null; flight_departure_from?: string | null; flight_departure_to?: string | null; nights_min: number | null; nights_max: number | null; adults: number; children_ages: number[]; pets: string[]; budget_total: number | null; currency: string; climate_preferences: string[]; trip_styles: string[]; max_travel_minutes: number | null; hotel_requirements: string[] };
export type Session = { id: string; revision: number; accepted_plan_revision?: number | null; previous_plan?: { revision: number; intent: Intent; options: Option[] } | null; intent: Intent; messages: { id: string; role: 'user' | 'assistant'; content: string; created_at: string }[]; options: Option[]; events: { id: string; step: string; status: string; message: string; created_at: string }[]; selected_option_id: string | null; assumptions: string[]; clarification: string | null; last_changes: string[]; preferences: Record<string, unknown> };
export type Config = { llm_configured: boolean; providers: Record<string, unknown> };
export type DemoInvoice = {
  quote_id: string; context_id: string; session_id: string; option_id: string; revision: number;
  destination: string; scope_label: string; expires_at: string; pricing_basis: 'synthetic_demo'; currency: string;
  line_items: { key: string; label: string; detail: string; amount_minor: number }[];
  subtotal_minor: number; service_fee_minor: number; service_fee_label: string; tax_label: string;
  total_minor: number; disclaimer: string; unknown_real_conditions: string[]; source_total: PriceValue | null;
};
export type DemoReceipt = {
  id: string; message: string; created_at: string; status: 'simulated'; ready_to_book: false;
  demo_payment: { status: 'simulated_paid'; invoice: DemoInvoice; paid_amount_minor: number; currency: string; card_label: string };
};
export type Job = { id: string; session_id: string; kind: 'message' | 'prices_refresh'; status: 'queued' | 'running' | 'completed' | 'failed' | 'superseded' | 'canceled' | 'interrupted'; error?: string | null; progress?: string | null; captured_revision?: number | null; result_revision?: number | null; target_option_ids?: string[]; result_option_ids?: string[] };
