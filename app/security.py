import hashlib
import ipaddress
import socket
from urllib.parse import urlsplit


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def validate_url(url: str) -> str:
    parts = urlsplit(url)
    if parts.scheme not in ('http', 'https') or not parts.hostname or parts.username or parts.password:
        raise ValueError('Нужна общедоступная ссылка http:// или https:// без логина и пароля.')
    if parts.port not in (None, 80, 443):
        raise ValueError('Поддерживаются только стандартные HTTP/HTTPS-порты.')
    if len(url) > 4096 or any(ord(c) < 32 or ord(c) == 127 for c in url):
        raise ValueError('Некорректная ссылка.')
    return url


def public_addresses(host: str, port: int):
    """Resolve once; the egress proxy connects to this exact vetted IP, preventing rebinding."""
    addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    if not addresses:
        raise ValueError('Адрес не найден.')
    for family, kind, proto, _, address in addresses:
        ip = ipaddress.ip_address(address[0])
        if not ip.is_global or ip.is_multicast or (ip.version == 6 and ip.ipv4_mapped and not ip.ipv4_mapped.is_global):
            raise ValueError('Доступ к локальным и служебным сетям запрещён.')
    return addresses
