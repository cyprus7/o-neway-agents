"""Bound every browser request and prevent access to private/network metadata targets."""
import asyncio
import ipaddress
import socket
from urllib.parse import urlsplit

GOOGLE_DOMAINS = frozenset({'www.google.com', 'consent.google.com', 'www.gstatic.com',
                           'fonts.gstatic.com', 'fonts.googleapis.com', 'lh3.googleusercontent.com',
                           'maps.googleapis.com', 'maps.gstatic.com'})
# Only hosts observed serving the official public application are permitted.
# Advertising, analytics, chat widgets and wildcard subdomains stay blocked.
OSTROVOK_DOMAINS = frozenset({'ostrovok.ru', 'hf.fastrovok.net', 'mpf.fastrovok.net'})
PROVIDER_DOMAINS = {'google_hotels': GOOGLE_DOMAINS, 'ostrovok': OSTROVOK_DOMAINS}
DOMAINS = GOOGLE_DOMAINS | OSTROVOK_DOMAINS


async def pinned_resolver_rules() -> str:
    """Pin the allowlist to public IPv4 addresses for the browser's lifetime (no DNS rebind)."""
    rules = []
    for host in sorted(DOMAINS):
        async with asyncio.timeout(2):
            entries = await asyncio.to_thread(socket.getaddrinfo, host, 443, socket.AF_INET, socket.SOCK_STREAM)
        addresses = [entry[4][0] for entry in entries]
        if not addresses or not all(ipaddress.ip_address(value).is_global for value in addresses):
            raise ValueError('nonpublic_browser_destination')
        rules.append(f'MAP {host} {addresses[0]}')
    return ', '.join([*rules, 'MAP * ~NOTFOUND'])


def allowed_url(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        return bool(parsed.scheme == 'https' and parsed.hostname in DOMAINS
                    and parsed.port in (None, 443) and not parsed.username and not parsed.password)
    except ValueError:
        return False


def provider_url_allowed(url: str, provider_id: str) -> bool:
    """A recipe cannot borrow another provider's hosts or enter a reserve flow."""
    if not allowed_url(url):
        return False
    parsed = urlsplit(url)
    return bool(parsed.hostname in PROVIDER_DOMAINS[provider_id]
                and not (provider_id == 'ostrovok' and parsed.path.startswith('/orders/')))


async def public_destination(url: str) -> bool:
    if not allowed_url(url):
        return False
    host = urlsplit(url).hostname
    try:
        async with asyncio.timeout(2):
            results = await asyncio.to_thread(socket.getaddrinfo, host, 443, type=socket.SOCK_STREAM)
        return bool(results) and all(ipaddress.ip_address(result[4][0]).is_global for result in results)
    except (TimeoutError, OSError, ValueError):
        return False


async def install_guards(context, provider_id='google_hotels'):
    """Guard redirects and subresources as well as the initial navigation; no arbitrary fetch."""
    if provider_id not in PROVIDER_DOMAINS:
        raise ValueError('Unknown browser recipe')
    async def close_websocket(route):
        await route.close()
    await context.route_web_socket('**/*', close_websocket)
    # Recipes do not need peer-to-peer networking. Disable it before any page script.
    await context.add_init_script("""for (const name of ['RTCPeerConnection','webkitRTCPeerConnection']) {
      Object.defineProperty(globalThis, name, {value: undefined, writable: false, configurable: false});
    }""")
    # One page per isolated context; two contexts are the worker-wide maximum.
    async def close_popup(page):
        if len(context.pages) > 1:
            await page.close()
    context.on('page', close_popup)
    async def route_request(route):
        request = route.request
        if request.resource_type in ('image', 'media', 'font'):
            await route.abort()
            return
        if not provider_url_allowed(request.url, provider_id) or not await public_destination(request.url):
            await route.abort()
            return
        await route.continue_()
    await context.route('**/*', route_request)
