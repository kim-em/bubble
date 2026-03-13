"""Tests for the GitHub auth proxy module."""

import json
import threading
import time
from collections import deque
from io import BytesIO
from unittest.mock import MagicMock

import pytest

from bubble.auth_proxy import (
    LEVEL_GH_READ,
    LEVEL_GH_READWRITE,
    LEVEL_GIT_ONLY,
    LEVEL_REST_READ,
    AuthProxyHandler,
    AuthTokenRegistry,
    GitHubTokenRefresher,
    ProxyRateLimiter,
    ThreadedHTTPServer,
    _build_api_url,
    _build_github_url,
    _collect_graphql_op_types,
    _parse_graphql_op_type,
    classify_graphql,
    validate_api_path,
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
        assert tokens[token]["level"] == LEVEL_GH_READ  # default
        assert tokens[token]["graphql_read"] == "whitelisted"
        assert tokens[token]["graphql_write"] == "whitelisted"

    def test_generate_token_with_level(self, auth_proxy_env):
        import bubble.auth_proxy

        token = bubble.auth_proxy.generate_auth_token("c1", "o", "r", level=LEVEL_GIT_ONLY)
        tokens = bubble.auth_proxy._load_tokens()
        assert tokens[token]["level"] == LEVEL_GIT_ONLY

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
        handler.token_refresher = GitHubTokenRefresher("ghp_test_token")

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
        AuthProxyHandler.token_refresher = GitHubTokenRefresher("ghp_test_token")

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
            # 401 = GitHub rejected our fake token (4xx passed through)
            # 404 = GitHub returned not found for the fake repo
            # 502 = proxy couldn't reach GitHub (network error)
            assert e.code in (401, 404, 502)
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
# API path validation
# ---------------------------------------------------------------------------


class TestValidateApiPath:
    # --- Allowed patterns ---

    def test_get_pulls(self):
        err = validate_api_path(
            "/repos/owner/repo/pulls/123", "", "GET", "owner", "repo", LEVEL_REST_READ
        )
        assert err is None

    def test_get_actions_runs(self):
        err = validate_api_path(
            "/repos/owner/repo/actions/runs", "", "GET", "owner", "repo", LEVEL_REST_READ
        )
        assert err is None

    def test_get_issues(self):
        err = validate_api_path(
            "/repos/owner/repo/issues", "state=open", "GET", "owner", "repo", LEVEL_REST_READ
        )
        assert err is None

    def test_head_allowed(self):
        err = validate_api_path(
            "/repos/owner/repo/pulls", "", "HEAD", "owner", "repo", LEVEL_REST_READ
        )
        assert err is None

    def test_get_repo_root(self):
        err = validate_api_path("/repos/owner/repo", "", "GET", "owner", "repo", LEVEL_REST_READ)
        assert err is None

    def test_case_insensitive(self):
        err = validate_api_path(
            "/repos/Owner/Repo/pulls", "", "GET", "owner", "repo", LEVEL_REST_READ
        )
        assert err is None

    # --- Write methods ---

    def test_post_blocked_at_level_2(self):
        err = validate_api_path(
            "/repos/owner/repo/issues/1/comments", "", "POST", "owner", "repo", LEVEL_REST_READ
        )
        assert err is not None
        assert "not allowed" in err.lower()

    def test_post_blocked_at_level_3(self):
        err = validate_api_path(
            "/repos/owner/repo/issues/1/comments", "", "POST", "owner", "repo", LEVEL_GH_READ
        )
        assert err is not None

    def test_post_allowed_at_level_4(self):
        err = validate_api_path(
            "/repos/owner/repo/issues/1/comments", "", "POST", "owner", "repo", LEVEL_GH_READWRITE
        )
        assert err is None

    def test_patch_allowed_at_level_4(self):
        err = validate_api_path(
            "/repos/owner/repo/pulls/1", "", "PATCH", "owner", "repo", LEVEL_GH_READWRITE
        )
        assert err is None

    def test_delete_allowed_at_level_4(self):
        err = validate_api_path(
            "/repos/owner/repo/comments/1", "", "DELETE", "owner", "repo", LEVEL_GH_READWRITE
        )
        assert err is None

    # --- Blocked patterns ---

    def test_wrong_repo(self):
        err = validate_api_path(
            "/repos/owner/other/pulls", "", "GET", "owner", "repo", LEVEL_REST_READ
        )
        assert err is not None
        assert "mismatch" in err.lower()

    def test_wrong_owner(self):
        err = validate_api_path(
            "/repos/hacker/repo/pulls", "", "GET", "owner", "repo", LEVEL_REST_READ
        )
        assert err is not None

    def test_non_repo_path(self):
        err = validate_api_path("/user", "", "GET", "owner", "repo", LEVEL_REST_READ)
        assert err is not None
        assert "pattern" in err.lower()

    def test_encoded_slash(self):
        err = validate_api_path(
            "/repos/owner%2frepo/pulls", "", "GET", "owner", "repo", LEVEL_REST_READ
        )
        assert err is not None

    def test_dot_segment(self):
        err = validate_api_path(
            "/repos/owner/repo/../../etc/passwd", "", "GET", "owner", "repo", LEVEL_REST_READ
        )
        assert err is not None


# ---------------------------------------------------------------------------
# API URL building
# ---------------------------------------------------------------------------


class TestBuildApiUrl:
    def test_basic(self):
        url = _build_api_url("/repos/owner/repo/pulls", "state=open")
        assert url == "https://api.github.com/repos/owner/repo/pulls?state=open"

    def test_no_query(self):
        url = _build_api_url("/repos/owner/repo/pulls/123", "")
        assert url == "https://api.github.com/repos/owner/repo/pulls/123"

    def test_graphql(self):
        url = _build_api_url("/graphql", "")
        assert url == "https://api.github.com/graphql"


# ---------------------------------------------------------------------------
# GraphQL parsing
# ---------------------------------------------------------------------------


class TestParseGraphqlOpType:
    def test_anonymous_query(self):
        assert _parse_graphql_op_type("{ repository { name } }") == "query"

    def test_named_query(self):
        assert _parse_graphql_op_type("query MyQuery { repository { name } }") == "query"

    def test_mutation(self):
        assert _parse_graphql_op_type("mutation { addComment(input: {}) { id } }") == "mutation"

    def test_named_mutation(self):
        assert _parse_graphql_op_type("mutation AddComment { addComment { id } }") == "mutation"

    def test_subscription(self):
        assert _parse_graphql_op_type("subscription { onIssue { id } }") == "subscription"

    def test_with_comments(self):
        query = """
        # This is a comment
        query {
            repository { name }
        }
        """
        assert _parse_graphql_op_type(query) == "query"

    def test_fragment_then_query(self):
        query = """
        fragment RepoFields on Repository {
            name
            description
        }
        query {
            repository(owner: "foo", name: "bar") {
                ...RepoFields
            }
        }
        """
        assert _parse_graphql_op_type(query) == "query"

    def test_fragment_then_mutation(self):
        query = """
        fragment F on Issue { title }
        mutation { addComment(input: {}) { id } }
        """
        assert _parse_graphql_op_type(query) == "mutation"

    def test_empty(self):
        assert _parse_graphql_op_type("") is None

    def test_only_comments(self):
        assert _parse_graphql_op_type("# just a comment") is None

    def test_whitespace_before_query(self):
        assert _parse_graphql_op_type("   \n  query { repo { name } }") == "query"

    def test_case_insensitive(self):
        assert _parse_graphql_op_type("QUERY { repo { name } }") == "query"
        assert _parse_graphql_op_type("Mutation { addComment { id } }") == "mutation"

    def test_multi_operation_mutation_detected(self):
        """A document with query + mutation returns 'mutation' (most dangerous)."""
        query = "query Safe { viewer { login } } mutation Evil { __typename }"
        assert _parse_graphql_op_type(query) == "mutation"

    def test_multi_operation_all_queries(self):
        query = "query A { viewer { login } } query B { repository { name } }"
        assert _parse_graphql_op_type(query) == "query"

    def test_operationName_bypass_blocked(self):
        """operationName selecting a mutation in a multi-op doc is caught."""
        query = "query Safe { viewer { login } } mutation Evil { __typename }"
        # Even though operationName would select Evil, the parser
        # scans ALL operations and finds the mutation
        assert _parse_graphql_op_type(query) == "mutation"


class TestCollectGraphqlOpTypes:
    def test_single_query(self):
        assert _collect_graphql_op_types("query { viewer { login } }") == ["query"]

    def test_single_mutation(self):
        assert _collect_graphql_op_types("mutation { addComment { id } }") == ["mutation"]

    def test_anonymous_query(self):
        assert _collect_graphql_op_types("{ viewer { login } }") == ["query"]

    def test_multi_ops(self):
        ops = _collect_graphql_op_types("query A { viewer { login } } mutation B { __typename }")
        assert ops == ["query", "mutation"]

    def test_fragment_then_ops(self):
        ops = _collect_graphql_op_types(
            "fragment F on User { name } query A { viewer { ...F } } mutation B { __typename }"
        )
        assert ops == ["query", "mutation"]

    def test_empty(self):
        assert _collect_graphql_op_types("") == []

    def test_only_comments(self):
        assert _collect_graphql_op_types("# just a comment") == []

    def test_string_with_braces(self):
        """Braces inside string literals must not confuse brace counting."""
        query = 'query { repository { issue(body: "{ }") { id } } }'
        assert _collect_graphql_op_types(query) == ["query"]

    def test_mutation_after_string_with_braces(self):
        """A mutation following a query whose string contains braces must be detected."""
        query = (
            'query A { repository { issue(body: "} }") { id } } } mutation B { addComment { id } }'
        )
        assert _collect_graphql_op_types(query) == ["query", "mutation"]

    def test_block_string_with_braces(self):
        """Block strings (triple-quoted) with braces must be handled."""
        query = 'query { repository { issue(body: """{ extra { nested } }""") { id } } }'
        assert _collect_graphql_op_types(query) == ["query"]


class TestClassifyGraphql:
    def test_valid_query(self):
        body = json.dumps({"query": "query { repository { name } }"}).encode()
        op_type, err = classify_graphql(body)
        assert op_type == "query"
        assert err is None

    def test_valid_mutation(self):
        body = json.dumps({"query": "mutation { addComment { id } }"}).encode()
        op_type, err = classify_graphql(body)
        assert op_type == "mutation"
        assert err is None

    def test_malformed_json(self):
        op_type, err = classify_graphql(b"not json")
        assert op_type is None
        assert "malformed" in err.lower()

    def test_batched_request(self):
        body = json.dumps([{"query": "query { x }"}, {"query": "query { y }"}]).encode()
        op_type, err = classify_graphql(body)
        assert op_type is None
        assert "batched" in err.lower()

    def test_missing_query_field(self):
        body = json.dumps({"variables": {}}).encode()
        op_type, err = classify_graphql(body)
        assert op_type is None
        assert "query" in err.lower()

    def test_non_string_query(self):
        body = json.dumps({"query": 123}).encode()
        op_type, err = classify_graphql(body)
        assert op_type is None

    def test_subscription_rejected(self):
        body = json.dumps({"query": "subscription { onEvent { id } }"}).encode()
        op_type, err = classify_graphql(body)
        assert op_type is None
        assert "subscription" in err.lower()

    def test_anonymous_query(self):
        body = json.dumps({"query": "{ repository { name } }"}).encode()
        op_type, err = classify_graphql(body)
        assert op_type == "query"
        assert err is None

    def test_gh_pr_view_query(self):
        """Real-world gh pr view GraphQL query."""
        query = """query PullRequestByNumber($owner: String!, $repo: String!, $pr_number: Int!) {
            repository(owner: $owner, name: $repo) {
                pullRequest(number: $pr_number) {
                    title
                    body
                    state
                }
            }
        }"""
        variables = {"owner": "o", "repo": "r", "pr_number": 1}
        body = json.dumps({"query": query, "variables": variables}).encode()
        op_type, err = classify_graphql(body)
        assert op_type == "query"
        assert err is None

    def test_gh_pr_comment_mutation(self):
        """Real-world gh pr comment GraphQL mutation."""
        query = """mutation AddComment($subjectId: ID!, $body: String!) {
            addComment(input: {subjectId: $subjectId, body: $body}) {
                commentEdge { node { id } }
            }
        }"""
        body = json.dumps({"query": query}).encode()
        op_type, err = classify_graphql(body)
        assert op_type == "mutation"
        assert err is None

    def test_operationName_bypass_blocked(self):
        """Multi-op document with operationName selecting mutation is classified as mutation."""
        body = json.dumps(
            {
                "query": "query Safe { viewer { login } } mutation Evil { __typename }",
                "operationName": "Evil",
            }
        ).encode()
        op_type, err = classify_graphql(body)
        assert op_type == "mutation"
        assert err is None


# ---------------------------------------------------------------------------
# Handler: Authorization header token extraction
# ---------------------------------------------------------------------------


class TestAuthorizationHeaderAuth:
    def _make_handler(self, headers=None, token_info=None):
        handler = AuthProxyHandler.__new__(AuthProxyHandler)
        mock_headers = MagicMock()
        header_dict = dict(headers or {})
        mock_headers.get = lambda k, d=None: header_dict.get(k, d)
        handler.headers = mock_headers

        handler.token_registry = AuthTokenRegistry()
        if token_info:
            handler.token_registry.lookup = lambda t: token_info if t == "valid-token" else None

        return handler

    def test_x_bubble_token_preferred(self):
        handler = self._make_handler(
            headers={"X-Bubble-Token": "valid-token", "Authorization": "token other-token"},
        )
        assert handler._get_container_token() == "valid-token"

    def test_authorization_token_fallback(self):
        handler = self._make_handler(
            headers={"Authorization": "token valid-token"},
        )
        assert handler._get_container_token() == "valid-token"

    def test_authorization_bearer_fallback(self):
        handler = self._make_handler(
            headers={"Authorization": "Bearer valid-token"},
        )
        assert handler._get_container_token() == "valid-token"

    def test_no_auth(self):
        handler = self._make_handler()
        assert handler._get_container_token() is None

    def test_invalid_authorization_format(self):
        handler = self._make_handler(
            headers={"Authorization": "Basic dXNlcjpwYXNz"},
        )
        assert handler._get_container_token() is None


# ---------------------------------------------------------------------------
# Handler: routing by access level
# ---------------------------------------------------------------------------


class TestAccessLevelRouting:
    @staticmethod
    def _tinfo(level):
        return {
            "container": "c1",
            "owner": "owner",
            "repo": "repo",
            "level": level,
        }

    def _make_handler(self, method, path, headers=None, body=None, token_info=None):
        """Create a mock handler with the given request parameters."""
        handler = AuthProxyHandler.__new__(AuthProxyHandler)
        handler.command = method
        handler.path = path
        handler.rfile = BytesIO(body or b"")
        handler.wfile = BytesIO()
        handler.requestline = f"{method} {path} HTTP/1.1"
        handler.request_version = "HTTP/1.1"

        mock_headers = MagicMock()
        header_dict = dict(headers or {})
        mock_headers.get = lambda k, d=None: header_dict.get(k, d)
        mock_headers.__iter__ = lambda s: iter(header_dict.items())
        mock_headers.items = lambda: header_dict.items()
        handler.headers = mock_headers

        handler.token_registry = AuthTokenRegistry()
        handler.rate_limiter = ProxyRateLimiter()
        handler.token_refresher = GitHubTokenRefresher("ghp_test_token")

        if token_info:
            handler.token_registry.lookup = lambda t: token_info if t == "valid-token" else None

        handler._responses = []

        def mock_send_response(code, message=None):
            handler._responses.append(code)

        handler.send_response = mock_send_response
        handler.send_header = lambda k, v: None
        handler.end_headers = lambda: None

        return handler

    def test_git_request_allowed_at_level_1(self):
        handler = self._make_handler(
            "GET",
            "/git/owner/repo/info/refs?service=git-upload-pack",
            headers={"X-Bubble-Token": "valid-token"},
            token_info=self._tinfo(LEVEL_GIT_ONLY),
        )
        handler._proxy_request("GET")
        assert 403 not in handler._responses

    def test_api_request_blocked_at_level_1(self):
        handler = self._make_handler(
            "GET",
            "/repos/owner/repo/pulls",
            headers={"X-Bubble-Token": "valid-token"},
            token_info=self._tinfo(LEVEL_GIT_ONLY),
        )
        handler._proxy_request("GET")
        assert 403 in handler._responses

    def test_api_request_allowed_at_level_2(self):
        handler = self._make_handler(
            "GET",
            "/repos/owner/repo/pulls",
            headers={"X-Bubble-Token": "valid-token"},
            token_info=self._tinfo(LEVEL_REST_READ),
        )
        handler._proxy_request("GET")
        assert 403 not in handler._responses

    def test_graphql_blocked_at_level_2(self):
        body = json.dumps({"query": "{ repository { name } }"}).encode()
        handler = self._make_handler(
            "POST",
            "/graphql",
            headers={
                "X-Bubble-Token": "valid-token",
                "Content-Length": str(len(body)),
            },
            body=body,
            token_info=self._tinfo(LEVEL_REST_READ),
        )
        handler._proxy_request("POST")
        assert 403 in handler._responses

    def test_graphql_query_allowed_at_level_3(self):
        body = json.dumps({"query": "query { repository { name } }"}).encode()
        handler = self._make_handler(
            "POST",
            "/graphql",
            headers={
                "X-Bubble-Token": "valid-token",
                "Content-Length": str(len(body)),
            },
            body=body,
            token_info=self._tinfo(LEVEL_GH_READ),
        )
        handler._proxy_request("POST")
        assert 403 not in handler._responses

    def test_graphql_mutation_blocked_at_level_3(self):
        body = json.dumps({"query": "mutation { addComment { id } }"}).encode()
        handler = self._make_handler(
            "POST",
            "/graphql",
            headers={
                "X-Bubble-Token": "valid-token",
                "Content-Length": str(len(body)),
            },
            body=body,
            token_info=self._tinfo(LEVEL_GH_READ),
        )
        handler._proxy_request("POST")
        assert 403 in handler._responses

    def test_graphql_mutation_allowed_at_level_4(self):
        body = json.dumps({"query": "mutation { addComment { id } }"}).encode()
        handler = self._make_handler(
            "POST",
            "/graphql",
            headers={
                "X-Bubble-Token": "valid-token",
                "Content-Length": str(len(body)),
            },
            body=body,
            token_info=self._tinfo(LEVEL_GH_READWRITE),
        )
        handler._proxy_request("POST")
        assert 403 not in handler._responses

    def test_unknown_route_blocked(self):
        handler = self._make_handler(
            "GET",
            "/user",
            headers={"X-Bubble-Token": "valid-token"},
            token_info=self._tinfo(LEVEL_GH_READ),
        )
        handler._proxy_request("GET")
        assert 403 in handler._responses


# ---------------------------------------------------------------------------
# Integration: API proxy round-trip
# ---------------------------------------------------------------------------


class TestApiProxyIntegration:
    @pytest.fixture
    def api_proxy_server(self, auth_proxy_env):
        """Start a real auth proxy server with level 3 token."""
        import bubble.auth_proxy

        token = bubble.auth_proxy.generate_auth_token(
            "test-container", "owner", "repo", level=LEVEL_GH_READ
        )
        registry = bubble.auth_proxy.AuthTokenRegistry()
        rate_limiter = bubble.auth_proxy.ProxyRateLimiter()

        AuthProxyHandler.token_registry = registry
        AuthProxyHandler.rate_limiter = rate_limiter
        AuthProxyHandler.token_refresher = GitHubTokenRefresher("ghp_test_token")

        server = ThreadedHTTPServer(("127.0.0.1", 0), AuthProxyHandler)
        port = server.server_address[1]

        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        yield {"server": server, "port": port, "token": token}

        server.shutdown()

    def test_api_get_proxied(self, api_proxy_server):
        """REST API GET request passes validation and reaches upstream."""
        import urllib.error
        import urllib.request

        port = api_proxy_server["port"]
        token = api_proxy_server["token"]
        req = urllib.request.Request(f"http://127.0.0.1:{port}/repos/owner/repo/pulls")
        req.add_header("Authorization", f"token {token}")
        try:
            urllib.request.urlopen(req, timeout=5)
        except urllib.error.HTTPError as e:
            # 401 = GitHub rejected fake token (4xx passed through)
            # 404 = GitHub returned not found for fake repo
            # 502 = proxy couldn't reach GitHub
            assert e.code in (401, 404, 502)
        except urllib.error.URLError:
            pass  # Network timeout is fine

    def test_api_wrong_repo_blocked(self, api_proxy_server):
        import urllib.request

        port = api_proxy_server["port"]
        token = api_proxy_server["token"]
        req = urllib.request.Request(f"http://127.0.0.1:{port}/repos/hacker/evil/pulls")
        req.add_header("Authorization", f"token {token}")
        try:
            urllib.request.urlopen(req)
            pytest.fail("Expected HTTP error")
        except urllib.error.HTTPError as e:
            assert e.code == 403

    def test_api_post_blocked_at_level_3(self, api_proxy_server):
        import urllib.request

        port = api_proxy_server["port"]
        token = api_proxy_server["token"]
        data = b'{"body": "test comment"}'
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/repos/owner/repo/issues/1/comments",
            data=data,
            method="POST",
        )
        req.add_header("Authorization", f"token {token}")
        req.add_header("Content-Type", "application/json")
        try:
            urllib.request.urlopen(req)
            pytest.fail("Expected HTTP error")
        except urllib.error.HTTPError as e:
            assert e.code == 403

    def test_graphql_query_proxied(self, api_proxy_server):
        """GraphQL query passes validation and reaches upstream."""
        import urllib.error
        import urllib.request

        port = api_proxy_server["port"]
        token = api_proxy_server["token"]
        query = (
            "query($owner: String!, $repo: String!)"
            " { repository(owner: $owner, name: $repo) { name } }"
        )
        data = json.dumps(
            {"query": query, "variables": {"owner": "owner", "repo": "repo"}}
        ).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/graphql",
            data=data,
            method="POST",
        )
        req.add_header("Authorization", f"token {token}")
        req.add_header("Content-Type", "application/json")
        try:
            urllib.request.urlopen(req, timeout=5)
        except urllib.error.HTTPError as e:
            # 401 = GitHub rejected fake token (4xx passed through)
            # 502 = proxy couldn't reach GitHub
            assert e.code in (401, 502)
        except urllib.error.URLError:
            pass

    def test_graphql_mutation_blocked(self, api_proxy_server):
        import urllib.request

        port = api_proxy_server["port"]
        token = api_proxy_server["token"]
        data = json.dumps({"query": "mutation { addComment(input: {}) { id } }"}).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/graphql",
            data=data,
            method="POST",
        )
        req.add_header("Authorization", f"token {token}")
        req.add_header("Content-Type", "application/json")
        try:
            urllib.request.urlopen(req)
            pytest.fail("Expected HTTP error")
        except urllib.error.HTTPError as e:
            assert e.code == 403

    def test_non_repo_path_blocked(self, api_proxy_server):
        import urllib.request

        port = api_proxy_server["port"]
        token = api_proxy_server["token"]
        req = urllib.request.Request(f"http://127.0.0.1:{port}/user")
        req.add_header("Authorization", f"token {token}")
        try:
            urllib.request.urlopen(req)
            pytest.fail("Expected HTTP error")
        except urllib.error.HTTPError as e:
            assert e.code == 403

    def test_authorization_header_auth(self, api_proxy_server):
        """Authorization header works as auth mechanism (for gh CLI)."""
        import urllib.error
        import urllib.request

        port = api_proxy_server["port"]
        token = api_proxy_server["token"]
        # Use Authorization: token (what gh sends) instead of X-Bubble-Token
        req = urllib.request.Request(f"http://127.0.0.1:{port}/repos/owner/repo/pulls")
        req.add_header("Authorization", f"token {token}")
        try:
            urllib.request.urlopen(req, timeout=5)
        except urllib.error.HTTPError as e:
            # 401 = GitHub rejected fake token (4xx passed through)
            # 404/502 = other upstream responses
            assert e.code in (401, 404, 502)  # Passed validation
        except urllib.error.URLError:
            pass

    def test_graphql_operationname_bypass_blocked(self, api_proxy_server):
        """Multi-op document selecting a mutation via operationName is blocked at level 3."""
        import urllib.request

        port = api_proxy_server["port"]
        token = api_proxy_server["token"]
        data = json.dumps(
            {
                "query": "query Safe { viewer { login } } mutation Evil { __typename }",
                "operationName": "Evil",
            }
        ).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/graphql",
            data=data,
            method="POST",
        )
        req.add_header("Authorization", f"token {token}")
        req.add_header("Content-Type", "application/json")
        try:
            urllib.request.urlopen(req)
            pytest.fail("Expected HTTP error")
        except urllib.error.HTTPError as e:
            assert e.code == 403

    def test_batched_graphql_blocked(self, api_proxy_server):
        import urllib.request

        port = api_proxy_server["port"]
        token = api_proxy_server["token"]
        data = json.dumps([{"query": "{ viewer { login } }"}]).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/graphql",
            data=data,
            method="POST",
        )
        req.add_header("Authorization", f"token {token}")
        req.add_header("Content-Type", "application/json")
        try:
            urllib.request.urlopen(req)
            pytest.fail("Expected HTTP error")
        except urllib.error.HTTPError as e:
            assert e.code == 400


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
