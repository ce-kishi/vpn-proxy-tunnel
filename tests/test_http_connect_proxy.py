"""Loopback CONNECT proxy tests; no external network access."""

import os
import select
import shlex
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
import http_connect_proxy as proxy


@contextmanager
def client_socket(address):
    with socket.socket() as client:
        client.settimeout(3)
        client.connect(address)
        yield client


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.server = proxy.ThreadedProxy(("127.0.0.1", 0))
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        )
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)

    def test_disallowed_connect_and_get_never_contact_upstream(self):
        cases = [
            (f"CONNECT example.com:{port} HTTP/1.1\r\n\r\n".encode(), b"403 Forbidden")
            for port in (22, 80, 444, 8443, 65535)
        ] + [
            (b"GET http://example.com/ HTTP/1.1\r\n\r\n", b"405 Method Not Allowed"),
        ]
        with patch.object(proxy.socket, "create_connection") as connect:
            for request, expected in cases:
                with self.subTest(request=request), client_socket(self.server.server_address) as client:
                    client.sendall(request)
                    self.assertIn(expected, client.recv(4096))
            connect.assert_not_called()

    def test_arbitrary_hosts_on_port_443_are_contacted(self):
        for host in ("example.com", "updates.vendor.example", "127.0.0.1"):
            with (
                self.subTest(host=host),
                patch.object(proxy.socket, "create_connection", side_effect=OSError) as connect,
                client_socket(self.server.server_address) as client,
            ):
                client.sendall(f"CONNECT {host}:443 HTTP/1.1\r\n\r\n".encode())
                self.assertIn(b"502 Bad Gateway", client.recv(4096))
                connect.assert_called_once_with((host, 443), timeout=15)

    def test_allowed_connect_relays_bytes_in_both_directions(self):
        upstream, peer = socket.socketpair()
        self.addCleanup(upstream.close)
        peer.settimeout(3)
        # The handler owns upstream; closing it here while select() is running
        # would race the server thread and emit an asynchronous bad-FD error.
        with peer, patch.object(
            proxy.socket, "create_connection", return_value=upstream
        ) as connect:
            with client_socket(self.server.server_address) as client:
                client.sendall(b"CONNECT example.com:443 HTTP/1.1\r\n\r\n")
                self.assertIn(b"200 Connection Established", client.recv(4096))
                client.sendall(b"request bytes")
                self.assertEqual(peer.recv(4096), b"request bytes")
                peer.sendall(b"response bytes")
                self.assertEqual(client.recv(4096), b"response bytes")
                peer.shutdown(socket.SHUT_WR)
            connect.assert_called_once_with(("example.com", 443), timeout=15)
            deadline = time.monotonic() + 3
            while upstream.fileno() != -1 and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual(upstream.fileno(), -1, "handler did not close its socket")

    def test_upstream_response_is_relayed_while_upload_is_backpressured(self):
        upstream, peer = socket.socketpair()
        upstream.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
        self.addCleanup(upstream.close)
        peer.settimeout(3)
        with (
            peer,
            patch.object(proxy.socket, "create_connection", return_value=upstream),
            client_socket(self.server.server_address) as client,
        ):
            client.sendall(b"CONNECT archive.ubuntu.com:443 HTTP/1.1\r\n\r\n")
            self.assertIn(b"200 Connection Established", client.recv(4096))
            # Fill the proxy's upstream send buffer without blocking this test.
            client.sendall(b"x" * 65536)
            self.assertEqual(peer.recv(1), b"x")
            peer.sendall(b"early rejection")
            self.assertEqual(client.recv(4096), b"early rejection")

    def test_idle_select_wakeup_does_not_close_tunnel(self):
        upstream, peer = socket.socketpair()
        self.addCleanup(upstream.close)
        idle = threading.Event()
        real_select = select.select

        def wake_once(*args, **kwargs):
            if not idle.is_set():
                idle.set()
                return [], [], []
            return real_select(*args, **kwargs)

        with (
            peer,
            patch.object(proxy.socket, "create_connection", return_value=upstream),
            patch.object(proxy.select, "select", side_effect=wake_once),
            client_socket(self.server.server_address) as client,
        ):
            client.sendall(b"CONNECT api.openai.com:443 HTTP/1.1\r\n\r\n")
            self.assertIn(b"200 Connection Established", client.recv(4096))
            self.assertTrue(idle.wait(3))
            peer.sendall(b"delayed response")
            self.assertEqual(client.recv(4096), b"delayed response")

    def test_half_closed_upload_is_fully_flushed_before_response(self):
        upstream, peer = socket.socketpair()
        upstream.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
        self.addCleanup(upstream.close)
        peer.settimeout(3)
        payload = b"upload data" * 30000
        failures = []

        def upload(client):
            try:
                client.sendall(payload)
                client.shutdown(socket.SHUT_WR)
            except OSError as exc:
                failures.append(exc)

        with (
            peer,
            patch.object(proxy.socket, "create_connection", return_value=upstream),
            client_socket(self.server.server_address) as client,
        ):
            client.sendall(b"CONNECT archive.ubuntu.com:443 HTTP/1.1\r\n\r\n")
            self.assertIn(b"200 Connection Established", client.recv(4096))
            sender = threading.Thread(target=upload, args=(client,), daemon=True)
            sender.start()
            received = bytearray()
            while True:
                chunk = peer.recv(65536)
                if not chunk:
                    break
                received.extend(chunk)
            sender.join(timeout=3)
            self.assertFalse(sender.is_alive())
            self.assertFalse(failures)
            self.assertEqual(received, payload)
            peer.sendall(b"upload accepted")
            peer.shutdown(socket.SHUT_WR)
            self.assertEqual(client.recv(4096), b"upload accepted")
            self.assertEqual(client.recv(1), b"")


class CommandLineTests(unittest.TestCase):
    @contextmanager
    def running_proxy(self, directory, *options):
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        process = subprocess.Popen(
            [sys.executable, str(REPO / "http_connect_proxy.py"), str(port), *options],
            cwd=directory, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        try:
            deadline = time.monotonic() + 5
            while True:
                if process.poll() is not None:
                    self.fail(f"proxy exited early: {process.communicate()}")
                try:
                    with client_socket(("127.0.0.1", port)):
                        break
                except ConnectionRefusedError:
                    if time.monotonic() >= deadline:
                        self.fail("proxy did not start")
                    time.sleep(0.02)
            yield ("127.0.0.1", port)
        finally:
            process.terminate()
            try:
                process.communicate(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()

    def test_starts_from_an_unrelated_working_directory(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            self.running_proxy(directory) as address,
            client_socket(address) as client,
        ):
            client.sendall(b"CONNECT example.com:80 HTTP/1.1\r\n\r\n")
            self.assertIn(b"403 Forbidden", client.recv(4096))

    def test_occupied_port_exits_unsuccessfully(self):
        with socket.socket() as occupied:
            occupied.bind(("127.0.0.1", 0))
            occupied.listen()
            port = occupied.getsockname()[1]
            result = subprocess.run(
                [sys.executable, str(REPO / "http_connect_proxy.py"), str(port)],
                capture_output=True, text=True, timeout=5, check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Address already in use", result.stderr)


class TunnelStartupTests(unittest.TestCase):
    def run_tunnel(self, directory, port, check_proxy=False):
        root = Path(directory)
        launcher = root / "python-launcher"
        launcher.write_text(
            '#!/bin/sh\nif [ "$1" != "-c" ]; then sleep 0.6; fi\n'
            f'exec {shlex.quote(sys.executable)} "$@"\n'
        )
        launcher.chmod(0o700)
        marker = root / "ssh-called"
        ssh = root / "ssh"
        ssh.write_text(
            f"#!{sys.executable}\n"
            "import socket\nfrom pathlib import Path\n"
            f"Path({str(marker)!r}).write_text('called')\n"
            + (
                f"with socket.create_connection(('127.0.0.1', {port}), timeout=3) as client:\n"
                "    client.sendall(b'CONNECT example.com:80 HTTP/1.1\\r\\n\\r\\n')\n"
                "    assert b'403 Forbidden' in client.recv(4096)\n"
                if check_proxy else ""
            )
        )
        ssh.chmod(0o700)
        env = dict(
            os.environ,
            PATH=str(root) + os.pathsep + os.environ["PATH"],
            TUNNEL_PYTHON_BIN=str(launcher),
            TUNNEL_REMOTE_HOST="unused-test-host",
            TUNNEL_PROXY_PORT=str(port),
        )
        result = subprocess.run(
            ["bash", str(REPO / "proxy-tunnel.sh")], env=env,
            capture_output=True, text=True, timeout=10, check=False,
        )
        return result, marker.exists()

    def test_slow_port_collision_never_starts_ssh(self):
        with tempfile.TemporaryDirectory() as directory, socket.socket() as occupied:
            occupied.bind(("127.0.0.1", 0))
            occupied.listen()
            result, ssh_called = self.run_tunnel(directory, occupied.getsockname()[1])
            self.assertFalse(ssh_called, result.stdout + result.stderr)
            self.assertNotEqual(result.returncode, 0)

    def test_slow_successful_start_is_ready_before_ssh(self):
        with tempfile.TemporaryDirectory() as directory:
            with socket.socket() as reservation:
                reservation.bind(("127.0.0.1", 0))
                port = reservation.getsockname()[1]
            result, ssh_called = self.run_tunnel(directory, port, check_proxy=True)
            self.assertTrue(ssh_called, result.stdout + result.stderr)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
