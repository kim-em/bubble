"""Heartbeat messages for slow, silent operations.

Prints periodic status messages to stderr when an operation produces no
output for several seconds.  Automatically disabled when stderr is not a
TTY (piped output, CI, ``--machine-readable``).
"""

from __future__ import annotations

import sys
import threading
from contextlib import contextmanager


@contextmanager
def heartbeat(
    message: str = "  still working...",
    delay: float = 5.0,
    interval: float = 10.0,
):
    """Print periodic heartbeat messages to stderr during slow operations.

    After *delay* seconds of silence, prints *message* every *interval*
    seconds until the context exits.  Does nothing when stderr is not a TTY.
    """
    if not sys.stderr.isatty():
        yield
        return

    stop = threading.Event()

    def _worker():
        if stop.wait(delay):
            return
        while True:
            print(message, file=sys.stderr, flush=True)
            if stop.wait(interval):
                return

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    try:
        yield
    finally:
        stop.set()
        t.join(timeout=1.0)
