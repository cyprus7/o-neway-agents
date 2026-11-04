"""Finite public Google egress and a read-only navigation boundary."""
import asyncio
import ipaddress
import socket
from urllib.parse import urlsplit


DOMAINS = frozenset({"www.google.com", "consent.google.com", "www.gstatic.com", "fonts.gstatic.com",
                     "fonts.googleapis.com", "lh3.googleusercontent.com", "maps.googleapis.com", "maps.gstatic.com"})
_FORBIDDEN = ("/accounts", "/signin", "/ServiceLogin", "/url", "/aclk", "/travel/clk",
              "/travel/flights/booking", "/travel/flights/checkout")


def allowed_url(url, *, navigation=False, method="GET"):
    try:
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or parsed.hostname not in DOMAINS or parsed.port not in (None, 443)
                or parsed.username or parsed.password or parsed.path.startswith(_FORBIDDEN)):
            return False
        # Observed ordinary Reject all submits this exact consent form. It is
        # the only allowed mutation-shaped navigation and grants no account or
        # seller access; redirects must pass the navigation boundary again.
        if parsed.hostname == "consent.google.com" and parsed.path == "/save":
            return method == "POST"
        return not navigation or parsed.hostname == "www.google.com" and parsed.path in (
            "/travel/flights", "/travel/flights/search") or parsed.hostname == "consent.google.com" and parsed.path == "/m"
    except (ValueError, TypeError):
        return False


async def public_destination(url, *, method="GET"):
    if not allowed_url(url, method=method):
        return False
    try:
        async with asyncio.timeout(2):
            addresses = await asyncio.to_thread(socket.getaddrinfo, urlsplit(url).hostname, 443, type=socket.SOCK_STREAM)
        return bool(addresses) and all(ipaddress.ip_address(item[4][0]).is_global for item in addresses)
    except (OSError, TimeoutError, ValueError):
        return False


async def pinned_resolver_rules():
    rules = []
    for host in sorted(DOMAINS):
        async with asyncio.timeout(2):
            addresses = await asyncio.to_thread(socket.getaddrinfo, host, 443, socket.AF_INET, socket.SOCK_STREAM)
        if not addresses or not all(ipaddress.ip_address(item[4][0]).is_global for item in addresses):
            raise ValueError("Nonpublic flight destination")
        rules.append(f"MAP {host} {addresses[0][4][0]}")
    return ", ".join([*rules, "MAP * ~NOTFOUND"])


async def install_guards(context):
    async def websocket(route):
        await route.close()
    await context.route_web_socket("**/*", websocket)
    await context.add_init_script("""for(const name of ['RTCPeerConnection','webkitRTCPeerConnection']){
        Object.defineProperty(globalThis,name,{value:undefined,writable:false,configurable:false});} """)
    async def page_opened(page):
        if len(context.pages) > 1:
            await page.close()
        else:
            async def cancel_download(download):
                await download.cancel()
            page.on("download", cancel_download)
    context.on("page", page_opened)
    async def request(route):
        value = route.request
        if (value.resource_type in ("image", "media", "font")
                or not allowed_url(value.url, navigation=value.is_navigation_request(), method=value.method)
                or not await public_destination(value.url, method=value.method)):
            await route.abort()
        else:
            await route.continue_()
    await context.route("**/*", request)
