"""HTTP reverse proxy for repo-scoped GitHub authentication.

Keeps the host's GitHub token on the host side. Containers use
git's `url.insteadOf` to route HTTPS requests through this proxy,
which validates the request targets the allowed repository, then
adds the real Authorization header before forwarding to GitHub.

The host GitHub token never enters the container. Each container
gets a per-container bearer token that only works against this
proxy and is scoped to a single repository.

Access policies (per-container):
  rest_api:       whether repo-scoped REST API access is allowed
  graphql_read:   "whitelisted", "unrestricted", or "none"
  graphql_write:  "whitelisted", "unrestricted", or "none"

Security model:
- Git: strict 4-pattern allowlist (git smart HTTP protocol only)
- REST API: path-validated against /repos/{owner}/{repo}/...
- GraphQL: controlled by graphql_read/graphql_write policies
- Path canonicalization rejects encoded separators, dot-segments,
  duplicate slashes
- Redirect following for API responses (CI logs) with hardened rules:
  GET/HEAD only, HTTPS only, allowlisted hosts, max 2 hops,
  auth headers stripped, response size capped
- Pinned outbound to github.com/api.github.com with TLS verification
- Ignores ambient HTTPS_PROXY/ALL_PROXY to prevent token leakage
- Per-container token isolation via X-Bubble-Token or Authorization header
- Rate limited + logged (reuses relay patterns)

On macOS (Colima): TCP listener, port saved to ~/.bubble/auth-proxy.port.
On Linux: TCP listener on 127.0.0.1 (Incus proxy needs TCP for HTTP).
"""

import base64
import json
import logging
import os
import re
import ssl
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.error import HTTPError
from urllib.parse import urlparse
from urllib.request import (
    BaseHandler,
    HTTPSHandler,
    ProxyHandler,
    Request,
    build_opener,
)

from .config import DATA_DIR
from .token_store import RateLimiter as _RateLimiter
from .token_store import RateWindow, TokenStore, setup_file_logging

AUTH_PROXY_PORT_FILE = DATA_DIR / "auth-proxy.port"
AUTH_PROXY_LOG = DATA_DIR / "auth-proxy.log"
AUTH_PROXY_TOKENS = DATA_DIR / "auth-tokens.json"

# Default port (configurable via config.toml)
DEFAULT_PORT = 7654

# Maximum concurrent handler threads (HTTPServer is threaded)
MAX_CONCURRENT_HANDLERS = 8

# Maximum request body size for git pack data (256 MB)
MAX_BODY_SIZE = 256 * 1024 * 1024

# Rate limiting: per-container
RATE_LIMIT_PER_MINUTE = 60
RATE_LIMIT_PER_HOUR = 600

# Maximum tracked containers
MAX_TRACKED_CONTAINERS = 100

# ---------------------------------------------------------------------------
# Path patterns
# ---------------------------------------------------------------------------

# Allowed git smart HTTP path patterns (the only 4 patterns git uses)
# Matches: /{owner}/{repo}[.git]/info/refs?service=git-{upload,receive}-pack
#          /{owner}/{repo}[.git]/git-{upload,receive}-pack
_VALID_OWNER_REPO = r"[a-zA-Z0-9._-]+"
_GIT_PATH_RE = re.compile(
    r"^/git/"
    + _VALID_OWNER_REPO
    + r"/"
    + _VALID_OWNER_REPO
    + r"(?:\.git)?"
    + r"/(info/refs|git-upload-pack|git-receive-pack)$"
)

# Allowed query strings for git endpoints
_ALLOWED_QUERIES = {
    "info/refs": {"service=git-upload-pack", "service=git-receive-pack"},
    "git-upload-pack": set(),
    "git-receive-pack": set(),
}

# REST API path pattern: /repos/{owner}/{repo}/...
_API_PATH_RE = re.compile(r"^/repos/" + _VALID_OWNER_REPO + r"/" + _VALID_OWNER_REPO + r"(/.*)?$")

# GitHub hosts
GITHUB_HOST = "github.com"
GITHUB_URL = f"https://{GITHUB_HOST}"
GITHUB_API_HOST = "api.github.com"
GITHUB_API_URL = f"https://{GITHUB_API_HOST}"

# Redirect following for API responses (e.g. CI log downloads)
MAX_REDIRECT_HOPS = 2
MAX_REDIRECT_RESPONSE_SIZE = 256 * 1024 * 1024  # 256 MB
REDIRECT_TIMEOUT = 60  # seconds

# Hosts allowed as redirect targets (fnmatch patterns)
_REDIRECT_ALLOWED_HOSTS = [
    "*.blob.core.windows.net",
    "*.githubusercontent.com",
]

logger = logging.getLogger("bubble.auth_proxy")


# ---------------------------------------------------------------------------
# Token management (backed by shared TokenStore)
# ---------------------------------------------------------------------------


def generate_auth_token(
    container_name: str,
    owner: str,
    repo: str,
    rest_api: bool = True,
    graphql_read: str = "whitelisted",
    graphql_write: str = "whitelisted",
) -> str:
    """Generate an auth proxy token for a container.

    The token maps to (container_name, owner, repo, rest_api, graphql_read,
    graphql_write) — the proxy uses this to validate requests and enforce
    the access policy.
    Uses file locking to prevent read-modify-write races.
    """
    return TokenStore(AUTH_PROXY_TOKENS).generate(
        {
            "container": container_name,
            "owner": owner,
            "repo": repo,
            "rest_api": rest_api,
            "graphql_read": graphql_read,
            "graphql_write": graphql_write,
        }
    )


def remove_auth_tokens(container_name: str):
    """Remove all auth proxy tokens for a container (e.g. on pop)."""
    TokenStore(AUTH_PROXY_TOKENS).remove(lambda v: v.get("container") == container_name)


def _load_tokens() -> dict:
    """Load token registry from disk."""
    return TokenStore(AUTH_PROXY_TOKENS)._load()


class AuthTokenRegistry:
    """Thread-safe token lookup with file-based persistence.

    Caches the tokens file and reloads when the mtime changes.
    """

    def __init__(self):
        self._store = TokenStore(AUTH_PROXY_TOKENS)

    def lookup(self, token: str) -> dict | None:
        """Look up a token. Returns {container, owner, repo} or None."""
        return self._store.lookup(token)


class ProxyRateLimiter(_RateLimiter):
    """Per-container rate limiter for the auth proxy.

    More generous than the relay (git does many requests per operation).
    """

    def __init__(self):
        super().__init__(
            windows=[RateWindow(60, RATE_LIMIT_PER_MINUTE), RateWindow(3600, RATE_LIMIT_PER_HOUR)],
            max_tracked=MAX_TRACKED_CONTAINERS,
        )


# ---------------------------------------------------------------------------
# Path validation
# ---------------------------------------------------------------------------


def validate_path(path: str, query: str, owner: str, repo: str) -> str | None:
    """Validate a request path against the git smart HTTP allowlist.

    Returns an error message string, or None if the path is valid.
    """
    # Reject encoded separators and dot-segments in raw path
    if "%2f" in path.lower() or "%2F" in path:
        return "Encoded path separators not allowed"
    if "%2e" in path.lower() or "%2E" in path:
        return "Encoded dots not allowed"
    if "//" in path:
        return "Duplicate slashes not allowed"
    if "/.." in path or "../" in path:
        return "Dot-segments not allowed"

    # Match against the allowlist
    m = _GIT_PATH_RE.match(path)
    if not m:
        return "Path does not match git smart HTTP pattern"

    # Extract owner/repo from path
    # Path format: /git/{owner}/{repo}[.git]/{endpoint}
    parts = path.split("/")
    # parts[0] = '', parts[1] = 'git', parts[2] = owner, parts[3] = repo[.git], ...
    path_owner = parts[2]
    path_repo = parts[3]
    # Strip .git suffix if present
    if path_repo.endswith(".git"):
        path_repo = path_repo[:-4]

    # Validate owner/repo matches the allowed repo
    if path_owner.lower() != owner.lower() or path_repo.lower() != repo.lower():
        return f"Repository mismatch: {path_owner}/{path_repo} != {owner}/{repo}"

    # Validate query string
    endpoint = m.group(1)
    allowed = _ALLOWED_QUERIES.get(endpoint, set())
    if endpoint == "info/refs":
        if query not in allowed:
            return f"Invalid query string for {endpoint}: {query}"
    else:
        if query:
            return f"Unexpected query string for {endpoint}"

    return None


def _build_github_url(path: str, query: str) -> str:
    """Build the upstream GitHub URL from a validated git request path.

    Strips the /git/ prefix and constructs the full GitHub URL.
    """
    # Remove /git/ prefix
    github_path = path[4:]  # "/git/owner/repo/..." -> "/owner/repo/..."
    url = f"{GITHUB_URL}{github_path}"
    if query:
        url += f"?{query}"
    return url


def _build_api_url(path: str, query: str) -> str:
    """Build the upstream GitHub API URL from a validated API path."""
    url = f"{GITHUB_API_URL}{path}"
    if query:
        url += f"?{query}"
    return url


# ---------------------------------------------------------------------------
# API path validation
# ---------------------------------------------------------------------------


def validate_api_path(path: str, query: str, method: str, owner: str, repo: str) -> str | None:
    """Validate a REST API request path.

    REST is always repo-scoped by path validation, so all HTTP methods
    are allowed when REST access is enabled.

    Returns an error message string, or None if the path is valid.
    """
    # Same encoding/traversal checks as git paths
    if "%2f" in path.lower() or "%2F" in path:
        return "Encoded path separators not allowed"
    if "%2e" in path.lower() or "%2E" in path:
        return "Encoded dots not allowed"
    if "//" in path:
        return "Duplicate slashes not allowed"
    if "/.." in path or "../" in path:
        return "Dot-segments not allowed"

    # Match against repo-scoped API path pattern
    m = _API_PATH_RE.match(path)
    if not m:
        return "Path does not match /repos/{owner}/{repo}/... pattern"

    # Extract owner/repo from path
    parts = path.split("/")
    # parts[0] = '', parts[1] = 'repos', parts[2] = owner, parts[3] = repo, ...
    path_owner = parts[2]
    path_repo = parts[3]

    if path_owner.lower() != owner.lower() or path_repo.lower() != repo.lower():
        return f"Repository mismatch: {path_owner}/{path_repo} != {owner}/{repo}"

    return None


# ---------------------------------------------------------------------------
# GraphQL validation (level 3+)
# ---------------------------------------------------------------------------


def _skip_braced_tokens(tokens: list, start: int) -> int:
    """Skip a balanced { ... } block in a token list starting at *start*.

    tokens[start] must be a '{' punct token.
    Returns the index after the closing '}', or -1 on error.
    """
    if start >= len(tokens) or tokens[start].value != "{":
        return -1
    depth = 1
    i = start + 1
    while i < len(tokens) and depth > 0:
        v = tokens[i].value
        if v == "{":
            depth += 1
        elif v == "}":
            depth -= 1
        i += 1
    return i if depth == 0 else -1


def _collect_graphql_op_types(query: str) -> list[str]:
    """Extract ALL operation types from a GraphQL document.

    Returns a list of operation types found (e.g. ['query', 'mutation']).
    Handles line comments, fragment definitions, and anonymous queries.
    Multiple operations in a single document are all reported.

    Uses the string-aware tokenizer from graphql_validator so that braces
    inside string literals are not miscounted.
    """
    from .graphql_validator import _tokenize

    tokens = _tokenize(query)
    if not tokens:
        return []

    ops: list[str] = []
    i = 0

    while i < len(tokens):
        tok = tokens[i]

        # Anonymous query starts with {
        if tok.kind == "punct" and tok.value == "{":
            ops.append("query")
            end = _skip_braced_tokens(tokens, i)
            if end == -1:
                break
            i = end
            continue

        if tok.kind == "ident":
            # Skip fragment definitions: fragment Name on Type { ... }
            if tok.value == "fragment":
                # Scan forward to the opening brace
                j = i + 1
                while j < len(tokens) and tokens[j].value != "{":
                    j += 1
                if j >= len(tokens):
                    break
                end = _skip_braced_tokens(tokens, j)
                if end == -1:
                    break
                i = end
                continue

            # Check for operation keyword
            if tok.value.lower() in ("query", "mutation", "subscription"):
                ops.append(tok.value.lower())
                # Scan forward to the opening brace
                j = i + 1
                while j < len(tokens) and tokens[j].value != "{":
                    j += 1
                if j >= len(tokens):
                    break
                end = _skip_braced_tokens(tokens, j)
                if end == -1:
                    break
                i = end
                continue

        # Unrecognized token — stop parsing
        break

    return ops


def _parse_graphql_op_type(query: str) -> str | None:
    """Extract the highest-privilege operation type from a GraphQL document.

    Returns 'mutation' if any mutation is present, 'subscription' if any
    subscription is present, 'query' if only queries, or None if empty.

    This is the safe classifier: if a document contains both a query and
    a mutation, it returns 'mutation' regardless of operationName.
    """
    ops = _collect_graphql_op_types(query)
    if not ops:
        return None
    # Return the most dangerous operation type present
    if "subscription" in ops:
        return "subscription"
    if "mutation" in ops:
        return "mutation"
    return "query"


def classify_graphql(body: bytes) -> tuple[str | None, str | None]:
    """Classify a GraphQL request body.

    Returns (operation_type, error_message).
    operation_type is 'query' or 'mutation' if valid, None on error.
    error_message is set on validation failure.

    Security: scans ALL operations in the document and returns the
    most dangerous one. This prevents operationName-based bypasses
    where a query is listed first but a mutation is selected for
    execution.
    """
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None, "Malformed JSON body"

    if isinstance(data, list):
        return None, "Batched requests not allowed"

    if not isinstance(data, dict):
        return None, "Invalid request format"

    query_str = data.get("query")
    if not query_str or not isinstance(query_str, str):
        return None, "Missing or invalid 'query' field"

    op_type = _parse_graphql_op_type(query_str)
    if op_type is None:
        return None, "Could not determine operation type"

    if op_type == "subscription":
        return None, "Subscriptions not supported"

    return op_type, None


# ---------------------------------------------------------------------------
# Redirect following for API responses
# ---------------------------------------------------------------------------


def _is_redirect_host_allowed(host: str) -> bool:
    """Check if a redirect target host is in the allowlist."""
    import fnmatch

    host = host.lower()
    return any(fnmatch.fnmatch(host, pat) for pat in _REDIRECT_ALLOWED_HOSTS)


def _follow_redirect(
    location: str, hops_remaining: int, method: str = "GET"
) -> tuple[int, dict, bytes]:
    """Follow a redirect URL with hardened rules.

    Returns (status_code, headers_dict, body).
    Raises ValueError on policy violations.
    """
    parsed = urlparse(location)
    if parsed.scheme != "https":
        raise ValueError(f"Redirect to non-HTTPS URL: {location}")

    if not _is_redirect_host_allowed(parsed.hostname or ""):
        raise ValueError(f"Redirect to disallowed host: {parsed.hostname}")

    if hops_remaining <= 0:
        raise ValueError("Too many redirects")

    ctx = ssl.create_default_context()
    opener = build_opener(
        ProxyHandler({}),
        HTTPSHandler(context=ctx),
        _NoRedirectHandler(),
    )

    req = Request(location, method=method)
    # Do NOT send Authorization or other sensitive headers to redirect target
    try:
        resp = opener.open(req, timeout=REDIRECT_TIMEOUT)
    except HTTPError as e:
        if 300 <= e.code < 400:
            next_location = e.headers.get("Location")
            if next_location:
                return _follow_redirect(next_location, hops_remaining - 1, method=method)
        raise

    # Read body with size cap
    body_parts = []
    total = 0
    while True:
        chunk = resp.read(65536)
        if not chunk:
            break
        total += len(chunk)
        if total > MAX_REDIRECT_RESPONSE_SIZE:
            raise ValueError("Redirect response too large")
        body_parts.append(chunk)

    headers = {k: v for k, v in resp.getheaders()}
    return resp.status, headers, b"".join(body_parts)


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------


class _NoRedirectHandler(BaseHandler):
    """urllib handler that rejects all HTTP redirects.

    Raises HTTPError for any 3xx response so the caller can return it
    to the client as-is without following the redirect (which would
    forward the Authorization header to a non-GitHub host).
    """

    def http_error_301(self, req, fp, code, msg, headers):
        raise HTTPError(req.full_url, code, msg, headers, fp)

    http_error_302 = http_error_301
    http_error_303 = http_error_301
    http_error_307 = http_error_301
    http_error_308 = http_error_301


class GitHubTokenRefresher:
    """Thread-safe GitHub token with automatic refresh on 401.

    The ``gh`` CLI stores OAuth tokens (``gho_*``) that expire after ~8 hours.
    Running ``gh auth token`` triggers an automatic refresh via the stored
    refresh token.  This class re-fetches the token when prompted, with a
    cooldown to avoid hammering ``gh`` on persistent auth failures.
    """

    _MIN_REFRESH_INTERVAL = 30  # seconds between refresh attempts

    def __init__(self, initial_token: str):
        self._token = initial_token
        self._lock = threading.Lock()
        self._last_refresh = 0.0

    @property
    def token(self) -> str:
        return self._token

    def refresh(self) -> str:
        """Re-fetch the token from ``gh auth token``.

        Returns the (possibly new) token.  Skips the subprocess call if
        a refresh was attempted within the last ``_MIN_REFRESH_INTERVAL``
        seconds to avoid contention when GitHub returns persistent 401s
        for reasons other than token expiry.
        """
        import time

        with self._lock:
            now = time.monotonic()
            if now - self._last_refresh < self._MIN_REFRESH_INTERVAL:
                return self._token
            self._last_refresh = now

        # Run outside the lock — subprocess may block
        try:
            new_token = _get_github_token()
        except Exception:
            return self._token

        with self._lock:
            old = self._token
            self._token = new_token
            if new_token != old:
                logger.info("GitHub token refreshed")
        return new_token


class AuthProxyHandler(BaseHTTPRequestHandler):
    """HTTP request handler for the git auth proxy."""

    # Per-socket read timeout (seconds) — prevents slowloris attacks
    timeout = 60

    # Class-level references set by the server
    token_registry: AuthTokenRegistry
    rate_limiter: ProxyRateLimiter
    token_refresher: GitHubTokenRefresher

    # Thread-safe caches for pre-flight queries (shared across handler instances)
    _repo_node_id_cache: dict[tuple[str, str], str] = {}
    _repo_node_id_lock = threading.Lock()

    def log_message(self, format, *args):
        """Route HTTP server logs to our logger."""
        logger.info(format, *args)

    def _get_container_token(self) -> str | None:
        """Extract the container auth token.

        Checks X-Bubble-Token first (git traffic via url.insteadOf),
        then Authorization header (gh traffic via http_unix_socket).
        """
        token = self.headers.get("X-Bubble-Token")
        if token:
            return token
        # gh sends Authorization: token <bubble-proxy-token>
        auth = self.headers.get("Authorization") or ""
        if auth.startswith("token "):
            return auth[6:].strip()
        if auth.startswith("Bearer "):
            return auth[7:].strip()
        return None

    def _authenticate(self) -> dict | None:
        """Authenticate the request via X-Bubble-Token.

        Returns the token info dict or None (sends error response).
        """
        token = self._get_container_token()
        if not token:
            self._send_error(401, "Missing X-Bubble-Token header")
            return None

        info = self.token_registry.lookup(token)
        if not info:
            self._send_error(403, "Invalid auth proxy token")
            return None

        return info

    def _send_error(self, code: int, message: str):
        """Send a plain-text error response."""
        self.send_response(code)
        self.send_header("Content-Type", "text/plain")
        body = (message + "\n").encode("utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _proxy_request(self, method: str):
        """Core proxy logic shared by all HTTP methods."""
        # Authenticate
        info = self._authenticate()
        if not info:
            return

        container = info["container"]
        owner = info["owner"]
        repo = info["repo"]
        rest_api = info.get("rest_api", False)
        graphql_read = info.get("graphql_read", "none")
        graphql_write = info.get("graphql_write", "none")

        # Rate limit
        if not self.rate_limiter.check(container):
            self._send_error(429, "Rate limited")
            logger.info("RATE_LIMITED container=%s", container)
            return

        # Parse path
        parsed = urlparse(self.path)
        path = parsed.path
        query = parsed.query

        # Read request body for methods that have one
        body = None
        if method in ("POST", "PUT", "PATCH"):
            body = self._read_body()
            if body is None:
                return  # Error already sent by _read_body()

        # Route: git smart HTTP (/git/...)
        if path.startswith("/git/"):
            self._handle_git_request(method, path, query, body, container, owner, repo)
            return

        # Route: GraphQL (/graphql)
        if path == "/graphql" and method == "POST":
            self._handle_graphql_request(body, container, owner, repo, graphql_read, graphql_write)
            return

        # Route: REST API (/repos/{owner}/{repo}/...)
        if path.startswith("/repos/"):
            self._handle_api_request(method, path, query, body, container, owner, repo, rest_api)
            return

        self._send_error(403, "Path not recognized")
        logger.info("BLOCKED %s %s container=%s reason=unknown_route", method, path, container)

    def _read_body(self) -> bytes | None:
        """Read request body (Content-Length or chunked). Returns None on error."""
        try:
            content_length = int(self.headers.get("Content-Length", 0))
        except (ValueError, TypeError):
            self._send_error(400, "Invalid Content-Length")
            return None
        if content_length > MAX_BODY_SIZE:
            self._send_error(413, "Request body too large")
            return None
        if content_length > 0:
            return self.rfile.read(content_length)
        if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
            return self._read_chunked()
        return b""

    def _handle_git_request(self, method, path, query, body, container, owner, repo):
        """Handle git smart HTTP requests (always allowed for valid tokens)."""
        error = validate_path(path, query, owner, repo)
        if error:
            self._send_error(403, error)
            logger.info("BLOCKED %s %s container=%s reason=%s", method, path, container, error)
            return

        upstream_url = _build_github_url(path, query)
        self._forward_to_github(
            method, upstream_url, body, container, path, host=GITHUB_HOST, follow_redirects=False
        )

    def _handle_api_request(self, method, path, query, body, container, owner, repo, rest_api):
        """Handle REST API requests (requires rest_api=True)."""
        if not rest_api:
            self._send_error(403, "REST API access not enabled")
            logger.info("BLOCKED %s %s container=%s reason=rest_disabled", method, path, container)
            return

        error = validate_api_path(path, query, method, owner, repo)
        if error:
            self._send_error(403, error)
            logger.info("BLOCKED %s %s container=%s reason=%s", method, path, container, error)
            return

        upstream_url = _build_api_url(path, query)
        # Follow redirects for GET (e.g. CI log downloads return 302)
        self._forward_to_github(
            method,
            upstream_url,
            body,
            container,
            path,
            host=GITHUB_API_HOST,
            follow_redirects=(method in ("GET", "HEAD")),
        )

    def _handle_graphql_request(self, body, container, owner, repo, graphql_read, graphql_write):
        """Handle GraphQL requests with policy-based access control.

        graphql_read/graphql_write: "whitelisted", "unrestricted", or "none".
        """
        if graphql_read == "none" and graphql_write == "none":
            self._send_error(403, "GraphQL access not enabled")
            logger.info("BLOCKED POST /graphql container=%s reason=graphql_disabled", container)
            return

        if not body:
            self._send_error(400, "Missing request body for GraphQL")
            return

        # For unrestricted mode, use the simpler classify_graphql path
        if graphql_read == "unrestricted" and graphql_write == "unrestricted":
            op_type, error = classify_graphql(body)
            if error:
                self._send_error(400, f"GraphQL validation failed: {error}")
                logger.info("BLOCKED POST /graphql container=%s reason=%s", container, error)
                return
            # Both unrestricted: allow any query or mutation
            upstream_url = f"{GITHUB_API_URL}/graphql"
            self._forward_to_github(
                "POST", upstream_url, body, container, "/graphql", host=GITHUB_API_HOST
            )
            return

        if graphql_read == "unrestricted" and graphql_write == "none":
            # Legacy level 3 behavior: unrestricted reads, no writes
            op_type, error = classify_graphql(body)
            if error:
                self._send_error(400, f"GraphQL validation failed: {error}")
                logger.info("BLOCKED POST /graphql container=%s reason=%s", container, error)
                return
            if op_type == "mutation":
                self._send_error(403, "Mutations not allowed at this access level")
                logger.info(
                    "BLOCKED POST /graphql container=%s reason=mutation_rejected", container
                )
                return
            upstream_url = f"{GITHUB_API_URL}/graphql"
            self._forward_to_github(
                "POST", upstream_url, body, container, "/graphql", host=GITHUB_API_HOST
            )
            return

        # Whitelisted mode: full structural + semantic validation
        from .graphql_validator import (
            _count_operations,
            _tokenize,
            parse_graphql,
            validate_read,
            validate_structure,
            validate_write,
        )

        parsed, error = parse_graphql(body)
        if error:
            self._send_error(400, f"GraphQL validation failed: {error}")
            logger.info("BLOCKED POST /graphql container=%s reason=%s", container, error)
            return

        # Count operations for structural validation
        tokens = _tokenize(parsed.query_str)
        op_count = _count_operations(tokens)

        # Structural validation
        error = validate_structure(parsed, op_count=op_count)
        if error:
            self._send_error(403, f"GraphQL structural validation failed: {error}")
            logger.info("BLOCKED POST /graphql container=%s reason=structural_%s", container, error)
            return

        if parsed.op_type == "mutation":
            if graphql_write == "none":
                self._send_error(403, "Mutations not allowed")
                logger.info(
                    "BLOCKED POST /graphql container=%s reason=mutation_rejected", container
                )
                return

            if graphql_write == "whitelisted":
                error = validate_write(
                    parsed,
                    owner,
                    repo,
                    preflight_fn=self._preflight_check,
                    repo_node_id_fn=self._get_repo_node_id,
                )
                if error:
                    self._send_error(403, f"GraphQL mutation validation failed: {error}")
                    logger.info(
                        "BLOCKED POST /graphql container=%s mutation=%s reason=%s",
                        container,
                        parsed.top_level_fields[0].name if parsed.top_level_fields else "?",
                        error,
                    )
                    return
            # "unrestricted" write: structural validation already passed
        else:
            # Query
            if graphql_read == "none":
                self._send_error(403, "Queries not allowed")
                logger.info("BLOCKED POST /graphql container=%s reason=query_rejected", container)
                return

            if graphql_read == "whitelisted":
                error = validate_read(
                    parsed,
                    owner,
                    repo,
                    preflight_fn=self._preflight_check,
                )
                if error:
                    self._send_error(403, f"GraphQL read validation failed: {error}")
                    logger.info("BLOCKED POST /graphql container=%s reason=%s", container, error)
                    return
            # "unrestricted" read: structural validation already passed

        upstream_url = f"{GITHUB_API_URL}/graphql"
        self._forward_to_github(
            "POST", upstream_url, body, container, "/graphql", host=GITHUB_API_HOST
        )

    def _github_graphql_query(self, query: str, variables: dict) -> dict:
        """Make a GraphQL query to GitHub API. Returns parsed JSON response.

        Used for pre-flight ownership checks and repo node ID resolution.
        """
        github_token = self.token_refresher.token
        body = json.dumps({"query": query, "variables": variables}).encode()

        req = Request(f"{GITHUB_API_URL}/graphql", data=body, method="POST")
        req.add_header("Authorization", f"token {github_token}")
        req.add_header("Content-Type", "application/json")
        req.add_header("Host", GITHUB_API_HOST)

        ctx = ssl.create_default_context()
        opener = build_opener(ProxyHandler({}), HTTPSHandler(context=ctx))
        resp = opener.open(req, timeout=30)
        return json.loads(resp.read())

    def _get_repo_node_id(self, owner: str, repo: str) -> str | None:
        """Get the GitHub node ID for a repository. Cached."""
        key = (owner.lower(), repo.lower())
        with self._repo_node_id_lock:
            cached = self._repo_node_id_cache.get(key)
            if cached is not None:
                return cached

        from .graphql_validator import REPO_ID_QUERY

        try:
            data = self._github_graphql_query(REPO_ID_QUERY, {"owner": owner, "name": repo})
            node_id = data.get("data", {}).get("repository", {}).get("id")
            if node_id:
                with self._repo_node_id_lock:
                    self._repo_node_id_cache[key] = node_id
            return node_id
        except Exception:
            logger.info("PREFLIGHT repo_node_id failed for %s/%s", owner, repo)
            return None

    def _preflight_check(self, node_id: str) -> str | None:
        """Check which repo a node belongs to.

        Returns "owner/repo" string or None on failure.
        """
        from .graphql_validator import PREFLIGHT_QUERY, extract_repo_from_preflight

        try:
            data = self._github_graphql_query(PREFLIGHT_QUERY, {"id": node_id})
            return extract_repo_from_preflight(data)
        except Exception:
            logger.info("PREFLIGHT check failed for node %s", node_id)
            return None

    def _forward_to_github(
        self,
        method,
        upstream_url,
        body,
        container,
        log_path,
        host,
        follow_redirects=False,
        _retried=False,
    ):
        """Forward a validated request to GitHub and return the response."""
        github_token = self.token_refresher.token
        b64_cred = base64.b64encode(f"x-access-token:{github_token}".encode()).decode()

        def _redact(msg: str) -> str:
            """Strip both raw token and base64 credential from a message."""
            msg = msg.replace(github_token, "[REDACTED]")
            msg = msg.replace(b64_cred, "[REDACTED]")
            return msg

        req = Request(upstream_url, data=body, method=method)

        # Copy relevant headers, strip auth/proxy headers
        for header, value in self.headers.items():
            lower = header.lower()
            if lower in ("host", "x-bubble-token", "authorization", "connection"):
                continue
            req.add_header(header, value)

        # Add real authorization.
        # Git smart HTTP on github.com requires Basic auth for OAuth tokens;
        # the API (api.github.com) accepts "token <token>" directly.
        if host == GITHUB_HOST:
            req.add_header("Authorization", f"Basic {b64_cred}")
        else:
            req.add_header("Authorization", f"token {github_token}")
        req.add_header("Host", host)

        # Forward to GitHub — pinned TLS, no proxy, no redirects
        ctx = ssl.create_default_context()
        opener = build_opener(
            ProxyHandler({}),
            HTTPSHandler(context=ctx),
            _NoRedirectHandler(),
        )
        try:
            resp = opener.open(req, timeout=300)
        except HTTPError as e:
            if 300 <= e.code < 400:
                # For API GET requests, follow redirects through the proxy
                # (e.g. CI log downloads return 302 to blob storage)
                location = e.headers.get("Location")
                if follow_redirects and location and method in ("GET", "HEAD"):
                    self._handle_redirect(location, container, log_path, method)
                    return
                # For git or non-followable redirects, return as-is
                self.send_response(e.code)
                for header, value in e.headers.items():
                    lower = header.lower()
                    if lower in ("transfer-encoding", "connection", "keep-alive"):
                        continue
                    self.send_header(header, value)
                self.end_headers()
                body_data = e.read()
                if body_data:
                    self.wfile.write(body_data)
                logger.info(
                    "REDIRECT %s %s container=%s -> %d",
                    method,
                    log_path,
                    container,
                    e.code,
                )
                return
            # On 401 from GitHub, the OAuth token may have expired.
            # Refresh via `gh auth token` (which triggers OAuth refresh)
            # and retry the request once.
            if e.code == 401 and not _retried:
                e.read()  # drain response body before retry
                new_token = self.token_refresher.refresh()
                if new_token != github_token:
                    logger.info(
                        "RETRY %s %s container=%s after token refresh",
                        method,
                        log_path,
                        container,
                    )
                    self._forward_to_github(
                        method,
                        upstream_url,
                        body,
                        container,
                        log_path,
                        host,
                        follow_redirects,
                        _retried=True,
                    )
                    return
            # Pass through 4xx errors from GitHub (auth failures, not found, etc.)
            if 400 <= e.code < 500:
                self.send_response(e.code)
                for header, value in e.headers.items():
                    lower = header.lower()
                    if lower in ("transfer-encoding", "connection", "keep-alive"):
                        continue
                    # Strip GitHub's auth-related headers
                    if lower == "authorization":
                        continue
                    self.send_header(header, value)
                self.end_headers()
                body_data = e.read()
                if body_data:
                    self.wfile.write(body_data)
                logger.info(
                    "UPSTREAM_ERROR %s %s container=%s -> %d",
                    method,
                    log_path,
                    container,
                    e.code,
                )
                return
            error_msg = _redact(str(e))
            self._send_error(502, f"Upstream error: {error_msg}")
            logger.info(
                "UPSTREAM_ERROR %s %s container=%s error=%s",
                method,
                log_path,
                container,
                error_msg,
            )
            return
        except Exception as e:
            error_msg = _redact(str(e))
            self._send_error(502, f"Upstream error: {error_msg}")
            logger.info(
                "UPSTREAM_ERROR %s %s container=%s error=%s",
                method,
                log_path,
                container,
                e,
            )
            return

        # Send response back to client
        self.send_response(resp.status)
        for header, value in resp.getheaders():
            lower = header.lower()
            if lower in ("transfer-encoding", "connection", "keep-alive"):
                continue
            self.send_header(header, value)
        self.end_headers()

        # Stream response body
        while True:
            chunk = resp.read(65536)
            if not chunk:
                break
            self.wfile.write(chunk)

        logger.info("PROXY %s %s container=%s -> %d", method, log_path, container, resp.status)

    def _handle_redirect(self, location, container, log_path, method="GET"):
        """Follow a redirect from a GitHub API response with hardened rules."""
        current_token = self.token_refresher.token
        b64_cred = base64.b64encode(f"x-access-token:{current_token}".encode()).decode()

        def _redact_redirect(msg: str) -> str:
            msg = msg.replace(current_token, "[REDACTED]")
            msg = msg.replace(b64_cred, "[REDACTED]")
            return msg

        try:
            status, headers, body = _follow_redirect(location, MAX_REDIRECT_HOPS, method=method)
        except (ValueError, HTTPError) as e:
            error_msg = _redact_redirect(str(e))
            self._send_error(502, f"Redirect error: {error_msg}")
            logger.info("REDIRECT_ERROR %s container=%s error=%s", log_path, container, error_msg)
            return
        except Exception as e:
            error_msg = _redact_redirect(str(e))
            self._send_error(502, f"Redirect error: {error_msg}")
            logger.info("REDIRECT_ERROR %s container=%s error=%s", log_path, container, error_msg)
            return

        self.send_response(status)
        for header, value in headers.items():
            lower = header.lower()
            if lower in ("transfer-encoding", "connection", "keep-alive"):
                continue
            self.send_header(header, value)
        self.end_headers()
        if body:
            self.wfile.write(body)

        logger.info(
            "REDIRECT_FOLLOWED %s container=%s -> %d (%d bytes)",
            log_path,
            container,
            status,
            len(body),
        )

    def _read_chunked(self) -> bytes | None:
        """Read a chunked transfer-encoded body."""
        parts = []
        total = 0
        while True:
            line = self.rfile.readline(128)
            if not line:
                break
            try:
                size = int(line.strip().split(b";")[0], 16)
            except (ValueError, IndexError):
                self._send_error(400, "Invalid chunk size")
                return None
            if size == 0:
                self.rfile.readline()  # trailing CRLF
                break
            if total + size > MAX_BODY_SIZE:
                self._send_error(413, "Request body too large")
                return None
            parts.append(self.rfile.read(size))
            total += size
            self.rfile.readline()  # trailing CRLF
        return b"".join(parts)

    def do_GET(self):
        self._proxy_request("GET")

    def do_HEAD(self):
        self._proxy_request("HEAD")

    def do_POST(self):
        self._proxy_request("POST")

    def do_PUT(self):
        self._proxy_request("PUT")

    def do_PATCH(self):
        self._proxy_request("PATCH")

    def do_DELETE(self):
        self._proxy_request("DELETE")


class ThreadedHTTPServer(HTTPServer):
    """HTTPServer that handles each request in a bounded thread pool.

    Enforces MAX_CONCURRENT_HANDLERS to prevent thread exhaustion from
    concurrent or slow connections. Excess connections are rejected
    immediately.
    """

    allow_reuse_address = True
    daemon_threads = True
    request_queue_size = MAX_CONCURRENT_HANDLERS

    def __init__(self, *args, **kwargs):
        self._handler_semaphore = threading.Semaphore(MAX_CONCURRENT_HANDLERS)
        super().__init__(*args, **kwargs)

    def process_request(self, request, client_address):
        if not self._handler_semaphore.acquire(blocking=False):
            # All handler slots busy — reject immediately
            try:
                request.close()
            except Exception:
                pass
            return
        t = threading.Thread(target=self._handle_request_thread, args=(request, client_address))
        t.daemon = True
        t.start()

    def _handle_request_thread(self, request, client_address):
        try:
            self.finish_request(request, client_address)
        except Exception:
            self.handle_error(request, client_address)
        finally:
            self.shutdown_request(request)
            self._handler_semaphore.release()


# ---------------------------------------------------------------------------
# Daemon
# ---------------------------------------------------------------------------


def _setup_logging():
    """Configure auth proxy logging to ~/.bubble/auth-proxy.log."""
    setup_file_logging(logger, AUTH_PROXY_LOG)


def _get_github_token() -> str:
    """Get the host's GitHub token for proxy use.

    Runs ``gh auth status`` first to trigger an OAuth token refresh if
    the stored access token has expired, then reads the (now-current)
    token via ``gh auth token``.
    """
    import subprocess

    # Trigger OAuth refresh (gh auth token alone returns the stale token)
    try:
        subprocess.run(
            ["gh", "auth", "status"],
            capture_output=True,
            timeout=15,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass  # Best-effort; get_host_gh_token may still succeed

    from .github_token import get_host_gh_token

    token = get_host_gh_token()
    if not token:
        raise RuntimeError("No GitHub token available. Run 'gh auth login' first.")
    return token


def run_daemon(port: int = 0):
    """Run the auth proxy daemon.

    Listens on TCP (both macOS and Linux — HTTP needs TCP).

    Args:
        port: Port to listen on. 0 means use config or default.
    """
    _setup_logging()
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    if not port:
        from .config import load_config

        config = load_config()
        port = config.get("auth_proxy", {}).get("port", DEFAULT_PORT)

    # Get GitHub token (refreshed automatically on 401)
    github_token = _get_github_token()
    token_refresher = GitHubTokenRefresher(github_token)

    token_registry = AuthTokenRegistry()
    rate_limiter = ProxyRateLimiter()

    # Configure the handler class
    AuthProxyHandler.token_registry = token_registry
    AuthProxyHandler.rate_limiter = rate_limiter
    AuthProxyHandler.token_refresher = token_refresher

    # On macOS, Incus runs inside a Colima VM.  Bind to the VMNet bridge
    # IP (e.g. 192.168.64.1) so the VM can reach us without exposing the
    # service to the wider LAN.  Falls back to 0.0.0.0 if no bridge found.
    import platform

    if platform.system() == "Darwin":
        from .runtime.colima import colima_bind_ip

        bind_addr = colima_bind_ip()
    else:
        bind_addr = "127.0.0.1"
    server = ThreadedHTTPServer((bind_addr, port), AuthProxyHandler)

    AUTH_PROXY_PORT_FILE.write_text(str(port))
    os.chmod(str(AUTH_PROXY_PORT_FILE), 0o600)

    logger.info("Auth proxy daemon started on %s:%d", bind_addr, port)
    print(f"Auth proxy listening on {bind_addr}:{port}")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Auth proxy daemon stopped")
    finally:
        server.shutdown()
        AUTH_PROXY_PORT_FILE.unlink(missing_ok=True)
