"""Tests for the bubble-in-bubble relay module."""

import json
import socket
import threading
import time

import pytest

from bubble.relay import (
    GLOBAL_RATE_LIMIT_PER_HOUR,
    MAX_REQUEST_SIZE,
    MAX_TARGET_LENGTH,
    MAX_TRACKED_CONTAINERS,
    RELAY_TOKENS,
    RateLimiter,
    TokenRegistry,
    _handle_connection,
    _load_tokens,
    _sanitize_for_log,
    _save_tokens,
    _send_response,
    generate_relay_token,
    remove_relay_token,
    validate_relay_target,
)


# ---------------------------------------------------------------------------
# RateLimiter
# ---------------------------------------------------------------------------


class TestRateLimiter:
    def test_allows_first_request(self):
        rl = RateLimiter()
        assert rl.check("container-1") is True

    def test_allows_three_in_a_minute(self):
        rl = RateLimiter()
        assert rl.check("c1") is True
        assert rl.check("c1") is True
        assert rl.check("c1") is True

    def test_rejects_fourth_in_a_minute(self):
        rl = RateLimiter()
        for _ in range(3):
            assert rl.check("c1") is True
        assert rl.check("c1") is False

    def test_different_containers_independent(self):
        rl = RateLimiter()
        for _ in range(3):
            rl.check("c1")
        assert rl.check("c1") is False
        assert rl.check("c2") is True

    def test_ten_minute_window(self):
        rl = RateLimiter()
        now = time.time()
        # Simulate 9 requests spread over 10 minutes (3 per minute window)
        q = rl._requests.setdefault("c1", __import__("collections").deque())
        for i in range(9):
            # Each at 2-minute intervals — clears the 1-minute window
            q.append(now - 600 + i * 65)
        # 10th request should still succeed (under 10/10min)
        assert rl.check("c1") is True

    def test_ten_minute_limit(self):
        rl = RateLimiter()
        now = time.time()
        q = rl._requests.setdefault("c1", __import__("collections").deque())
        for i in range(10):
            q.append(now - 500 + i * 50)
        assert rl.check("c1") is False

    def test_hour_window(self):
        rl = RateLimiter()
        now = time.time()
        q = rl._requests.setdefault("c1", __import__("collections").deque())
        for i in range(19):
            q.append(now - 3500 + i * 180)
        # Under all windows
        assert rl.check("c1") is True

    def test_hour_limit(self):
        rl = RateLimiter()
        now = time.time()
        q = rl._requests.setdefault("c1", __import__("collections").deque())
        for i in range(20):
            q.append(now - 3500 + i * 170)
        assert rl.check("c1") is False

    def test_old_entries_pruned(self):
        rl = RateLimiter()
        now = time.time()
        q = rl._requests.setdefault("c1", __import__("collections").deque())
        # Add entries from over an hour ago
        for i in range(20):
            q.append(now - 4000)
        # Should be pruned and allow new request
        assert rl.check("c1") is True

    def test_thread_safety(self):
        rl = RateLimiter()
        results = []

        def check():
            results.append(rl.check("c1"))

        threads = [threading.Thread(target=check) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Exactly 3 should succeed
        assert sum(results) == 3

    def test_global_rate_limit(self):
        rl = RateLimiter()
        now = time.time()
        # Fill global limit with requests from different containers
        for i in range(GLOBAL_RATE_LIMIT_PER_HOUR):
            rl._global.append(now - 100)
        # New request from any container should be rejected
        assert rl.check("new-container") is False

    def test_container_eviction(self):
        rl = RateLimiter()
        now = time.time()
        # Directly populate tracking dict to avoid global rate limit
        for i in range(MAX_TRACKED_CONTAINERS):
            q = rl._requests.setdefault(f"container-{i}", __import__("collections").deque())
            q.append(now - 3500)  # old enough to not trigger per-container limits
        # Adding one more should evict the oldest, not crash
        assert rl.check("new-container") is True
        assert len(rl._requests) <= MAX_TRACKED_CONTAINERS


# ---------------------------------------------------------------------------
# validate_relay_target
# ---------------------------------------------------------------------------


class TestValidateRelayTarget:
    def test_empty_target(self):
        status, msg = validate_relay_target("")
        assert status == "error"

    def test_none_target(self):
        status, msg = validate_relay_target(None)
        assert status == "error"

    def test_too_long(self):
        status, msg = validate_relay_target("a" * (MAX_TARGET_LENGTH + 1))
        assert status == "error"
        assert "too long" in msg.lower()

    def test_reject_dot_path(self):
        status, msg = validate_relay_target(".")
        assert status == "error"
        assert "local path" in msg.lower()

    def test_reject_relative_path(self):
        status, msg = validate_relay_target("./foo/bar")
        assert status == "error"
        assert "local path" in msg.lower()

    def test_reject_absolute_path(self):
        status, msg = validate_relay_target("/home/user/repo")
        assert status == "error"
        assert "local path" in msg.lower()

    def test_reject_dotdot_path(self):
        status, msg = validate_relay_target("../foo")
        assert status == "error"

    def test_reject_tilde_path(self):
        status, msg = validate_relay_target("~/repos/foo")
        assert status == "error"

    def test_reject_path_flag(self):
        status, msg = validate_relay_target("--path foo")
        assert status == "error"

    def test_reject_semicolons(self):
        status, msg = validate_relay_target("foo; rm -rf /")
        assert status == "error"
        assert "invalid characters" in msg.lower()

    def test_reject_pipe(self):
        status, msg = validate_relay_target("foo | cat")
        assert status == "error"

    def test_reject_ampersand(self):
        status, msg = validate_relay_target("foo & echo pwned")
        assert status == "error"

    def test_reject_backticks(self):
        status, msg = validate_relay_target("`whoami`")
        assert status == "error"

    def test_reject_dollar(self):
        status, msg = validate_relay_target("$(whoami)")
        assert status == "error"

    def test_reject_backslash(self):
        status, msg = validate_relay_target("foo\\bar")
        assert status == "error"

    def test_reject_path_traversal(self):
        status, msg = validate_relay_target("owner/../etc/passwd")
        assert status == "error"
        assert "path traversal" in msg.lower()

    def test_reject_dotdot_in_owner(self):
        status, msg = validate_relay_target("../evil/repo")
        assert status == "error"

    def test_reject_dotdot_in_repo(self):
        """owner/.. would resolve to GIT_DIR parent without this check."""
        status, msg = validate_relay_target("owner/..")
        assert status == "error"

    def test_unknown_repo(self, tmp_path, monkeypatch):
        # Point BUBBLE_HOME to a temp dir so no repos are "known"
        monkeypatch.setenv("BUBBLE_HOME", str(tmp_path))
        # Re-import to pick up new DATA_DIR
        import importlib
        import bubble.config
        importlib.reload(bubble.config)
        import bubble.git_store
        importlib.reload(bubble.git_store)
        import bubble.relay
        importlib.reload(bubble.relay)

        status, msg = bubble.relay.validate_relay_target("leanprover/lean4")
        assert status == "unknown_repo"
        assert "not available" in msg.lower()

    def test_known_repo(self, tmp_path, monkeypatch):
        # Set up a fake known repo
        monkeypatch.setenv("BUBBLE_HOME", str(tmp_path))
        import importlib
        import bubble.config
        importlib.reload(bubble.config)
        import bubble.git_store
        importlib.reload(bubble.git_store)
        import bubble.relay
        importlib.reload(bubble.relay)

        # Create fake bare repo directory
        git_dir = tmp_path / "git"
        git_dir.mkdir()
        (git_dir / "lean4.git").mkdir()

        # Also need a repos.json for the registry
        repos_file = tmp_path / "repos.json"
        repos_file.write_text("{}")

        status, msg = bubble.relay.validate_relay_target("leanprover/lean4")
        assert status == "ok"

    def test_valid_pr_target(self, tmp_path, monkeypatch):
        monkeypatch.setenv("BUBBLE_HOME", str(tmp_path))
        import importlib
        import bubble.config
        importlib.reload(bubble.config)
        import bubble.git_store
        importlib.reload(bubble.git_store)
        import bubble.relay
        importlib.reload(bubble.relay)

        git_dir = tmp_path / "git"
        git_dir.mkdir()
        (git_dir / "lean4.git").mkdir()
        repos_file = tmp_path / "repos.json"
        repos_file.write_text("{}")

        status, msg = bubble.relay.validate_relay_target("leanprover/lean4/pull/123")
        assert status == "ok"


# ---------------------------------------------------------------------------
# Protocol / JSON handling
# ---------------------------------------------------------------------------


class TestRelayProtocol:
    def test_send_response(self):
        """Test that _send_response sends valid JSON."""
        # Create a socketpair for testing
        s1, s2 = socket.socketpair()
        try:
            _send_response(s1, "ok", "test message")
            data = s2.recv(4096)
            response = json.loads(data.decode())
            assert response["status"] == "ok"
            assert response["message"] == "test message"
        finally:
            s1.close()
            s2.close()

    def test_handle_malformed_json(self):
        """Test handling of non-JSON input."""
        s1, s2 = socket.socketpair()
        rl = RateLimiter()
        try:
            s1.sendall(b"this is not json\n")
            s1.shutdown(socket.SHUT_WR)
            _handle_connection(s2, rl)
            # s1 should have received an error response
            data = s1.recv(4096)
            response = json.loads(data.decode())
            assert response["status"] == "error"
        finally:
            s1.close()
            s2.close()

    def test_handle_empty_data(self):
        """Test handling of empty connection."""
        s1, s2 = socket.socketpair()
        rl = RateLimiter()
        try:
            s1.shutdown(socket.SHUT_WR)
            _handle_connection(s2, rl)
            # Should not crash, just close cleanly
        finally:
            s1.close()
            s2.close()

    def test_handle_oversized_request(self):
        """Test that oversized requests are handled."""
        s1, s2 = socket.socketpair()
        rl = RateLimiter()
        try:
            # Send more than MAX_REQUEST_SIZE bytes
            big = json.dumps({"target": "a" * 2000})
            s1.sendall(big.encode())
            s1.shutdown(socket.SHUT_WR)
            _handle_connection(s2, rl)
            data = s1.recv(4096)
            response = json.loads(data.decode())
            assert response["status"] == "error"
        finally:
            s1.close()
            s2.close()

    def test_handle_rate_limited(self):
        """Test rate limiting through the handler (no token auth)."""
        rl = RateLimiter()
        # With token_registry=None, handler uses container="unknown"
        for _ in range(3):
            rl.check("unknown")

        s1, s2 = socket.socketpair()
        try:
            request = json.dumps({"target": "leanprover/lean4"})
            s1.sendall(request.encode())
            s1.shutdown(socket.SHUT_WR)
            # token_registry=None disables auth, container defaults to "unknown"
            _handle_connection(s2, rl, token_registry=None)
            data = s1.recv(4096)
            response = json.loads(data.decode())
            assert response["status"] == "rate_limited"
        finally:
            s1.close()
            s2.close()

    def test_handle_local_path_rejected(self):
        """Test that local paths are rejected through the handler."""
        rl = RateLimiter()
        s1, s2 = socket.socketpair()
        try:
            request = json.dumps({"target": "./some/path", "container": "c1"})
            s1.sendall(request.encode())
            s1.shutdown(socket.SHUT_WR)
            _handle_connection(s2, rl, token_registry=None)
            data = s1.recv(4096)
            response = json.loads(data.decode())
            assert response["status"] == "error"
            assert "local path" in response["message"].lower()
        finally:
            s1.close()
            s2.close()


# ---------------------------------------------------------------------------
# Log sanitization
# ---------------------------------------------------------------------------


class TestSanitizeForLog:
    def test_strips_newlines(self):
        assert _sanitize_for_log("foo\nbar") == "foo\\nbar"

    def test_strips_carriage_return(self):
        assert _sanitize_for_log("foo\rbar") == "foo\\rbar"

    def test_strips_tabs(self):
        assert _sanitize_for_log("foo\tbar") == "foo\\tbar"

    def test_normal_string_unchanged(self):
        assert _sanitize_for_log("leanprover/lean4") == "leanprover/lean4"


# ---------------------------------------------------------------------------
# Token authentication
# ---------------------------------------------------------------------------


class TestTokenManagement:
    def test_generate_token(self, tmp_path, monkeypatch):
        monkeypatch.setenv("BUBBLE_HOME", str(tmp_path))
        import importlib
        import bubble.config
        importlib.reload(bubble.config)
        import bubble.relay
        importlib.reload(bubble.relay)

        token = bubble.relay.generate_relay_token("my-container")
        assert len(token) == 64  # 32 bytes hex = 64 chars
        tokens = bubble.relay._load_tokens()
        assert tokens[token] == "my-container"

    def test_generate_multiple_tokens(self, tmp_path, monkeypatch):
        monkeypatch.setenv("BUBBLE_HOME", str(tmp_path))
        import importlib
        import bubble.config
        importlib.reload(bubble.config)
        import bubble.relay
        importlib.reload(bubble.relay)

        t1 = bubble.relay.generate_relay_token("container-1")
        t2 = bubble.relay.generate_relay_token("container-2")
        assert t1 != t2
        tokens = bubble.relay._load_tokens()
        assert tokens[t1] == "container-1"
        assert tokens[t2] == "container-2"

    def test_remove_token(self, tmp_path, monkeypatch):
        monkeypatch.setenv("BUBBLE_HOME", str(tmp_path))
        import importlib
        import bubble.config
        importlib.reload(bubble.config)
        import bubble.relay
        importlib.reload(bubble.relay)

        t1 = bubble.relay.generate_relay_token("keep-me")
        t2 = bubble.relay.generate_relay_token("remove-me")
        bubble.relay.remove_relay_token("remove-me")
        tokens = bubble.relay._load_tokens()
        assert t1 in tokens
        assert t2 not in tokens

    def test_token_registry_lookup(self, tmp_path, monkeypatch):
        monkeypatch.setenv("BUBBLE_HOME", str(tmp_path))
        import importlib
        import bubble.config
        importlib.reload(bubble.config)
        import bubble.relay
        importlib.reload(bubble.relay)

        token = bubble.relay.generate_relay_token("my-container")
        registry = bubble.relay.TokenRegistry()
        assert registry.lookup(token) == "my-container"
        assert registry.lookup("invalid-token") is None


class TestTokenAuth:
    def test_missing_token_rejected(self):
        """With token registry active, missing token is rejected."""
        rl = RateLimiter()
        tr = TokenRegistry()  # empty registry
        s1, s2 = socket.socketpair()
        try:
            request = json.dumps({"target": "leanprover/lean4"})
            s1.sendall(request.encode())
            s1.shutdown(socket.SHUT_WR)
            _handle_connection(s2, rl, token_registry=tr)
            data = s1.recv(4096)
            response = json.loads(data.decode())
            assert response["status"] == "error"
            assert "token" in response["message"].lower()
        finally:
            s1.close()
            s2.close()

    def test_invalid_token_rejected(self):
        """With token registry active, invalid token is rejected."""
        rl = RateLimiter()
        tr = TokenRegistry()
        s1, s2 = socket.socketpair()
        try:
            request = json.dumps({"target": "leanprover/lean4", "token": "fake-token"})
            s1.sendall(request.encode())
            s1.shutdown(socket.SHUT_WR)
            _handle_connection(s2, rl, token_registry=tr)
            data = s1.recv(4096)
            response = json.loads(data.decode())
            assert response["status"] == "error"
            assert "invalid" in response["message"].lower()
        finally:
            s1.close()
            s2.close()

    def test_valid_token_accepted(self, tmp_path, monkeypatch):
        """With valid token, request proceeds to target validation."""
        monkeypatch.setenv("BUBBLE_HOME", str(tmp_path))
        import importlib
        import bubble.config
        importlib.reload(bubble.config)
        import bubble.git_store
        importlib.reload(bubble.git_store)
        import bubble.relay
        importlib.reload(bubble.relay)

        token = bubble.relay.generate_relay_token("my-container")
        rl = bubble.relay.RateLimiter()
        tr = bubble.relay.TokenRegistry()

        s1, s2 = socket.socketpair()
        try:
            # Target will fail validation (unknown repo), but that's after auth
            request = json.dumps({"target": "leanprover/lean4", "token": token})
            s1.sendall(request.encode())
            s1.shutdown(socket.SHUT_WR)
            bubble.relay._handle_connection(s2, rl, token_registry=tr)
            data = s1.recv(4096)
            response = json.loads(data.decode())
            # Should get past auth — will fail on unknown_repo or error, not token
            assert response["status"] in ("unknown_repo", "error")
            assert "token" not in response["message"].lower()
        finally:
            s1.close()
            s2.close()

    def test_rate_limit_uses_authenticated_name(self, tmp_path, monkeypatch):
        """Rate limiting is keyed on authenticated container, not spoofable."""
        monkeypatch.setenv("BUBBLE_HOME", str(tmp_path))
        import importlib
        import bubble.config
        importlib.reload(bubble.config)
        import bubble.git_store
        importlib.reload(bubble.git_store)
        import bubble.relay
        importlib.reload(bubble.relay)

        token = bubble.relay.generate_relay_token("my-container")
        rl = bubble.relay.RateLimiter()
        tr = bubble.relay.TokenRegistry()

        # Use up the rate limit for "my-container"
        for _ in range(3):
            rl.check("my-container")

        s1, s2 = socket.socketpair()
        try:
            request = json.dumps({"target": "leanprover/lean4", "token": token})
            s1.sendall(request.encode())
            s1.shutdown(socket.SHUT_WR)
            bubble.relay._handle_connection(s2, rl, token_registry=tr)
            data = s1.recv(4096)
            response = json.loads(data.decode())
            assert response["status"] == "rate_limited"
        finally:
            s1.close()
            s2.close()
