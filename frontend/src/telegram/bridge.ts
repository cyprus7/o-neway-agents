import { useEffect, useRef } from 'react';

type Insets = { top?: number; right?: number; bottom?: number; left?: number };
type WebApp = {
  initData?: string; colorScheme?: 'light' | 'dark'; themeParams?: Record<string, string>;
  viewportStableHeight?: number; safeAreaInset?: Insets; contentSafeAreaInset?: Insets;
  ready?: () => void; close?: () => void; onEvent?: (event: string, handler: () => void) => void;
  offEvent?: (event: string, handler: () => void) => void;
  BackButton?: { show?: () => void; hide?: () => void; onClick?: (handler: () => void) => void; offClick?: (handler: () => void) => void };
};
declare global { interface Window { Telegram?: { WebApp?: WebApp } } }
export const telegramApp = () => window.Telegram?.WebApp;
export const telegramInitData = () => { const value = telegramApp()?.initData; return typeof value === 'string' && value.trim() ? value : ''; };
export const canCloseTelegram = () => typeof telegramApp()?.close === 'function';
export function closeTelegram() { telegramApp()?.close?.(); }

/** Navigation hint only, read after the server has authenticated the signed data. */
export function telegramTripHint() {
  const value = (new URLSearchParams(telegramInitData()).get('start_param') || new URLSearchParams(window.location.search).get('start_param'))?.replace(/^trip_/, '');
  return value && /^[a-zA-Z0-9_-]{1,128}$/.test(value) ? value : null;
}

export function useTelegramBridge(usable: boolean, onBack: () => void, terminalError = false) {
  const callback = useRef(onBack); callback.current = onBack;
  const readyApp = useRef<WebApp | null>(null);
  useEffect(() => {
    const app = telegramApp();
    if (!app || !telegramInitData()) return;
    const root = document.documentElement;
    const originalTheme = root.dataset.telegramTheme;
    const previous = new Map<string, string>();
    const set = (name: string, value: string) => {
      if (!previous.has(name)) previous.set(name, root.style.getPropertyValue(name));
      root.style.setProperty(name, value);
    };
    const update = () => {
      for (const [name, value] of previous) value ? root.style.setProperty(name, value) : root.style.removeProperty(name);
      root.dataset.telegramTheme = app.colorScheme === 'dark' ? 'dark' : 'light';
      const theme = app.themeParams || {};
      const colors = { bg_color: '--color-canvas', secondary_bg_color: '--color-surface', text_color: '--color-ink', hint_color: '--color-muted', button_color: '--color-brand' };
      Object.entries(colors).forEach(([key, css]) => { if (/^#[a-f\d]{6}$/i.test(theme[key] || '')) set(css, theme[key]); });
      if (/^#[a-f\d]{6}$/i.test(theme.text_color || '')) set('--color-text', theme.text_color);
      if (/^#[a-f\d]{6}$/i.test(theme.hint_color || '')) set('--color-subtle', theme.hint_color);
      if (/^#[a-f\d]{6}$/i.test(theme.button_text_color || '')) set('--miniapp-button-text', theme.button_text_color);
      const height = app.viewportStableHeight;
      if (typeof height === 'number' && Number.isFinite(height) && height > 0) set('--miniapp-height', `${height}px`);
      for (const edge of ['top', 'right', 'bottom', 'left'] as const) {
        const safe = Math.max(0, app.safeAreaInset?.[edge] || 0) + Math.max(0, app.contentSafeAreaInset?.[edge] || 0);
        set(`--miniapp-safe-${edge}`, `${Math.min(safe, 200)}px`);
      }
    };
    const events = ['themeChanged', 'viewportChanged', 'safeAreaChanged', 'contentSafeAreaChanged'];
    update(); events.forEach(event => app.onEvent?.(event, update));
    const back = () => callback.current();
    app.BackButton?.onClick?.(back);
    return () => {
      events.forEach(event => app.offEvent?.(event, update));
      app.BackButton?.offClick?.(back); app.BackButton?.hide?.();
      for (const [name, value] of previous) value ? root.style.setProperty(name, value) : root.style.removeProperty(name);
      if (originalTheme === undefined) delete root.dataset.telegramTheme; else root.dataset.telegramTheme = originalTheme;
    };
  }, []);
  useEffect(() => {
    const app = telegramApp();
    if (!app || !telegramInitData()) return;
    if (usable) {
      app.BackButton?.show?.();
    } else app.BackButton?.hide?.();
    // ready() dismisses Telegram's loading placeholder. A safe terminal error
    // is displayable even though authentication/private access did not succeed.
    if ((usable || terminalError) && readyApp.current !== app) { app.ready?.(); readyApp.current = app; }
  }, [usable, terminalError]);
}
