"""Internal HTTP proxy. Every outbound connection is pinned to a vetted public IP.

Only this container joins the external network. API and workers cannot bypass it.
"""
import select
import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from app.security import public_addresses, validate_url


def connect_public(host, port):
    if port not in (80, 443):
        raise ValueError('Port forbidden')
    candidates = public_addresses(host, port)
    last_error = None
    for family, kind, proto, _, address in candidates:
        conn = socket.socket(family, kind, proto)
        conn.settimeout(20)
        try:
            conn.connect(address)
            return conn
        except OSError as exc:
            conn.close()
            last_error = exc
    raise OSError('Upstream unavailable') from last_error


class Proxy(BaseHTTPRequestHandler):
    rbufsize = 0
    protocol_version = 'HTTP/1.1'

    def log_message(self, *args):
        pass  # Source URLs can contain signed credentials.

    def tunnel(self, remote):
        remote.setblocking(False)
        self.connection.setblocking(False)
        # sendall with a timeout is simpler and bounded for slow peers.
        remote.settimeout(30)
        self.connection.settimeout(30)
        try:
            while True:
                readable, _, _ = select.select([remote, self.connection], [], [], 60)
                if not readable:
                    return
                for source in readable:
                    block = source.recv(65536)
                    if not block:
                        return
                    (remote if source is self.connection else self.connection).sendall(block)
        except OSError:
            return
        finally:
            remote.close()
            self.close_connection = True

    def do_CONNECT(self):
        try:
            parsed = urlsplit('//' + self.path)
            if parsed.username or parsed.password or parsed.path:
                raise ValueError('Invalid target')
            remote = connect_public(parsed.hostname, parsed.port or 443)
        except (ValueError, OSError):
            self.send_error(403, 'Destination unavailable or forbidden')
            return
        self.send_response(200, 'Connection established')
        self.end_headers()
        self.tunnel(remote)

    def do_GET(self):
        try:
            validate_url(self.path)
            parsed = urlsplit(self.path)
            if parsed.scheme != 'http':
                raise ValueError('Use CONNECT for HTTPS')
            remote = connect_public(parsed.hostname, parsed.port or 80)
            path = (parsed.path or '/') + ('?' + parsed.query if parsed.query else '')
            excluded = {'proxy-connection', 'connection', 'host', 'proxy-authorization',
                        'transfer-encoding', 'content-length'}
            headers = ''.join(f'{k}: {v}\r\n' for k, v in self.headers.items() if k.lower() not in excluded)
            remote.sendall((f'{self.command} {path} HTTP/1.1\r\nHost: {parsed.netloc}\r\n'
                            f'{headers}Connection: close\r\n\r\n').encode('latin-1'))
        except (ValueError, OSError):
            self.send_error(403, 'Destination unavailable or forbidden')
            return
        # For GET/HEAD no request body is accepted. Relay upstream bytes until EOF.
        try:
            while block := remote.recv(65536):
                self.connection.sendall(block)
        except OSError:
            pass
        finally:
            remote.close()
            self.close_connection = True

    do_HEAD = do_GET


if __name__ == '__main__':
    ThreadingHTTPServer(('0.0.0.0', 8081), Proxy).serve_forever()
