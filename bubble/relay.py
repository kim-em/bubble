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
import socket
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from .config import DATA_DIR
from .git_store import repo_is_known
from .repo_registry import RepoRegistry
from .target import TargetParseError, parse_target
from .token_store import RateLimiter as _RateLimiter
from .token_store import RateWindow, TokenStore, setup_file_logging

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

    Returns the token string. The token->container mapping is stored in
    ~/.bubble/relay-tokens.json. Uses file locking to prevent races
    when multiple bubbles are created concurrently.
    """
    return TokenStore(RELAY_TOKENS).generate(container_name)


def _load_tokens() -> dict[str, str]:
    """Load token->container_name mapping from disk."""
    return TokenStore(RELAY_TOKENS)._load()


def remove_relay_token(container_name: str):
    """Remove all tokens for a container (e.g. on pop)."""
    TokenStore(RELAY_TOKENS).remove(lambda v: v == container_name)


class TokenRegistry:
    """Thread-safe token->container lookup with file-based persistence.

    Caches the tokens file and reloads when the mtime changes.
    """

    def __init__(self):
        self._store = TokenStore(RELAY_TOKENS)

    def lookup(self, token: str) -> str | None:
        """Look up a token. Returns container name or None."""
        return self._store.lookup(token)


class RateLimiter(_RateLimiter):
    """Per-container rate limiter with sliding windows.

    Limits: 3/minute, 10/10 minutes, 20/hour per container.
    Also enforces a global limit across all containers.
    Caps the number of tracked container IDs to prevent memory exhaustion.
    """

    def __init__(self):
        super().__init__(
            windows=[RateWindow(60, 3), RateWindow(600, 10), RateWindow(3600, 20)],
            global_per_hour=GLOBAL_RATE_LIMIT_PER_HOUR,
            max_tracked=MAX_TRACKED_CONTAINERS,
        )


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
    setup_file_logging(logger, RELAY_LOG)


def _handle_connection(
    conn: socket.socket,
    rate_limiter: RateLimiter,
    token_registry: TokenRegistry | None = None,
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

        try:
            _open_bubble(target)
            _send_response(conn, "ok", f"Opening bubble for '{target}'...")
        except (OSError, subprocess.SubprocessError) as e:
            _send_response(conn, "error", f"Failed to open bubble: {e}")
            logger.info("ERROR  container=%s  target=%s  %s", log_container, log_target, e)

    except socket.timeout:
        try:
            _send_response(conn, "error", "Request timed out.")
        except OSError:
            pass
        logger.info("REJECT  timeout")
    except (OSError, ValueError, json.JSONDecodeError) as e:
        try:
            _send_response(conn, "error", "Request failed.")
        except OSError:
            pass
        logger.info("ERROR  %s: %s", type(e).__name__, e)
    finally:
        try:
            time.sleep(0.1)  # Allow proxy to flush response
            conn.close()
        except OSError:
            pass


def _send_response(conn: socket.socket, status: str, message: str):
    """Send a JSON response and close the write end."""
    response = json.dumps({"status": status, "message": message})
    conn.sendall(response.encode("utf-8") + b"\n")
    try:
        conn.shutdown(socket.SHUT_WR)
    except OSError:
        pass


def _open_bubble(target: str):
    """Open a bubble for the given target string.

    Uses subprocess to invoke the bubble CLI so we get the full open_cmd
    logic including VSCode, hooks, etc. Passes --no-clone to prevent
    cloning repos that don't exist in the git store (TOCTOU protection).
    """
    subprocess.Popen(
        ["bubble", "open", "--local", "--no-clone", "--no-interactive", target],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _guarded_handle(semaphore, conn, rate_limiter, token_registry):
    """Wrapper that releases the handler semaphore after connection handling."""
    try:
        _handle_connection(conn, rate_limiter, token_registry)
    finally:
        semaphore.release()


def run_daemon():
    """Run the relay daemon.

    On macOS: listens on TCP (Unix sockets can't traverse Colima's virtio-fs).
    On Linux: listens on a Unix socket.
    """
    _setup_logging()

    DATA_DIR.mkdir(parents=True, exist_ok=True)

    use_tcp = platform.system() == "Darwin"

    if use_tcp:
        from .config import load_config

        config = load_config()
        port = config.get("relay", {}).get("port", 7653)
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        # On macOS, Incus runs inside a Colima VM that reaches the host
        # via a bridge IP (not 127.0.0.1).  We must bind to 0.0.0.0
        # because the bridge IP is not a local address on the host.
        # Token auth prevents unauthorized access.
        bind_addr = "0.0.0.0"
        server.bind((bind_addr, port))
        server.listen(5)
        RELAY_PORT_FILE.write_text(str(port))
        os.chmod(str(RELAY_PORT_FILE), 0o600)
        listen_addr = f"{bind_addr}:{port}"
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
                except OSError:
                    pass
                continue
            executor.submit(
                _guarded_handle,
                handler_semaphore,
                conn,
                rate_limiter,
                token_registry,
            )
    except KeyboardInterrupt:
        logger.info("Relay daemon stopped")
    finally:
        executor.shutdown(wait=False)
        server.close()
        if not use_tcp:
            RELAY_SOCK.unlink(missing_ok=True)
