import os
import select
import signal
import socket
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path


PROXY = Path(__file__).parents[1] / "tools" / "ssh_latency_proxy.py"


class LocalTcpServer:
    def __init__(self, echo=False, eof_response=None):
        self.echo = echo
        self.eof_response = eof_response
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(1)
        self.port = self.listener.getsockname()[1]
        self.connected = threading.Event()
        self.stopping = threading.Event()
        self.condition = threading.Condition()
        self.received = bytearray()
        self.connection = None
        self.thread = threading.Thread(target=self._run)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.stopping.set()
        if self.connection is not None:
            try:
                self.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self.connection.close()
        self.listener.close()
        self.thread.join(timeout=2)
        self.assert_stopped()

    def _run(self):
        self.listener.settimeout(0.1)
        while not self.stopping.is_set():
            try:
                connection, _ = self.listener.accept()
                break
            except socket.timeout:
                continue
            except OSError:
                return
        else:
            return

        self.connection = connection
        self.connected.set()
        try:
            while data := connection.recv(65536):
                with self.condition:
                    self.received.extend(data)
                    self.condition.notify_all()
                if self.echo:
                    connection.sendall(data)
            if self.eof_response is not None:
                connection.sendall(self.eof_response)
        except OSError:
            pass
        finally:
            connection.close()

    def assert_stopped(self):
        if self.thread.is_alive():
            raise AssertionError("local TCP server did not stop")

    def wait_connected(self):
        if not self.connected.wait(2):
            raise AssertionError("proxy did not connect to local TCP server")

    def wait_received(self, size):
        deadline = time.monotonic() + 2
        with self.condition:
            while len(self.received) < size:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AssertionError(
                        f"server received {len(self.received)} bytes, expected {size}"
                    )
                self.condition.wait(remaining)
            return bytes(self.received[:size])

    def send(self, data):
        self.wait_connected()
        self.connection.sendall(data)


def read_exact(stream, size, timeout=2):
    deadline = time.monotonic() + timeout
    chunks = []
    remaining = size
    while remaining:
        wait = deadline - time.monotonic()
        if wait <= 0 or not select.select([stream], [], [], wait)[0]:
            raise AssertionError(f"timed out waiting for {size} bytes")
        data = os.read(stream.fileno(), remaining)
        if not data:
            raise AssertionError("proxy closed stdout before sending expected bytes")
        chunks.append(data)
        remaining -= len(data)
    return b"".join(chunks)


class SshLatencyProxyTest(unittest.TestCase):
    def start_proxy(self, port, delay_ms=0):
        return subprocess.Popen(
            [sys.executable, str(PROXY), "--delay-ms", str(delay_ms), "127.0.0.1", str(port)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def tearDown(self):
        proxy = getattr(self, "proxy", None)
        if proxy is None:
            return
        if proxy.stdin and not proxy.stdin.closed:
            proxy.stdin.close()
        if proxy.poll() is None:
            try:
                proxy.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proxy.terminate()
                proxy.wait(timeout=2)
                self.fail("proxy did not exit cleanly")
        if proxy.returncode != 0:
            self.fail(f"proxy exited with status {proxy.returncode}")
        if proxy.stdout:
            proxy.stdout.close()
        if proxy.stderr:
            proxy.stderr.close()

    def test_forwards_binary_data_in_both_directions(self):
        with LocalTcpServer(echo=True) as server:
            self.proxy = self.start_proxy(server.port)
            server.wait_connected()

            from_server = bytes(reversed(range(256)))
            server.send(from_server)
            self.assertEqual(read_exact(self.proxy.stdout, len(from_server)), from_server)

            from_client = bytes(range(256)) + b"\x00\xff\x00"
            self.proxy.stdin.write(from_client)
            self.proxy.stdin.flush()
            self.assertEqual(server.wait_received(len(from_client)), from_client)
            self.assertEqual(read_exact(self.proxy.stdout, len(from_client)), from_client)

    def test_applies_one_way_delay_in_both_directions(self):
        delay_ms = 100
        with LocalTcpServer() as server:
            self.proxy = self.start_proxy(server.port, delay_ms)
            server.wait_connected()

            from_client = b"client payload"
            started = time.monotonic()
            self.proxy.stdin.write(from_client)
            self.proxy.stdin.flush()
            self.assertEqual(server.wait_received(len(from_client)), from_client)
            elapsed = time.monotonic() - started
            self.assertGreaterEqual(elapsed, 0.08)
            self.assertLess(elapsed, 1.0)

            from_server = b"server payload"
            started = time.monotonic()
            server.send(from_server)
            self.assertEqual(read_exact(self.proxy.stdout, len(from_server)), from_server)
            elapsed = time.monotonic() - started
            self.assertGreaterEqual(elapsed, 0.08)
            self.assertLess(elapsed, 1.0)

    def test_delay_does_not_limit_throughput_after_initial_delay(self):
        delay_ms = 100
        payload = bytes(range(256)) * 4096
        with LocalTcpServer() as server:
            self.proxy = self.start_proxy(server.port, delay_ms)
            server.wait_connected()

            started = time.monotonic()
            self.proxy.stdin.write(payload)
            self.proxy.stdin.flush()
            self.assertEqual(server.wait_received(len(payload)), payload)
            elapsed = time.monotonic() - started

            self.assertGreaterEqual(elapsed, 0.08)
            self.assertLess(elapsed, 1.0)

    def test_ctrl_c_exits_cleanly(self):
        with LocalTcpServer() as server:
            self.proxy = self.start_proxy(server.port)
            server.wait_connected()
            self.proxy.send_signal(signal.SIGINT)
            self.assertEqual(self.proxy.wait(timeout=2), 0)
            self.assertEqual(self.proxy.stderr.read(), b"")

    def test_preserves_server_response_after_stdin_eof(self):
        response = b"response after stdin EOF"
        with LocalTcpServer(eof_response=response) as server:
            self.proxy = self.start_proxy(server.port)
            server.wait_connected()
            self.proxy.stdin.close()
            self.assertEqual(read_exact(self.proxy.stdout, len(response)), response)
            self.assertEqual(self.proxy.wait(timeout=2), 0)

    def test_remote_eof_exits_cleanly(self):
        with LocalTcpServer() as server:
            self.proxy = self.start_proxy(server.port)
            server.wait_connected()
            server.connection.shutdown(socket.SHUT_RDWR)
            server.connection.close()
            self.assertEqual(self.proxy.wait(timeout=2), 0)

    def test_stdin_eof_exits_cleanly(self):
        with LocalTcpServer() as server:
            self.proxy = self.start_proxy(server.port)
            server.wait_connected()
            self.proxy.stdin.close()
            self.assertEqual(self.proxy.wait(timeout=2), 0)

    def test_connection_failure_returns_nonzero_with_stderr(self):
        with socket.socket() as socket_:
            socket_.bind(("127.0.0.1", 0))
            port = socket_.getsockname()[1]

        result = subprocess.run(
            [sys.executable, str(PROXY), "--delay-ms", "0", "127.0.0.1", str(port)],
            input=b"",
            capture_output=True,
            timeout=2,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertRegex(result.stderr.decode(), r"(?i)(connect|connection|refused)")


if __name__ == "__main__":
    unittest.main()
