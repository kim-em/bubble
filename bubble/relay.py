"""Bubble-in-bubble relay daemon.

Listens for requests from inside containers to open new bubbles.

On macOS (Colima): listens on TCP because Unix sockets can't traverse
the virtio-fs mount between macOS and the Colima VM. The port is saved
to ~/.bubble/relay.port for the incus proxy device to connect to.

On Linux: listens on a Unix socket at ~/.bubble/relay.sock.

Security model:
- Only repos already cloned in ~/.bubble/git/ are allowed (no new clones)
- Local paths are rejected (no filesystem access from containers)
- Rate limited: per-container (3/min, 10/10min, 20/hr) + global (30/hr)
- All requests logged to ~/.bubble/relay.log
- Path traversal in owner/repo names is rejected
- Token-based container authentication (prevents container ID spoofing)
"""

import json
import logging
import os
import platform
import re
import secrets
import socket
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor

from .config import DATA_DIR
from .git_store import repo_is_known
from .repo_registry import RepoRegistry
from .target import TargetParseError, parse_target

RELAY_SOCK = DATA_DIR / "relay.sock"
RELAY_PORT_FILE = DATA_DIR / "relay.port"
RELAY_LOG = DATA_DIR / "relay.log"
RELAY_TOKENS = DATA_DIR / "relay-tokens.json"

# Maximum request size (bytes)
MAX_REQUEST_SIZE = 1024

# Maximum target string length
MAX_TARGET_LENGTH = 500

# Maximum concurrent handler threads
MAX_CONCURRENT_HANDLERS = 4

# Maximum distinct container IDs tracked before eviction
MAX_TRACKED_CONTAINERS = 100

# Global rate limit (all containers combined)
GLOBAL_RATE_LIMIT_PER_HOUR = 30

# Valid GitHub owner/repo name pattern
_GITHUB_NAME_RE = re.compile(r"^[a-zA-Z0-9._-]+$")

logger = logging.getLogger("bubble.relay")


def _sanitize_for_log(s: str) -> str:
    """Replace control characters for safe logging."""
    return s.replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")


def generate_relay_token(container_name: str) -> str:
    """Generate a relay token for a container and persist it.

    Returns the token string. The token→container mapping is stored in
    ~/.bubble/relay-tokens.json.
    """
    token = secrets.token_hex(32)
    tokens = _load_tokens()
    tokens[token] = container_name
    _save_tokens(tokens)
    return token


def _load_tokens() -> dict[str, str]:
    """Load token→container_name mapping from disk."""
    if RELAY_TOKENS.exists():
        try:
            return json.loads(RELAY_TOKENS.read_text())
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _save_tokens(tokens: dict[str, str]):
    """Save token→container_name mapping to disk."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = RELAY_TOKENS.with_suffix(".tmp")
    tmp.write_text(json.dumps(tokens))
    tmp.replace(RELAY_TOKENS)
    # Owner-only permissions — tokens are secrets
    os.chmod(str(RELAY_TOKENS), 0o600)


def remove_relay_token(container_name: str):
    """Remove all tokens for a container (e.g. on destroy)."""
    tokens = _load_tokens()
    tokens = {t: c for t, c in tokens.items() if c != container_name}
    _save_tokens(tokens)


class TokenRegistry:
    """Thread-safe token→container lookup with file-based persistence.

    Caches the tokens file and reloads when the mtime changes.
    """

    def __init__(self):
        self._tokens: dict[str, str] = {}
        self._mtime: float = 0
        self._lock = threading.Lock()

    def lookup(self, token: str) -> str | None:
        """Look up a token. Returns container name or None."""
        with self._lock:
            self._maybe_reload()
            return self._tokens.get(token)

    def _maybe_reload(self):
        try:
            st = RELAY_TOKENS.stat()
            if st.st_mtime != self._mtime:
                self._tokens = _load_tokens()
                self._mtime = st.st_mtime
        except FileNotFoundError:
            self._tokens = {}
            self._mtime = 0


class RateLimiter:
    """Per-container rate limiter with sliding windows.

    Limits: 3/minute, 10/10 minutes, 20/hour per container.
    Also enforces a global limit across all containers.
    Caps the number of tracked container IDs to prevent memory exhaustion.
    """

    def __init__(self):
        self._requests: dict[str, deque] = {}  # container → timestamps
        self._global: deque = deque()  # all timestamps
        self._lock = threading.Lock()

    def check(self, container: str) -> bool:
        """Check if a request is allowed. Records it if so."""
        now = time.time()
        with self._lock:
            # Prune global entries
            while self._global and self._global[0] < now - 3600:
                self._global.popleft()

            # Global rate limit
            if len(self._global) >= GLOBAL_RATE_LIMIT_PER_HOUR:
                return False

            # Evict oldest container if too many tracked
            if container not in self._requests and len(self._requests) >= MAX_TRACKED_CONTAINERS:
                oldest_key = min(
                    self._requests, key=lambda k: self._requests[k][-1] if self._requests[k] else 0
                )
                del self._requests[oldest_key]

            q = self._requests.setdefault(container, deque())
            # Prune entries older than 1 hour
            while q and q[0] < now - 3600:
                q.popleft()
            # Check windows
            last_60 = sum(1 for t in q if t > now - 60)
            last_600 = sum(1 for t in q if t > now - 600)
            if last_60 >= 3 or last_600 >= 10 or len(q) >= 20:
                return False
            q.append(now)
            self._global.append(now)
            return True


def validate_relay_target(target: str) -> tuple[str, str]:
    """Validate a target string from a relay request.

    Returns (status, message) where status is "ok" on success or an error
    status string otherwise.
    """
    if not target or not isinstance(target, str):
        return "error", "Empty target."

    if len(target) > MAX_TARGET_LENGTH:
        return "error", "Target too long."

    # Reject local paths — containers must not access the host filesystem
    if target.startswith((".", "/", "~")):
        return "error", "Local paths are not allowed via relay."

    # Reject targets starting with '-' to prevent CLI option injection
    if target.startswith("-"):
        return "error", "Invalid target."

    if "--path" in target:
        return "error", "The --path flag is not allowed via relay."

    # Reject shell metacharacters
    dangerous = set(";|&$`\\(){}[]!#")
    if any(c in dangerous for c in target):
        return "error", "Invalid characters in target."

    # Reject path traversal sequences anywhere in the target
    if ".." in target:
        return "error", "Path traversal is not allowed."

    # Try to parse the target
    registry = RepoRegistry()
    try:
        t = parse_target(target, registry)
    except TargetParseError as e:
        return "error", str(e)

    # Validate owner and repo names against GitHub naming rules
    if not _GITHUB_NAME_RE.match(t.owner):
        return "error", f"Invalid owner name: {t.owner!r}"
    if not _GITHUB_NAME_RE.match(t.repo):
        return "error", f"Invalid repo name: {t.repo!r}"

    # Check that the repo is already known (cloned in ~/.bubble/git/)
    if not repo_is_known(t.org_repo):
        return "unknown_repo", (
            f"Repo '{t.org_repo}' is not available. Open it outside of a bubble first."
        )

    return "ok", ""


def _setup_logging():
    """Configure relay request logging to ~/.bubble/relay.log."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(str(RELAY_LOG))
    handler.setFormatter(logging.Formatter("%(asctime)s  %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)


def _handle_connection(
    conn: socket.socket,
    rate_limiter: RateLimiter,
    token_registry: TokenRegistry | None = None,
    runtime_factory=None,
):
    """Handle a single relay connection."""
    try:
        conn.settimeout(5.0)
        data = conn.recv(MAX_REQUEST_SIZE)
        if not data:
            return

        # Parse JSON request
        try:
            request = json.loads(data.decode("utf-8", errors="replace"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            _send_response(conn, "error", "Invalid request format.")
            logger.info("REJECT  malformed JSON")
            return

        target = request.get("target", "")
        token = request.get("token", "")

        # Authenticate via token — ignore client-supplied container name
        container = "unknown"
        if token_registry and token:
            container = token_registry.lookup(str(token)[:128]) or ""
            if not container:
                _send_response(conn, "error", "Invalid relay token.")
                logger.info(
                    "REJECT  invalid_token  target=%s",
                    _sanitize_for_log(str(target)[:MAX_TARGET_LENGTH]),
                )
                return
        elif token_registry:
            # Token registry is active but no token provided
            _send_response(conn, "error", "Relay token required.")
            logger.info(
                "REJECT  missing_token  target=%s",
                _sanitize_for_log(str(target)[:MAX_TARGET_LENGTH]),
            )
            return

        # Sanitize for logging
        log_container = _sanitize_for_log(container[:64])
        log_target = _sanitize_for_log(str(target)[:MAX_TARGET_LENGTH])

        # Rate limit (keyed on authenticated container name)
        if not rate_limiter.check(container):
            _send_response(conn, "rate_limited", "Rate limited. Try again later.")
            logger.info("REJECT  rate_limited  container=%s  target=%s", log_container, log_target)
            return

        # Validate target
        status, message = validate_relay_target(target)
        if status != "ok":
            _send_response(conn, status, message)
            logger.info(
                "REJECT  %s  container=%s  target=%s  %s",
                status,
                log_container,
                log_target,
                message,
            )
            return

        # Success — open the bubble
        logger.info("ACCEPT  container=%s  target=%s", log_container, log_target)

        if runtime_factory:
            try:
                _open_bubble(target, runtime_factory)
                _send_response(conn, "ok", f"Opening bubble for '{target}'...")
            except Exception as e:
                _send_response(conn, "error", f"Failed to open bubble: {e}")
                logger.info("ERROR  container=%s  target=%s  %s", log_container, log_target, e)
        else:
            _send_response(conn, "ok", f"Opening bubble for '{target}'...")

    except socket.timeout:
        logger.info("REJECT  timeout")
    except Exception as e:
        try:
            _send_response(conn, "error", "Request failed.")
        except Exception:
            pass
        logger.info("ERROR  %s", e)
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _send_response(conn: socket.socket, status: str, message: str):
    """Send a JSON response and close the write end."""
    response = json.dumps({"status": status, "message": message})
    conn.sendall(response.encode("utf-8") + b"\n")


def _open_bubble(target: str, runtime_factory):
    """Open a bubble for the given target string.

    Uses subprocess to invoke the bubble CLI so we get the full open_cmd
    logic including VSCode, hooks, etc. Passes --no-clone to prevent
    cloning repos that don't exist in the git store (TOCTOU protection).
    """
    import subprocess

    subprocess.Popen(
        ["bubble", "open", "--no-clone", "--no-interactive", target],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _guarded_handle(semaphore, conn, rate_limiter, token_registry, runtime_factory):
    """Wrapper that releases the handler semaphore after connection handling."""
    try:
        _handle_connection(conn, rate_limiter, token_registry, runtime_factory)
    finally:
        semaphore.release()


def run_daemon(runtime_factory=None):
    """Run the relay daemon.

    On macOS: listens on TCP (Unix sockets can't traverse Colima's virtio-fs).
    On Linux: listens on a Unix socket.
    """
    _setup_logging()

    DATA_DIR.mkdir(parents=True, exist_ok=True)

    use_tcp = platform.system() == "Darwin"

    if use_tcp:
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(("127.0.0.1", 0))
        server.listen(5)
        port = server.getsockname()[1]
        RELAY_PORT_FILE.write_text(str(port))
        os.chmod(str(RELAY_PORT_FILE), 0o600)
        listen_addr = f"127.0.0.1:{port}"
    else:
        # Remove stale socket
        RELAY_SOCK.unlink(missing_ok=True)
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(RELAY_SOCK))
        server.listen(5)
        # Owner-only permissions
        RELAY_SOCK.chmod(0o600)
        listen_addr = str(RELAY_SOCK)

    rate_limiter = RateLimiter()
    token_registry = TokenRegistry()
    executor = ThreadPoolExecutor(max_workers=MAX_CONCURRENT_HANDLERS)
    # Pre-auth connection cap: reject new connections when all handler slots
    # are busy. Prevents unauthenticated DoS from blocking legitimate requests.
    handler_semaphore = threading.Semaphore(MAX_CONCURRENT_HANDLERS)

    logger.info("Relay daemon started on %s", listen_addr)
    print(f"Relay daemon listening on {listen_addr}")

    try:
        while True:
            conn, _ = server.accept()
            if not handler_semaphore.acquire(blocking=False):
                # All handler slots busy — drop the connection immediately
                try:
                    conn.close()
                except Exception:
                    pass
                continue
            executor.submit(
                _guarded_handle, handler_semaphore, conn,
                rate_limiter, token_registry, runtime_factory,
            )
    except KeyboardInterrupt:
        logger.info("Relay daemon stopped")
    finally:
        executor.shutdown(wait=False)
        server.close()
        if use_tcp:
            RELAY_PORT_FILE.unlink(missing_ok=True)
        else:
            RELAY_SOCK.unlink(missing_ok=True)
