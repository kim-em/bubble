"""HTTP reverse proxy for repo-scoped GitHub authentication.

Keeps the host's GitHub token on the host side. Containers use
git's `url.insteadOf` to route HTTPS requests through this proxy,
which validates the request targets the allowed repository, then
adds the real Authorization header before forwarding to GitHub.

The host GitHub token never enters the container. Each container
gets a per-container bearer token that only works against this
proxy and is scoped to a single repository.

Security model:
- Strict 4-pattern allowlist (git smart HTTP protocol only)
- Path canonicalization rejects encoded separators, dot-segments,
  duplicate slashes
- No redirect following (returns redirects as-is)
- Pinned outbound to github.com:443 with TLS verification
- Ignores ambient HTTPS_PROXY/ALL_PROXY to prevent token leakage
- Per-container token isolation via X-Bubble-Token header
- Rate limited + logged (reuses relay patterns)

On macOS (Colima): TCP listener, port saved to ~/.bubble/auth-proxy.port.
On Linux: TCP listener on 127.0.0.1 (Incus proxy needs TCP for HTTP).
"""

import fcntl
import json
import logging
import os
import re
import secrets
import ssl
import threading
import time
from collections import deque
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

# Allowed query strings
_ALLOWED_QUERIES = {
    "info/refs": {"service=git-upload-pack", "service=git-receive-pack"},
    "git-upload-pack": set(),
    "git-receive-pack": set(),
}

# GitHub API host
GITHUB_HOST = "github.com"
GITHUB_URL = f"https://{GITHUB_HOST}"

logger = logging.getLogger("bubble.auth_proxy")


# ---------------------------------------------------------------------------
# Token management (same pattern as relay tokens)
# ---------------------------------------------------------------------------


def _token_lock():
    """Acquire an exclusive file lock for the token registry."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    lock_path = AUTH_PROXY_TOKENS.with_suffix(".lock")
    fd = lock_path.open("w")
    fcntl.flock(fd, fcntl.LOCK_EX)
    return fd


def _token_unlock(fd):
    """Release the token registry file lock."""
    fcntl.flock(fd, fcntl.LOCK_UN)
    fd.close()


def generate_auth_token(container_name: str, owner: str, repo: str) -> str:
    """Generate an auth proxy token for a container.

    The token maps to (container_name, owner, repo) — the proxy uses
    this to validate that requests target the correct repository.
    Uses file locking to prevent read-modify-write races.
    """
    fd = _token_lock()
    try:
        token = secrets.token_hex(32)
        tokens = _load_tokens()
        tokens[token] = {"container": container_name, "owner": owner, "repo": repo}
        _save_tokens(tokens)
        return token
    finally:
        _token_unlock(fd)


def remove_auth_tokens(container_name: str):
    """Remove all auth proxy tokens for a container (e.g. on pop)."""
    fd = _token_lock()
    try:
        tokens = _load_tokens()
        tokens = {t: v for t, v in tokens.items() if v.get("container") != container_name}
        _save_tokens(tokens)
    finally:
        _token_unlock(fd)


def _load_tokens() -> dict:
    """Load token registry from disk. Caller must hold the lock for writes."""
    if AUTH_PROXY_TOKENS.exists():
        try:
            return json.loads(AUTH_PROXY_TOKENS.read_text())
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _save_tokens(tokens: dict):
    """Save token registry to disk."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = AUTH_PROXY_TOKENS.with_suffix(".tmp")
    tmp.write_text(json.dumps(tokens))
    tmp.replace(AUTH_PROXY_TOKENS)
    os.chmod(str(AUTH_PROXY_TOKENS), 0o600)


class AuthTokenRegistry:
    """Thread-safe token lookup with file-based persistence.

    Caches the tokens file and reloads when the mtime changes.
    """

    def __init__(self):
        self._tokens: dict = {}
        self._mtime: float = 0
        self._lock = threading.Lock()

    def lookup(self, token: str) -> dict | None:
        """Look up a token. Returns {container, owner, repo} or None."""
        with self._lock:
            self._maybe_reload()
            return self._tokens.get(token)

    def _maybe_reload(self):
        try:
            st = AUTH_PROXY_TOKENS.stat()
            if st.st_mtime != self._mtime:
                self._tokens = _load_tokens()
                self._mtime = st.st_mtime
        except FileNotFoundError:
            self._tokens = {}
            self._mtime = 0


class ProxyRateLimiter:
    """Per-container rate limiter for the auth proxy.

    More generous than the relay (git does many requests per operation).
    """

    def __init__(self):
        self._requests: dict[str, deque] = {}
        self._lock = threading.Lock()

    def check(self, container: str) -> bool:
        """Check if a request is allowed. Records it if so."""
        now = time.time()
        with self._lock:
            # Evict oldest container if too many tracked
            if container not in self._requests and len(self._requests) >= MAX_TRACKED_CONTAINERS:
                oldest_key = min(
                    self._requests,
                    key=lambda k: self._requests[k][-1] if self._requests[k] else 0,
                )
                del self._requests[oldest_key]

            q = self._requests.setdefault(container, deque())
            # Prune entries older than 1 hour
            while q and q[0] < now - 3600:
                q.popleft()
            # Check windows
            last_60 = sum(1 for t in q if t > now - 60)
            if last_60 >= RATE_LIMIT_PER_MINUTE or len(q) >= RATE_LIMIT_PER_HOUR:
                return False
            q.append(now)
            return True


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
    """Build the upstream GitHub URL from a validated request path.

    Strips the /git/ prefix and constructs the full GitHub URL.
    """
    # Remove /git/ prefix
    github_path = path[4:]  # "/git/owner/repo/..." -> "/owner/repo/..."
    url = f"{GITHUB_URL}{github_path}"
    if query:
        url += f"?{query}"
    return url


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


class AuthProxyHandler(BaseHTTPRequestHandler):
    """HTTP request handler for the git auth proxy."""

    # Per-socket read timeout (seconds) — prevents slowloris attacks
    timeout = 60

    # Class-level references set by the server
    token_registry: AuthTokenRegistry
    rate_limiter: ProxyRateLimiter
    github_token: str

    def log_message(self, format, *args):
        """Route HTTP server logs to our logger."""
        logger.info(format, *args)

    def _get_container_token(self) -> str | None:
        """Extract the X-Bubble-Token header value."""
        return self.headers.get("X-Bubble-Token")

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
        body = message.encode("utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _proxy_request(self, method: str):
        """Core proxy logic shared by GET and POST."""
        # Authenticate
        info = self._authenticate()
        if not info:
            return

        container = info["container"]
        owner = info["owner"]
        repo = info["repo"]

        # Rate limit
        if not self.rate_limiter.check(container):
            self._send_error(429, "Rate limited")
            logger.info("RATE_LIMITED container=%s", container)
            return

        # Parse and validate path
        parsed = urlparse(self.path)
        path = parsed.path
        query = parsed.query

        error = validate_path(path, query, owner, repo)
        if error:
            self._send_error(403, error)
            logger.info("BLOCKED %s %s container=%s reason=%s", method, path, container, error)
            return

        # Build upstream URL
        upstream_url = _build_github_url(path, query)

        # Read request body for POST
        body = None
        if method == "POST":
            try:
                content_length = int(self.headers.get("Content-Length", 0))
            except (ValueError, TypeError):
                self._send_error(400, "Invalid Content-Length")
                return
            if content_length > MAX_BODY_SIZE:
                self._send_error(413, "Request body too large")
                return
            if content_length > 0:
                body = self.rfile.read(content_length)
            elif self.headers.get("Transfer-Encoding", "").lower() == "chunked":
                # Read chunked body
                body = self._read_chunked()
                if body is None:
                    return  # Error already sent

        # Build upstream request
        req = Request(upstream_url, data=body, method=method)

        # Copy relevant headers, strip X-Bubble-Token
        for header, value in self.headers.items():
            lower = header.lower()
            if lower in ("host", "x-bubble-token", "authorization", "connection"):
                continue
            req.add_header(header, value)

        # Add real authorization
        req.add_header("Authorization", f"token {self.github_token}")
        req.add_header("Host", GITHUB_HOST)

        # Forward to GitHub — pinned TLS, no proxy, no redirects
        # ProxyHandler({}) disables ambient HTTPS_PROXY/ALL_PROXY env vars.
        # _NoRedirectHandler prevents redirect-following that could leak
        # the Authorization header to non-GitHub hosts.
        ctx = ssl.create_default_context()
        opener = build_opener(
            ProxyHandler({}),
            HTTPSHandler(context=ctx),
            _NoRedirectHandler(),
        )
        try:
            resp = opener.open(req, timeout=300)
        except HTTPError as e:
            # 3xx redirects arrive here as HTTPError — return them as-is
            if 300 <= e.code < 400:
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
                    path,
                    container,
                    e.code,
                )
                return
            error_msg = str(e)
            if self.github_token in error_msg:
                error_msg = error_msg.replace(self.github_token, "[REDACTED]")
            self._send_error(502, f"Upstream error: {error_msg}")
            logger.info(
                "UPSTREAM_ERROR %s %s container=%s error=%s",
                method,
                path,
                container,
                e,
            )
            return
        except Exception as e:
            error_msg = str(e)
            # Don't leak the token in error messages
            if self.github_token in error_msg:
                error_msg = error_msg.replace(self.github_token, "[REDACTED]")
            self._send_error(502, f"Upstream error: {error_msg}")
            logger.info("UPSTREAM_ERROR %s %s container=%s error=%s", method, path, container, e)
            return

        # Send response back to client
        self.send_response(resp.status)
        for header, value in resp.getheaders():
            lower = header.lower()
            # Skip hop-by-hop headers
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

        logger.info("PROXY %s %s container=%s -> %d", method, path, container, resp.status)

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

    def do_POST(self):
        self._proxy_request("POST")


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
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(str(AUTH_PROXY_LOG))
    handler.setFormatter(logging.Formatter("%(asctime)s  %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)


def _get_github_token() -> str:
    """Get the host's GitHub token for proxy use."""
    from .github_token import get_host_gh_token

    token = get_host_gh_token()
    if not token:
        raise RuntimeError("No GitHub token available. Run 'gh auth login' first.")
    return token


def run_daemon():
    """Run the auth proxy daemon.

    Listens on TCP (both macOS and Linux — HTTP needs TCP).
    """
    _setup_logging()
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    from .config import load_config

    config = load_config()
    port = config.get("auth_proxy", {}).get("port", DEFAULT_PORT)

    # Get GitHub token
    github_token = _get_github_token()

    token_registry = AuthTokenRegistry()
    rate_limiter = ProxyRateLimiter()

    # Configure the handler class
    AuthProxyHandler.token_registry = token_registry
    AuthProxyHandler.rate_limiter = rate_limiter
    AuthProxyHandler.github_token = github_token

    server = ThreadedHTTPServer(("127.0.0.1", port), AuthProxyHandler)

    AUTH_PROXY_PORT_FILE.write_text(str(port))
    os.chmod(str(AUTH_PROXY_PORT_FILE), 0o600)

    logger.info("Auth proxy daemon started on 127.0.0.1:%d", port)
    print(f"Auth proxy listening on 127.0.0.1:{port}")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Auth proxy daemon stopped")
    finally:
        server.shutdown()
        AUTH_PROXY_PORT_FILE.unlink(missing_ok=True)
