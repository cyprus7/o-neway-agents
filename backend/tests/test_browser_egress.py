"""Worker egress guards can be checked without installing a host browser."""
import asyncio
import socket

import pytest

from price_worker import guards


@pytest.mark.parametrize('url',[ 'http://www.google.com/travel', 'https://www.google.com:444/',
    'https://user:pass@www.google.com/', 'https://www.google.com.attacker.example/',
    'https://127.0.0.1/', 'https://169.254.169.254/latest/meta-data/',
    'file:///etc/passwd', 'javascript:alert(1)', 'https://price-worker:8080/',
    'https://[::1]/', 'https://www.google.com:bad/'])
def test_untrusted_browser_destinations_reject(url):
    assert not guards.allowed_url(url)


def test_allowlisted_hostname_with_private_dns_is_rejected(monkeypatch):
    monkeypatch.setattr(socket,'getaddrinfo',lambda *a,**k:[(socket.AF_INET,socket.SOCK_STREAM,6,'',('127.0.0.1',443))])
    assert not asyncio.run(guards.public_destination('https://www.google.com/travel'))
    with pytest.raises(ValueError):
        asyncio.run(guards.pinned_resolver_rules())


def test_resolver_pins_exact_public_hosts_and_denies_others(monkeypatch):
    monkeypatch.setattr(socket,'getaddrinfo',lambda *a,**k:[(socket.AF_INET,socket.SOCK_STREAM,6,'',('8.8.8.8',443))])
    rules=asyncio.run(guards.pinned_resolver_rules())
    assert rules.endswith('MAP * ~NOTFOUND')
    assert all('MAP '+host+' 8.8.8.8' in rules for host in guards.DOMAINS)
