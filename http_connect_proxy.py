#!/usr/bin/env python3
"""Loopback-only CONNECT proxy for any destination on port 443."""

from __future__ import annotations

import argparse
import select
import socket
import socketserver
from pathlib import Path

MAX_HEADER = 64 * 1024


class ConnectHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        client = self.request
        client.settimeout(15)
        header = bytearray()
        while b"\r\n\r\n" not in header:
            chunk = client.recv(4096)
            if not chunk:
                return
            header.extend(chunk)
            if len(header) > MAX_HEADER:
                client.sendall(b"HTTP/1.1 431 Request Header Fields Too Large\r\n\r\n")
                return

        first_line = bytes(header).split(b"\r\n", 1)[0]
        parts = first_line.split()
        if len(parts) != 3 or parts[0].upper() != b"CONNECT":
            client.sendall(b"HTTP/1.1 405 Method Not Allowed\r\n\r\n")
            return

        authority = parts[1].decode("ascii", "strict")
        host, separator, port_text = authority.rpartition(":")
        if not separator or not host:
            client.sendall(b"HTTP/1.1 400 Bad Request\r\n\r\n")
            return

        try:
            port = int(port_text)
            if not 1 <= port <= 65535:
                raise ValueError
            if port != 443:
                client.sendall(b"HTTP/1.1 403 Forbidden\r\n\r\n")
                return
            upstream = socket.create_connection((host, port), timeout=15)
        except (OSError, ValueError):
            client.sendall(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
            return

        with upstream:
            client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            sockets = (client, upstream)
            other = {client: upstream, upstream: client}
            for connection in sockets:
                connection.setblocking(False)
            readers = set(sockets)
            pending = {connection: bytearray() for connection in sockets}
            try:
                while readers or any(pending.values()):
                    # Bound buffering independently in each direction. A blocked
                    # upload must not prevent an upstream response from flowing.
                    readable, writable, exceptional = select.select(
                        [source for source in readers if len(pending[other[source]]) < 65536],
                        [destination for destination in sockets if pending[destination]],
                        sockets,
                    )
                    if exceptional:
                        return
                    for destination in writable:
                        try:
                            sent = destination.send(pending[destination])
                        except BlockingIOError:
                            continue
                        if not sent:
                            return
                        del pending[destination][:sent]
                        source = other[destination]
                        if not pending[destination] and source not in readers:
                            destination.shutdown(socket.SHUT_WR)
                    for source in readable:
                        destination = other[source]
                        try:
                            data = source.recv(65536 - len(pending[destination]))
                        except BlockingIOError:
                            continue
                        if data:
                            pending[destination].extend(data)
                        else:
                            readers.remove(source)
                            if not pending[destination]:
                                destination.shutdown(socket.SHUT_WR)
            except OSError:
                # Connection resets and broken pipes terminate only this tunnel.
                return


class ThreadedProxy(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, address: tuple[str, int]) -> None:
        super().__init__(address, ConnectHandler)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("port", nargs="?", type=int, default=18081)
    parser.add_argument("--ready-file", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    with ThreadedProxy(("127.0.0.1", args.port)) as server:
        if args.ready_file is not None:
            args.ready_file.write_text("ready\n", encoding="ascii")
        server.serve_forever()


if __name__ == "__main__":
    main()
