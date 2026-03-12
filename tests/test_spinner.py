"""Tests for the heartbeat spinner module."""

import io
import sys
import time

from bubble.spinner import heartbeat


class TestHeartbeat:
    def test_no_output_when_fast(self, monkeypatch):
        """No heartbeat message if operation finishes before delay."""
        buf = io.TextIOWrapper(io.BytesIO(), write_through=True)
        monkeypatch.setattr(sys, "stderr", buf)
        # Pretend stderr is a TTY
        monkeypatch.setattr(buf, "isatty", lambda: True)

        with heartbeat(delay=10.0, interval=10.0):
            pass  # instant

        buf.seek(0)
        assert buf.read() == ""

    def test_prints_after_delay(self, monkeypatch):
        """Heartbeat message appears after the delay elapses."""
        buf = io.TextIOWrapper(io.BytesIO(), write_through=True)
        monkeypatch.setattr(sys, "stderr", buf)
        monkeypatch.setattr(buf, "isatty", lambda: True)

        with heartbeat(message="working...", delay=0.1, interval=0.1):
            time.sleep(0.35)

        buf.seek(0)
        output = buf.read()
        assert "working..." in output

    def test_disabled_when_not_tty(self, monkeypatch):
        """No output when stderr is not a TTY."""
        buf = io.TextIOWrapper(io.BytesIO(), write_through=True)
        monkeypatch.setattr(sys, "stderr", buf)
        # isatty returns False by default for BytesIO-backed streams

        with heartbeat(message="working...", delay=0.1, interval=0.1):
            time.sleep(0.35)

        buf.seek(0)
        assert buf.read() == ""

    def test_stops_on_exit(self, monkeypatch):
        """Heartbeat stops printing after context exits."""
        buf = io.TextIOWrapper(io.BytesIO(), write_through=True)
        monkeypatch.setattr(sys, "stderr", buf)
        monkeypatch.setattr(buf, "isatty", lambda: True)

        with heartbeat(message="tick", delay=0.05, interval=0.05):
            time.sleep(0.2)

        buf.seek(0)
        count_during = buf.read().count("tick")

        # Sleep more after context exit — count should not grow
        time.sleep(0.2)
        buf.seek(0)
        count_after = buf.read().count("tick")
        assert count_after == count_during
