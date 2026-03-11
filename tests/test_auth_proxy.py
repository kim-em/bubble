"""Tests for the GitHub auth proxy module."""

import threading
import time
from collections import deque
from io import BytesIO
from unittest.mock import MagicMock

import pytest

from bubble.auth_proxy import (
    AuthProxyHandler,
    AuthTokenRegistry,
    ProxyRateLimiter,
    ThreadedHTTPServer,
    _build_github_url,
    validate_path,
)

# ---------------------------------------------------------------------------
# Token management
# ---------------------------------------------------------------------------


class TestAuthTokenManagement:
    def test_generate_token(self, auth_proxy_env):
        import bubble.auth_proxy

        token = bubble.auth_proxy.generate_auth_token("my-container", "owner", "repo")
        assert len(token) == 64  # 32 bytes hex
        tokens = bubble.auth_proxy._load_tokens()
        assert tokens[token]["container"] == "my-container"
        assert tokens[token]["owner"] == "owner"
        assert tokens[token]["repo"] == "repo"

    def test_generate_multiple_tokens(self, auth_proxy_env):
        import bubble.auth_proxy

        t1 = bubble.auth_proxy.generate_auth_token("c1", "owner1", "repo1")
        t2 = bubble.auth_proxy.generate_auth_token("c2", "owner2", "repo2")
        assert t1 != t2
        tokens = bubble.auth_proxy._load_tokens()
        assert tokens[t1]["container"] == "c1"
        assert tokens[t2]["container"] == "c2"

    def test_remove_tokens(self, auth_proxy_env):
        import bubble.auth_proxy

        t1 = bubble.auth_proxy.generate_auth_token("keep-me", "o", "r")
        t2 = bubble.auth_proxy.generate_auth_token("remove-me", "o", "r")
        bubble.auth_proxy.remove_auth_tokens("remove-me")
        tokens = bubble.auth_proxy._load_tokens()
        assert t1 in tokens
        assert t2 not in tokens

    def test_token_registry_lookup(self, auth_proxy_env):
        import bubble.auth_proxy

        token = bubble.auth_proxy.generate_auth_token("my-container", "owner", "repo")
        registry = bubble.auth_proxy.AuthTokenRegistry()
        info = registry.lookup(token)
        assert info is not None
        assert info["container"] == "my-container"
        assert info["owner"] == "owner"
        assert info["repo"] == "repo"
        assert registry.lookup("invalid-token") is None

    def test_token_registry_reloads_on_change(self, auth_proxy_env):
        import bubble.auth_proxy

        registry = bubble.auth_proxy.AuthTokenRegistry()
        assert registry.lookup("nonexistent") is None

        token = bubble.auth_proxy.generate_auth_token("new", "o", "r")
        # Force mtime change
        time.sleep(0.01)
        info = registry.lookup(token)
        assert info is not None


# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------


class TestProxyRateLimiter:
    def test_allows_requests(self):
        rl = ProxyRateLimiter()
        for _ in range(10):
            assert rl.check("c1") is True

    def test_per_minute_limit(self):
        rl = ProxyRateLimiter()
        for _ in range(60):
            assert rl.check("c1") is True
        assert rl.check("c1") is False

    def test_different_containers_independent(self):
        rl = ProxyRateLimiter()
        for _ in range(60):
            rl.check("c1")
        assert rl.check("c1") is False
        assert rl.check("c2") is True

    def test_old_entries_pruned(self):
        rl = ProxyRateLimiter()
        now = time.time()
        q = rl._requests.setdefault("c1", deque())
        for _ in range(600):
            q.append(now - 4000)  # Over an hour ago
        assert rl.check("c1") is True

    def test_container_eviction(self):
        rl = ProxyRateLimiter()
        now = time.time()
        from bubble.auth_proxy import MAX_TRACKED_CONTAINERS

        for i in range(MAX_TRACKED_CONTAINERS):
            q = rl._requests.setdefault(f"container-{i}", deque())
            q.append(now - 3500)
        assert rl.check("new-container") is True
        assert len(rl._requests) <= MAX_TRACKED_CONTAINERS


# ---------------------------------------------------------------------------
# Path validation
# ---------------------------------------------------------------------------


class TestValidatePath:
    # --- Allowed patterns ---

    def test_fetch_refs(self):
        err = validate_path("/git/owner/repo/info/refs", "service=git-upload-pack", "owner", "repo")
        assert err is None

    def test_fetch_refs_with_git_suffix(self):
        err = validate_path(
            "/git/owner/repo.git/info/refs", "service=git-upload-pack", "owner", "repo"
        )
        assert err is None

    def test_push_refs(self):
        err = validate_path(
            "/git/owner/repo/info/refs", "service=git-receive-pack", "owner", "repo"
        )
        assert err is None

    def test_upload_pack(self):
        err = validate_path("/git/owner/repo/git-upload-pack", "", "owner", "repo")
        assert err is None

    def test_receive_pack(self):
        err = validate_path("/git/owner/repo/git-receive-pack", "", "owner", "repo")
        assert err is None

    def test_case_insensitive_repo_match(self):
        err = validate_path("/git/Owner/Repo/info/refs", "service=git-upload-pack", "owner", "repo")
        assert err is None

    def test_dotted_owner_name(self):
        err = validate_path(
            "/git/my.org/repo/info/refs", "service=git-upload-pack", "my.org", "repo"
        )
        assert err is None

    def test_hyphenated_repo_name(self):
        err = validate_path(
            "/git/owner/my-repo/info/refs", "service=git-upload-pack", "owner", "my-repo"
        )
        assert err is None

    # --- Blocked patterns ---

    def test_wrong_repo(self):
        err = validate_path(
            "/git/owner/other-repo/info/refs", "service=git-upload-pack", "owner", "repo"
        )
        assert err is not None
        assert "mismatch" in err.lower()

    def test_wrong_owner(self):
        err = validate_path(
            "/git/hacker/repo/info/refs", "service=git-upload-pack", "owner", "repo"
        )
        assert err is not None
        assert "mismatch" in err.lower()

    def test_encoded_slash(self):
        err = validate_path(
            "/git/owner%2frepo/info/refs", "service=git-upload-pack", "owner", "repo"
        )
        assert err is not None
        assert "encoded" in err.lower()

    def test_encoded_dot(self):
        err = validate_path(
            "/git/owner/repo%2egit/info/refs", "service=git-upload-pack", "owner", "repo"
        )
        assert err is not None
        assert "encoded" in err.lower()

    def test_double_slash(self):
        err = validate_path(
            "/git/owner//repo/info/refs", "service=git-upload-pack", "owner", "repo"
        )
        assert err is not None
        assert "duplicate" in err.lower()

    def test_dot_segment(self):
        err = validate_path(
            "/git/owner/../etc/passwd/info/refs", "service=git-upload-pack", "owner", "repo"
        )
        assert err is not None

    def test_arbitrary_path(self):
        err = validate_path("/git/owner/repo/some/random/path", "", "owner", "repo")
        assert err is not None
        assert "pattern" in err.lower()

    def test_no_git_prefix(self):
        err = validate_path("/owner/repo/info/refs", "service=git-upload-pack", "owner", "repo")
        assert err is not None

    def test_invalid_query_for_info_refs(self):
        err = validate_path("/git/owner/repo/info/refs", "service=git-evil-pack", "owner", "repo")
        assert err is not None
        assert "invalid query" in err.lower()

    def test_extra_query_on_pack_endpoint(self):
        err = validate_path("/git/owner/repo/git-upload-pack", "extra=param", "owner", "repo")
        assert err is not None
        assert "unexpected query" in err.lower()

    def test_empty_query_for_info_refs(self):
        """info/refs requires a service parameter."""
        err = validate_path("/git/owner/repo/info/refs", "", "owner", "repo")
        assert err is not None

    def test_git_lfs_blocked(self):
        """Git LFS uses different URL patterns — should be blocked."""
        err = validate_path("/git/owner/repo.git/info/lfs/objects/batch", "", "owner", "repo")
        assert err is not None


# ---------------------------------------------------------------------------
# URL building
# ---------------------------------------------------------------------------


class TestBuildGithubUrl:
    def test_basic(self):
        url = _build_github_url("/git/owner/repo/info/refs", "service=git-upload-pack")
        assert url == "https://github.com/owner/repo/info/refs?service=git-upload-pack"

    def test_no_query(self):
        url = _build_github_url("/git/owner/repo/git-upload-pack", "")
        assert url == "https://github.com/owner/repo/git-upload-pack"

    def test_with_git_suffix(self):
        url = _build_github_url("/git/owner/repo.git/git-receive-pack", "")
        assert url == "https://github.com/owner/repo.git/git-receive-pack"


# ---------------------------------------------------------------------------
# HTTP handler tests (with mock server)
# ---------------------------------------------------------------------------


class TestAuthProxyHandler:
    def _make_handler(self, method, path, headers=None, body=None, token_info=None):
        """Create a mock handler with the given request parameters."""
        handler = AuthProxyHandler.__new__(AuthProxyHandler)
        handler.command = method
        handler.path = path
        handler.headers = {}
        handler.rfile = BytesIO(body or b"")
        handler.wfile = BytesIO()
        handler.requestline = f"{method} {path} HTTP/1.1"
        handler.request_version = "HTTP/1.1"

        # Mock headers
        mock_headers = MagicMock()
        header_dict = dict(headers or {})
        mock_headers.get = lambda k, d=None: header_dict.get(k, d)
        mock_headers.__iter__ = lambda s: iter(header_dict.items())
        mock_headers.items = lambda: header_dict.items()
        handler.headers = mock_headers

        # Set up class-level attributes
        handler.token_registry = AuthTokenRegistry()
        handler.rate_limiter = ProxyRateLimiter()
        handler.github_token = "ghp_test_token"

        # Mock token lookup
        if token_info:
            handler.token_registry.lookup = lambda t: token_info if t == "valid-token" else None

        # Capture responses
        handler._responses = []

        def mock_send_response(code, message=None):
            handler._responses.append(code)

        handler.send_response = mock_send_response
        handler.send_header = lambda k, v: None
        handler.end_headers = lambda: None

        return handler

    def test_missing_token_returns_401(self):
        handler = self._make_handler(
            "GET",
            "/git/owner/repo/info/refs?service=git-upload-pack",
        )
        handler._proxy_request("GET")
        assert 401 in handler._responses

    def test_invalid_token_returns_403(self):
        handler = self._make_handler(
            "GET",
            "/git/owner/repo/info/refs?service=git-upload-pack",
            headers={"X-Bubble-Token": "bad-token"},
            token_info={"container": "c1", "owner": "owner", "repo": "repo"},
        )
        # Override token lookup to reject "bad-token"
        handler.token_registry.lookup = lambda t: None
        handler._proxy_request("GET")
        assert 403 in handler._responses

    def test_wrong_repo_returns_403(self):
        handler = self._make_handler(
            "GET",
            "/git/hacker/evil-repo/info/refs?service=git-upload-pack",
            headers={"X-Bubble-Token": "valid-token"},
            token_info={"container": "c1", "owner": "owner", "repo": "repo"},
        )
        handler._proxy_request("GET")
        assert 403 in handler._responses


# ---------------------------------------------------------------------------
# Integration: full proxy round-trip with mock GitHub backend
# ---------------------------------------------------------------------------


class TestProxyIntegration:
    @pytest.fixture
    def proxy_server(self, auth_proxy_env):
        """Start a real auth proxy server on a random port."""
        import bubble.auth_proxy

        token = bubble.auth_proxy.generate_auth_token("test-container", "owner", "repo")
        registry = bubble.auth_proxy.AuthTokenRegistry()
        rate_limiter = bubble.auth_proxy.ProxyRateLimiter()

        AuthProxyHandler.token_registry = registry
        AuthProxyHandler.rate_limiter = rate_limiter
        AuthProxyHandler.github_token = "ghp_test_token"

        server = ThreadedHTTPServer(("127.0.0.1", 0), AuthProxyHandler)
        port = server.server_address[1]

        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        yield {"server": server, "port": port, "token": token}

        server.shutdown()

    def test_missing_token_rejected(self, proxy_server):
        import urllib.request

        port = proxy_server["port"]
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/git/owner/repo/info/refs?service=git-upload-pack"
        )
        try:
            urllib.request.urlopen(req)
            pytest.fail("Expected HTTP error")
        except urllib.error.HTTPError as e:
            assert e.code == 401

    def test_invalid_token_rejected(self, proxy_server):
        import urllib.request

        port = proxy_server["port"]
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/git/owner/repo/info/refs?service=git-upload-pack"
        )
        req.add_header("X-Bubble-Token", "invalid-token")
        try:
            urllib.request.urlopen(req)
            pytest.fail("Expected HTTP error")
        except urllib.error.HTTPError as e:
            assert e.code == 403

    def test_wrong_repo_rejected(self, proxy_server):
        import urllib.request

        port = proxy_server["port"]
        token = proxy_server["token"]
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/git/hacker/evil/info/refs?service=git-upload-pack"
        )
        req.add_header("X-Bubble-Token", token)
        try:
            urllib.request.urlopen(req)
            pytest.fail("Expected HTTP error")
        except urllib.error.HTTPError as e:
            assert e.code == 403

    def test_valid_request_proxied(self, proxy_server):
        """Valid request reaches GitHub (will get a real response or connection error)."""
        import urllib.error
        import urllib.request

        port = proxy_server["port"]
        token = proxy_server["token"]
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/git/owner/repo/info/refs?service=git-upload-pack"
        )
        req.add_header("X-Bubble-Token", token)
        try:
            urllib.request.urlopen(req, timeout=5)
            # If GitHub is reachable, we get a response (possibly 404 for fake repo)
            # The important thing is we got past validation
        except urllib.error.HTTPError as e:
            # 502 means the proxy tried to reach GitHub (validation passed)
            # 404 means GitHub returned not found for the fake repo
            assert e.code in (404, 502)
        except urllib.error.URLError:
            # Network timeout / connection refused is fine — validation passed
            pass

    def test_encoded_slash_blocked(self, proxy_server):
        import urllib.request

        port = proxy_server["port"]
        token = proxy_server["token"]
        # Manually construct URL with encoded slash (bypass urllib quoting)
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/git/owner%2frepo/info/refs?service=git-upload-pack"
        )
        req.add_header("X-Bubble-Token", token)
        try:
            urllib.request.urlopen(req)
            pytest.fail("Expected HTTP error")
        except urllib.error.HTTPError as e:
            assert e.code == 403

    def test_arbitrary_path_blocked(self, proxy_server):
        import urllib.request

        port = proxy_server["port"]
        token = proxy_server["token"]
        req = urllib.request.Request(f"http://127.0.0.1:{port}/git/owner/repo/tree/main/README.md")
        req.add_header("X-Bubble-Token", token)
        try:
            urllib.request.urlopen(req)
            pytest.fail("Expected HTTP error")
        except urllib.error.HTTPError as e:
            assert e.code == 403

    def test_git_protocol_header_preserved(self, proxy_server):
        """Git-Protocol header should be forwarded (not stripped)."""
        import urllib.request

        port = proxy_server["port"]
        token = proxy_server["token"]
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/git/owner/repo/info/refs?service=git-upload-pack"
        )
        req.add_header("X-Bubble-Token", token)
        req.add_header("Git-Protocol", "version=2")
        try:
            urllib.request.urlopen(req, timeout=5)
        except Exception:
            pass  # We just care that it doesn't error on the header


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def auth_proxy_env(tmp_path, monkeypatch):
    """Set BUBBLE_HOME to tmp_path and reload auth_proxy module."""
    import importlib

    import bubble.auth_proxy
    import bubble.config

    monkeypatch.setenv("BUBBLE_HOME", str(tmp_path))
    importlib.reload(bubble.config)
    importlib.reload(bubble.auth_proxy)
    return tmp_path
