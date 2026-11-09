"""Allowlist policy and loopback protocol tests; no external network access."""

import json
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


def empty_config():
    return {
        "exact_hosts": [],
        "domains_and_subdomains": [],
        "subdomains_only": [],
    }


@contextmanager
def client_socket(address):
    with socket.socket() as client:
        client.settimeout(3)
        client.connect(address)
        yield client


class AllowlistTests(unittest.TestCase):
    def setUp(self):
        self.allowlist = proxy.load_allowlist(proxy.DEFAULT_CONFIG)

    def test_existing_and_apt_hosts_are_allowed(self):
        hosts = [
            "openai.com", "api.openai.com", "deep.api.openai.com",
            "chatgpt.com", "chat.chatgpt.com", "registry.ollama.ai",
            "registry-1.docker.io", "auth.docker.io",
            "production.cloudflare.docker.com", "production.cloudfront.docker.com",
            "bucket.r2.cloudflarestorage.com", "archive.ubuntu.com",
            "security.ubuntu.com", "download.docker.com", "nvidia.github.io",
            "ports.ubuntu.com", "esm.ubuntu.com", "pkgs.tailscale.com",
            "developer.download.nvidia.com", "repo.download.nvidia.com",
            "workbench.download.nvidia.com", "snapshot.ppa.launchpadcontent.net",
            "ppa.launchpadcontent.net", "storage.googleapis.com",
            "releases.bazel.build",
            "packages.cloud.google.com",
            "deb.nodesource.com",
            "API.OPENAI.COM.",
        ]
        for host in hosts:
            with self.subTest(host=host):
                self.assertTrue(proxy.destination_allowed(host, 443, self.allowlist))

    def test_other_hosts_lookalikes_and_non_https_ports_are_denied(self):
        hosts = [
            "example.com", "evilopenai.com", "openai.com.attacker.example",
            "evilchatgpt.com", "r2.cloudflarestorage.com",
            "evilr2.cloudflarestorage.com", "registry-1.docker.io.attacker.example",
            "sub.archive.ubuntu.com", "sub.download.docker.com", "evilnvidia.github.io",
            "nvidia.github.io.attacker.example", "docker.com", "github.io",
            "sub.ports.ubuntu.com", "esm.ubuntu.com.attacker.example",
            "evilpkgs.tailscale.com", "nvidia.com", "download.nvidia.com",
            "sub.developer.download.nvidia.com", "repo.download.nvidia.com.evil.example",
            "evilworkbench.download.nvidia.com", "ppa.launchpad.net",
            "sub.ppa.launchpadcontent.net", "sub.storage.googleapis.com",
            "sub.packages.cloud.google.com", "storage.googleapis.com.evil.example",
            "packages.cloud.google.com.evil.example",
            "sub.releases.bazel.build", "releases.bazel.build.evil.example",
            "sub.snapshot.ppa.launchpadcontent.net", "nodesource.com",
        ]
        for host in hosts:
            with self.subTest(host=host):
                self.assertFalse(proxy.destination_allowed(host, 443, self.allowlist))
        for port in (0, 22, 80, 444, 8443, 65536):
            with self.subTest(port=port):
                self.assertFalse(proxy.destination_allowed("openai.com", port, self.allowlist))

    def test_explicit_policy_does_not_inherit_default_hosts(self):
        # Explicitly supplied policy cannot silently inherit the default hosts.
        deny_all = proxy.Allowlist(frozenset(), (), ())
        self.assertFalse(proxy.destination_allowed("openai.com", 443, deny_all))

    def test_custom_config_and_normalization(self):
        config = empty_config()
        config["exact_hosts"] = ["EXAMPLE.COM."]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "allow.json"
            path.write_text(json.dumps(config))
            allowlist = proxy.load_allowlist(path)
        self.assertTrue(proxy.destination_allowed("example.com", 443, allowlist))
        self.assertFalse(proxy.destination_allowed("sub.example.com", 443, allowlist))
        self.assertFalse(proxy.destination_allowed("openai.com", 443, allowlist))

    def test_invalid_configurations_fail_closed(self):
        invalid = [[], {}, {**empty_config(), "unexpected": []}]
        for key in empty_config():
            for value in (None, "example.com", {}, [123], [None]):
                invalid.append({**empty_config(), key: value})
            for host in (
                "", "https://example.com", "example.com:443", "*.example.com",
                " example.com", "example.com/path", "a..com", "-bad.com",
                "bad-.com", "a_b.com", "éxample.com", "a" * 64 + ".com",
            ):
                invalid.append({**empty_config(), key: [host]})
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "allow.json"
            for config in invalid:
                with self.subTest(config=config):
                    path.write_text(json.dumps(config))
                    with self.assertRaises((ValueError, TypeError)):
                        proxy.load_allowlist(path)
            path.write_text("{")
            with self.assertRaises(ValueError):
                proxy.load_allowlist(path)
            with self.assertRaises(OSError):
                proxy.load_allowlist(Path(directory) / "missing.json")


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.server = proxy.ThreadedProxy(
            ("127.0.0.1", 0), proxy.load_allowlist(proxy.DEFAULT_CONFIG)
        )
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
            (b"CONNECT example.com:443 HTTP/1.1\r\n\r\n", b"403 Forbidden"),
            (b"CONNECT openai.com:80 HTTP/1.1\r\n\r\n", b"403 Forbidden"),
            (b"GET http://example.com/ HTTP/1.1\r\n\r\n", b"405 Method Not Allowed"),
        ]
        with patch.object(proxy.socket, "create_connection") as connect:
            for request, expected in cases:
                with self.subTest(request=request), client_socket(self.server.server_address) as client:
                    client.sendall(request)
                    self.assertIn(expected, client.recv(4096))
            connect.assert_not_called()

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
                client.sendall(b"CONNECT archive.ubuntu.com:443 HTTP/1.1\r\n\r\n")
                self.assertIn(b"200 Connection Established", client.recv(4096))
                client.sendall(b"request bytes")
                self.assertEqual(peer.recv(4096), b"request bytes")
                peer.sendall(b"response bytes")
                self.assertEqual(client.recv(4096), b"response bytes")
                peer.shutdown(socket.SHUT_WR)
            connect.assert_called_once_with(("archive.ubuntu.com", 443), timeout=15)
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

    def test_default_config_loads_from_an_unrelated_working_directory(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            self.running_proxy(directory) as address,
            client_socket(address) as client,
        ):
            client.sendall(b"CONNECT denied.example:443 HTTP/1.1\r\n\r\n")
            self.assertIn(b"403 Forbidden", client.recv(4096))

    def test_config_option_overrides_default(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "deny-all.json"
            path.write_text(json.dumps(empty_config()))
            with (
                self.running_proxy(directory, "--config", str(path)) as address,
                client_socket(address) as client,
            ):
                client.sendall(b"CONNECT openai.com:443 HTTP/1.1\r\n\r\n")
                self.assertIn(b"403 Forbidden", client.recv(4096))

    def test_missing_and_invalid_configuration_exit_unsuccessfully(self):
        with tempfile.TemporaryDirectory() as directory:
            invalid_path = Path(directory) / "invalid.json"
            invalid_path.write_text("{")
            for path in (invalid_path, Path(directory) / "missing.json"):
                with self.subTest(path=path):
                    result = subprocess.run(
                        [sys.executable, str(REPO / "http_connect_proxy.py"), "--config", str(path)],
                        cwd=directory, capture_output=True, text=True, timeout=5, check=False,
                    )
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("cannot load allowlist", result.stderr)

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
                "    client.sendall(b'CONNECT denied.example:443 HTTP/1.1\\r\\n\\r\\n')\n"
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
