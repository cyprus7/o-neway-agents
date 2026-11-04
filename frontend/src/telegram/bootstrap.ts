const SDK_URL = 'https://telegram.org/js/telegram-web-app.js?64';
const SDK_TIMEOUT_MS = 8_000;

function hasLaunchData() {
  const value = window.Telegram?.WebApp?.initData;
  return typeof value === 'string' && value.trim().length > 0;
}

/** Hash presence selects SDK bootstrapping only. The server authenticates initData. */
export async function bootstrapTelegram(): Promise<void> {
  if (hasLaunchData()) return;
  if (!new URLSearchParams(window.location.hash.slice(1)).has('tgWebAppData')) return;

  await new Promise<void>((resolve, reject) => {
    const script = document.createElement('script');
    script.src = SDK_URL;
    script.async = true;
    script.referrerPolicy = 'no-referrer';
    const finish = (error?: Error) => {
      window.clearTimeout(timer);
      script.onload = script.onerror = null;
      if (error) { script.remove(); reject(error); } else resolve();
    };
    const failure = () => new Error('Не удалось загрузить Telegram. Откройте Mini App заново или повторите загрузку.');
    const timer = window.setTimeout(() => finish(failure()), SDK_TIMEOUT_MS);
    script.onload = () => finish(hasLaunchData() ? undefined : failure());
    script.onerror = () => finish(failure());
    document.head.append(script);
  });
}
