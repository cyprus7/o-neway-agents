"""Small safe fetch boundary for reviewed official Event pages only."""
from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
from dataclasses import dataclass
from typing import Awaitable, Callable
from urllib.parse import urljoin, urlsplit

import httpx

from .domain import MAX_HTML_BYTES, source_url, utcnow


Resolver = Callable[[str], Awaitable[tuple[str, ...] | list[str]]]


def public_address(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
        return address.is_global and not any((address.is_private, address.is_loopback, address.is_link_local,
                                              address.is_multicast, address.is_reserved, address.is_unspecified))
    except ValueError:
        return False


async def resolve_public_host(host: str) -> tuple[str, ...]:
    answers = await asyncio.get_running_loop().getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    addresses = tuple(dict.fromkeys(answer[4][0] for answer in answers))
    # Reject mixed public/private DNS as well as private-only answers.
    if not addresses or not all(public_address(address) for address in addresses):
        raise ValueError("event_source_dns_not_public")
    return tuple(sorted(addresses, key=lambda address: ":" in address))


@dataclass(frozen=True)
class FetchedDocument:
    url: str
    html: str
    retrieved_at: str


class OfficialEventFetcher:
    def __init__(self, client: httpx.AsyncClient, network_slots: asyncio.Semaphore | None = None,
                 *, resolver: Resolver | None = None, timeout_seconds: float = 7):
        self.client = client
        self.slots = network_slots or asyncio.Semaphore(3)
        self.resolver = resolver or resolve_public_host
        self.timeout_seconds = min(8.0, max(0.01, timeout_seconds))

    async def fetch(self, url: str) -> FetchedDocument | None:
        current = source_url(url)
        if current is None:
            return None
        try:
            # Queue acquisition, DNS, headers and streaming share the same deadline.
            async with asyncio.timeout(self.timeout_seconds):
                async with self.slots:
                    for redirect in range(2):
                        host = urlsplit(current).hostname
                        addresses = await self.resolver(host)
                        if not addresses or not all(public_address(address) for address in addresses):
                            return None
                        # Pin the verified address, preserving Host and TLS hostname. A
                        # second DNS lookup by the HTTP transport cannot rebind to a LAN IP.
                        pinned = httpx.URL(current).copy_with(host=addresses[0])
                        async with self.client.stream("GET", pinned, follow_redirects=False,
                                headers={"Host": host, "Accept": "text/html,application/xhtml+xml", "Accept-Encoding": "identity", "Connection": "close"},
                                extensions={"sni_hostname": host}, timeout=httpx.Timeout(self.timeout_seconds, connect=min(4, self.timeout_seconds))) as response:
                            if response.status_code in (301, 302, 303, 307, 308):
                                if redirect:
                                    return None
                                current = source_url(urljoin(current, response.headers.get("location", "")))
                                if current is None:
                                    return None
                                continue
                            if response.status_code != 200:
                                return None
                            # aiter_bytes() decodes Content-Encoding before yielding.
                            # Refuse compressed responses before iteration so a small
                            # compressed bomb cannot allocate beyond the document cap.
                            encoding = response.headers.get("content-encoding", "").strip().casefold()
                            if encoding and encoding != "identity":
                                return None
                            content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().casefold()
                            if content_type not in ("text/html", "application/xhtml+xml"):
                                return None
                            length = response.headers.get("content-length")
                            if length is not None and (not length.isdigit() or int(length) > MAX_HTML_BYTES):
                                return None
                            body = bytearray()
                            async for chunk in response.aiter_bytes(chunk_size=65536):
                                if len(body) + len(chunk) > MAX_HTML_BYTES:
                                    return None
                                body.extend(chunk)
                            document = body.decode(response.encoding or "utf-8", errors="replace")
                            if re.search(r"cf-chl-|challenge-platform|verify you are human|checking your browser|<title>\s*access denied", document[:100_000], re.IGNORECASE):
                                return None
                            return FetchedDocument(current, document, utcnow())
        except (TimeoutError, OSError, httpx.HTTPError, ValueError, TypeError, UnicodeError, LookupError):
            return None
        return None
