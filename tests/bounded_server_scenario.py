"""Child-process scenarios for ``bubble.artifact_cache._BoundedServerMixin``.

Run by ``tests/test_artifact_cache.py`` under ``sys.executable`` with a parent-enforced
deadline: the
failure these scenarios exist to catch is an accept loop blocked forever on a leaked permit, and a
wedged server thread must not survive inside the pytest process. Every network operation and wait
here has a finite timeout; a failure exits non-zero with its traceback on stderr.

Usage: python tests/bounded_server_scenario.py <threaded|stdlib> <sequential|concurrency> SLOTS N
  sequential:  N separate TCP connections, one request each, then every permit is re-acquired with
               a bounded wait and returned — the leak regression.
  concurrency: handlers block until told to finish; N connections are opened at once and the peak
               number of simultaneously active handlers is reported (must equal min(N, SLOTS)),
               with a further connection admitted only after a permit is released.
"""

from __future__ import annotations

import json
import socket
import sys
import threading
from contextlib import closing
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(
    0, str(Path(__file__).resolve().parent.parent)
)  # the checkout, not an installed bubble

from bubble import artifact_cache  # noqa: E402
from bubble.auth_proxy import ThreadedHTTPServer  # noqa: E402

BASES = {"threaded": ThreadedHTTPServer, "stdlib": ThreadingHTTPServer}
TIMEOUT = 5.0


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    gate: threading.Event | None = None  # concurrency mode: handlers wait on it
    lock = threading.Lock()
    active = 0
    peak = 0

    def do_GET(self):
        cls = type(self)
        with cls.lock:
            cls.active += 1
            cls.peak = max(cls.peak, cls.active)
        try:
            if cls.gate is not None:
                cls.gate.wait(TIMEOUT)
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")
        finally:
            with cls.lock:
                cls.active -= 1

    def log_message(self, *args):
        pass


def get(port: int) -> None:
    with closing(HTTPConnection("127.0.0.1", port, timeout=TIMEOUT)) as connection:
        connection.request("GET", "/", headers={"Connection": "close"})
        with connection.getresponse() as response:
            assert response.status == 200, response.status
            assert response.read() == b"ok"


def main(base: str, mode: str, slots: int, n: int) -> dict:
    class Server(artifact_cache._BoundedServerMixin, BASES[base]):
        _request_slots = threading.BoundedSemaphore(slots)  # test-local, never the production bound

    server = Server(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    serving = threading.Thread(target=server.serve_forever, name="scenario-server", daemon=True)
    serving.start()
    result: dict = {"base": base, "mode": mode, "slots": slots, "n": n}
    if mode == "sequential":
        for i in range(n):  # a NEW connection per request: the leak is per accepted connection
            get(port)
        result["answered"] = n
    else:
        Handler.gate = threading.Event()
        clients = [threading.Thread(target=get, args=(port,), daemon=True) for _ in range(n)]
        for c in clients:
            c.start()
        # Wait until every permit is in use (the first min(n, slots) handlers are inside do_GET).
        deadline = TIMEOUT
        import time

        t0 = time.monotonic()
        while time.monotonic() - t0 < deadline:
            with Handler.lock:
                if Handler.active >= min(n, slots):
                    break
            time.sleep(0.01)
        with Handler.lock:
            result["active_at_capacity"] = Handler.active
        # With every permit held, one more connection is accepted by the kernel but not admitted:
        # its request must not reach a handler until a permit is released.
        extra = socket.create_connection(("127.0.0.1", port), timeout=TIMEOUT)
        extra.sendall(b"GET / HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
        time.sleep(0.3)
        with Handler.lock:
            result["active_with_extra_pending"] = Handler.active
            result["peak_before_release"] = Handler.peak
        Handler.gate.set()  # let everyone finish; the extra one is admitted as permits return
        for c in clients:
            c.join(TIMEOUT)
            assert not c.is_alive(), "a client never got its response"
        extra.settimeout(TIMEOUT)
        data = extra.recv(4096)
        extra.close()
        assert b" 200 " in data, data[:80]
        result["peak"] = Handler.peak
    # Every permit comes back. A client can finish reading just before its server thread completes
    # cleanup, so each acquisition is timed rather than asserted immediately; what is taken is returned.
    taken = 0
    try:
        for _ in range(slots):
            assert server._request_slots.acquire(timeout=TIMEOUT), (
                f"permit {taken + 1}/{slots} never came back"
            )
            taken += 1
    finally:
        for _ in range(taken):
            server._request_slots.release()
    result["permits_recovered"] = taken
    server.shutdown()
    serving.join(TIMEOUT)
    assert not serving.is_alive(), "the serving thread did not stop"
    server.server_close()
    result["ok"] = True
    return result


if __name__ == "__main__":
    base, mode, slots, n = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4])
    print(json.dumps(main(base, mode, slots, n)))
