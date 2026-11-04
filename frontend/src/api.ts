import type { AuthSession } from './types';
import { telegramInitData } from './telegram/bridge';

export class ApiError extends Error {
  constructor(message: string, public status: number, public code?: string) { super(message); }
}
let epoch = 0;
let bootstrap: Promise<AuthSession> | null = null;
let identityPhase: 'unverified' | 'bootstrapping' | 'established' | 'reentry_required' = 'unverified';
export const AUTH_CHANGED = 'one-request-auth-changed';
function resetIdentity(signedOut = true) {
  epoch++; bootstrap = null;
  identityPhase = 'reentry_required';
  localStorage.removeItem('one-request-session'); localStorage.removeItem('one-request-job');
  const url = new URL(window.location.href); url.searchParams.delete('trip'); window.history.replaceState(null, '', url);
  window.dispatchEvent(new CustomEvent(AUTH_CHANGED, { detail: { signedOut } }));
}
function csrfToken() {
  const raw = document.cookie.split('; ').find(value => value.startsWith('one_request_csrf='))?.split('=').slice(1).join('=');
  return raw ? decodeURIComponent(raw) : '';
}
export async function api<T>(path: string, body?: unknown, options: { idempotencyKey?: string; method?: 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE' } = {}): Promise<T> {
  const capturedEpoch = epoch;
  const headers: Record<string, string> = { 'Content-Type': 'application/json' };
  const method = options.method || (body === undefined ? 'GET' : 'POST');
  if (method !== 'GET') {
    headers['X-CSRF-Token'] = csrfToken();
    headers['Idempotency-Key'] = options.idempotencyKey || crypto.randomUUID();
  }
  const response = await fetch(path, { method, credentials: 'same-origin', headers, ...(body === undefined ? {} : { body: JSON.stringify(body) }) });
  if (capturedEpoch !== epoch) throw new ApiError('Сеанс пользователя изменился.', 401, 'identity_changed');
  if (!response.ok) {
    const bootstrapPrivate401 = response.status === 401 && identityPhase === 'bootstrapping'
      && !['/api/auth/guest', '/api/auth/telegram'].includes(path);
    // Hide private state immediately after established access expires. A failed
    // bootstrap must never remount App and repeat the login automatically.
    if (response.status === 401 && identityPhase === 'established') resetIdentity(true);
    const error = await response.json().catch(() => ({}));
    if (bootstrapPrivate401) throw new ApiError('Не удалось подтвердить вход. Откройте Mini App заново в Telegram или откройте сайт в отдельном браузере.', 401, 'bootstrap_not_confirmed');
    throw new ApiError(typeof error.detail === 'string' ? error.detail : typeof error.detail?.message === 'string' ? error.detail.message : `Не удалось выполнить запрос (${response.status}). Попробуйте ещё раз.`, response.status, error.detail?.code);
  }
  const result = response.status === 204 ? undefined as T : await response.json() as T;
  if (capturedEpoch !== epoch) throw new ApiError('Сеанс пользователя изменился.', 401, 'identity_changed');
  return result;
}
function beginBootstrap(path: string, body: unknown) {
  if (bootstrap) return bootstrap;
  const capturedEpoch = epoch;
  identityPhase = 'bootstrapping';
  const attempt = (async () => {
    const login = await api<AuthSession>(path, body);
    const confirmed = await api<AuthSession>('/api/auth/session');
    if (confirmed.user.id !== login.user.id) throw new ApiError('Вход изменился. Откройте приложение заново.', 401, 'bootstrap_identity_mismatch');
    return login;
  })().catch(reason => {
    if (capturedEpoch === epoch) identityPhase = 'unverified';
    throw reason;
  }).finally(() => { if (bootstrap === attempt) bootstrap = null; });
  bootstrap = attempt;
  return bootstrap;
}
export const ensureGuest = () => beginBootstrap('/api/auth/guest', {});
export function ensureIdentity() {
  const initData = telegramInitData();
  if (!initData) return ensureGuest();
  return beginBootstrap('/api/auth/telegram', { init_data: initData });
}
export function completeIdentityBootstrap() { if (identityPhase !== 'reentry_required') identityPhase = 'established'; }
export function failIdentityBootstrap() { if (identityPhase === 'bootstrapping') identityPhase = 'unverified'; }
export const currentIdentity = () => api<AuthSession>('/api/auth/session');
export async function logout() { await api('/api/auth/logout', {}); resetIdentity(true); }
export const clearDeletedIdentity = () => resetIdentity(true);
