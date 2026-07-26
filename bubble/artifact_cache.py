"""Host-global, capability-scoped, GET-only artifact cache.

The daemon is deliberately separate from Bubble's GitHub auth proxy: public
Lean caches must keep working when ``security.github=off`` or the host has no
GitHub credential.  Containers receive an unguessable URL capability whose
routes are fixed by the trusted host-side Bubble process.  A request cannot
choose an arbitrary upstream URL.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import os
import re
import shutil
import socket
import threading
import time
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import httpx

from . import __version__
from .config import HOST_DATA_DIR
from .token_store import TokenStore, setup_file_logging

DEFAULT_PORT = 7655
DEFAULT_MAX_BYTES = 50 * 1024**3
DEFAULT_MAX_OBJECT_BYTES = 4 * 1024**3
MAX_ERROR_BODY = 1024 * 1024

ARTIFACT_CACHE_DIR = HOST_DATA_DIR / "artifact-cache"
ARTIFACT_CACHE_OBJECTS = ARTIFACT_CACHE_DIR / "objects"
ARTIFACT_CACHE_TOKENS = HOST_DATA_DIR / "artifact-cache-tokens.json"
ARTIFACT_CACHE_ENDPOINT_FILE = HOST_DATA_DIR / "artifact-cache.endpoint"
ARTIFACT_CACHE_LOG = HOST_DATA_DIR / "artifact-cache.log"
ARTIFACT_CACHE_LOCK = HOST_DATA_DIR / "locks" / "artifact-cache.lock"

# Mathlib's Cache/Requests.lean gives MATHLIB_CACHE_GET_URL precedence, and
# Cache/Infra.lean defines this current-primary then legacy Azure chain.
MATHLIB_CACHE_ENDPOINTS = (
    "https://lakecache.blob.core.windows.net/mathlib4-master",
    "https://lakecache.blob.core.windows.net/mathlib4",
)

_SAFE_ROUTE_RE = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
_TOKEN_RE = re.compile(r"^[0-9a-f]{64}$")
_STRIPPED_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "date",
    "server",
}

logger = logging.getLogger("bubble.artifact_cache")


def load_host_artifact_config() -> dict:
    """Load host-global cache settings, independent of the caller's BUBBLE_HOME."""
    try:
        try:
            import tomllib
        except ModuleNotFoundError:  # Python 3.10
            import tomli as tomllib

        with open(HOST_DATA_DIR / "config.toml", "rb") as source:
            config = tomllib.load(source)
        section = config.get("artifact_cache", {})
        return section if isinstance(section, dict) else {}
    except (OSError, ValueError):
        return {}


@dataclass(frozen=True)
class LakeCacheService:
    """A named Lake download service supplied by the trusted Bubble caller."""

    name: str
    artifact_endpoint: str
    revision_endpoint: str


def validate_lake_service(
    name: str, artifact_endpoint: str, revision_endpoint: str
) -> LakeCacheService:
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9._-]{0,62}", name):
        raise ValueError(f"invalid Lake cache service name: {name!r}")
    return LakeCacheService(
        name=name,
        artifact_endpoint=normalize_endpoint(artifact_endpoint),
        revision_endpoint=normalize_endpoint(revision_endpoint),
    )


def normalize_endpoint(endpoint: str) -> str:
    """Validate and normalize a trusted upstream HTTPS base URL."""
    value = endpoint.strip().rstrip("/")
    parsed = urlparse(value)
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError(f"artifact cache endpoint has an invalid port: {endpoint}") from exc
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or port == 0
    ):
        raise ValueError(f"artifact cache endpoint must be a plain HTTPS base URL: {endpoint}")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise ValueError("artifact cache endpoint contains control characters")
    return value


def normalize_routes(routes: dict[str, list[str] | tuple[str, ...]]) -> dict[str, list[str]]:
    """Validate the immutable route table stored behind a capability token."""
    result: dict[str, list[str]] = {}
    for name, endpoints in routes.items():
        if not _SAFE_ROUTE_RE.fullmatch(name):
            raise ValueError(f"invalid artifact cache route name: {name!r}")
        normalized = list(dict.fromkeys(normalize_endpoint(url) for url in endpoints))
        if not normalized:
            raise ValueError(f"artifact cache route {name!r} has no upstream endpoints")
        result[name] = normalized
    if not result:
        raise ValueError("artifact cache capability must contain at least one route")
    return result


def generate_cache_token(
    container_name: str,
    routes: dict[str, list[str] | tuple[str, ...]],
    *,
    mathlib: bool = False,
    lake_service_names: tuple[str, ...] = (),
) -> str:
    """Issue a per-container capability for an exact set of upstream routes."""
    return TokenStore(ARTIFACT_CACHE_TOKENS).generate(
        {
            "container": container_name,
            "routes": normalize_routes(routes),
            "mathlib": mathlib,
            "lake_service_names": list(lake_service_names),
        }
    )


def remove_cache_tokens(container_name: str) -> None:
    """Revoke all artifact-cache capabilities belonging to a container."""
    TokenStore(ARTIFACT_CACHE_TOKENS).remove(lambda value: value.get("container") == container_name)


def container_has_cache_token(container_name: str) -> bool:
    """Whether a live registry entry grants this container cache access."""
    return any(
        value.get("container") == container_name
        for value in TokenStore(ARTIFACT_CACHE_TOKENS).values()
    )


def container_cache_capability(container_name: str) -> tuple[str, dict] | None:
    """Return one active ``(token, grant)`` pair for a container."""
    for token, value in TokenStore(ARTIFACT_CACHE_TOKENS).items():
        if value.get("container") == container_name:
            return token, value
    return None


class CacheTokenRegistry:
    def __init__(self):
        self._store = TokenStore(ARTIFACT_CACHE_TOKENS)

    def lookup(self, token: str) -> dict | None:
        return self._store.lookup(token)


class CacheRateLimiter:
    """O(1)-amortized sliding windows sized for concurrent full restores."""

    def __init__(
        self,
        windows: tuple[tuple[int, int], ...] = ((60, 60_000), (3600, 200_000)),
        max_tracked: int = 1000,
    ):
        self._windows = windows
        self._max_tracked = max_tracked
        self._requests: dict[str, list[deque[float]]] = {}
        self._last_seen: dict[str, float] = {}
        self._lock = threading.Lock()

    def check(self, key: str) -> bool:
        now = time.time()
        with self._lock:
            if key not in self._requests and len(self._requests) >= self._max_tracked:
                oldest = min(self._last_seen, key=self._last_seen.get)
                self._requests.pop(oldest, None)
                self._last_seen.pop(oldest, None)
            queues = self._requests.setdefault(key, [deque() for _ in self._windows])
            self._last_seen[key] = now
            for queue, (seconds, maximum) in zip(queues, self._windows, strict=True):
                cutoff = now - seconds
                while queue and queue[0] <= cutoff:
                    queue.popleft()
                if len(queue) >= maximum:
                    return False
            for queue in queues:
                queue.append(now)
            return True


def _cache_paths(upstream_url: str) -> tuple[Path, Path]:
    digest = hashlib.sha256(upstream_url.encode()).hexdigest()
    directory = ARTIFACT_CACHE_OBJECTS / digest[:2]
    return directory / f"{digest}.body", directory / f"{digest}.json"


@contextmanager
def _store_lock():
    ARTIFACT_CACHE_LOCK.parent.mkdir(parents=True, exist_ok=True)
    with open(ARTIFACT_CACHE_LOCK, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def cache_stats() -> tuple[int, int]:
    """Return ``(object_count, bytes)`` for completed cached response bodies."""
    count = 0
    total = 0
    if not ARTIFACT_CACHE_OBJECTS.exists():
        return count, total
    for path in ARTIFACT_CACHE_OBJECTS.glob("*/*.body"):
        try:
            total += path.stat().st_size
            count += 1
        except OSError:
            pass
    return count, total


def cleanup_stale_temps(max_age_seconds: int = 3600) -> tuple[int, int]:
    """Remove abandoned in-flight files, returning ``(count, bytes)``."""
    cutoff = time.time() - max_age_seconds
    count = 0
    total = 0
    if not ARTIFACT_CACHE_OBJECTS.exists():
        return count, total
    for path in ARTIFACT_CACHE_OBJECTS.glob("*/.*.tmp"):
        try:
            stat = path.stat()
            if stat.st_mtime > cutoff:
                continue
            path.unlink()
            count += 1
            total += stat.st_size
        except OSError:
            pass
    return count, total


def prune_cache(max_bytes: int, *, execute: bool = True) -> tuple[int, int]:
    """Evict least-recently-used objects until the store fits ``max_bytes``.

    Returns ``(objects_selected, bytes_selected)``. Access time is represented
    by body-file mtime, explicitly touched on every hit so this works even on
    filesystems mounted with ``noatime``.
    """
    if max_bytes < 0:
        raise ValueError("cache size cannot be negative")
    if execute:
        cleanup_stale_temps()
    with _store_lock():
        entries: list[tuple[float, int, Path, Path]] = []
        total = 0
        if ARTIFACT_CACHE_OBJECTS.exists():
            for body in ARTIFACT_CACHE_OBJECTS.glob("*/*.body"):
                try:
                    stat = body.stat()
                except OSError:
                    continue
                meta = body.with_suffix(".json")
                entries.append((stat.st_mtime, stat.st_size, body, meta))
                total += stat.st_size
        selected: list[tuple[int, Path, Path]] = []
        for _, size, body, meta in sorted(entries):
            if total <= max_bytes:
                break
            selected.append((size, body, meta))
            total -= size
        if execute:
            for _, body, meta in selected:
                body.unlink(missing_ok=True)
                meta.unlink(missing_ok=True)
        return len(selected), sum(size for size, _, _ in selected)


def clear_cache() -> tuple[int, int]:
    """Delete every completed cache object, returning the previous statistics."""
    with _store_lock():
        stats = cache_stats()
        shutil.rmtree(ARTIFACT_CACHE_OBJECTS, ignore_errors=True)
        return stats


def _load_cached(body: Path, meta: Path) -> dict | None:
    try:
        if not body.is_file():
            return None
        data = json.loads(meta.read_text())
        if not isinstance(data.get("headers"), dict):
            return None
        os.utime(body, None)
        return data
    except (OSError, ValueError, json.JSONDecodeError):
        return None


# One lazy process-wide, thread-safe client keeps upstream TLS connections
# alive. Mathlib restores contain thousands of small objects, so creating a new
# TLS connection for each object is prohibitively slow on high-latency links.
_UPSTREAM_CLIENT = None
_UPSTREAM_CLIENT_LOCK = threading.Lock()


def _upstream_client():
    global _UPSTREAM_CLIENT
    if _UPSTREAM_CLIENT is None:
        with _UPSTREAM_CLIENT_LOCK:
            if _UPSTREAM_CLIENT is None:
                # Redirects remain disabled so a configured origin cannot
                # redirect the proxy outside its exact allowlist. Environment
                # proxies are also ignored for the same reason.
                _UPSTREAM_CLIENT = httpx.Client(
                    http2=True,
                    follow_redirects=False,
                    trust_env=False,
                    headers={"User-Agent": "bubble"},
                    limits=httpx.Limits(
                        max_connections=64,
                        max_keepalive_connections=64,
                        keepalive_expiry=60,
                    ),
                    timeout=httpx.Timeout(120, connect=15),
                )
    return _UPSTREAM_CLIENT


def _close_upstream_client() -> None:
    global _UPSTREAM_CLIENT
    with _UPSTREAM_CLIENT_LOCK:
        client = _UPSTREAM_CLIENT
        _UPSTREAM_CLIENT = None
    if isinstance(client, httpx.Client):
        client.close()


class ArtifactCacheHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    timeout = 60
    token_registry: CacheTokenRegistry
    rate_limiter: CacheRateLimiter
    peer_rate_limiter: CacheRateLimiter
    max_bytes = DEFAULT_MAX_BYTES
    max_object_bytes = DEFAULT_MAX_OBJECT_BYTES
    cache_bytes = 0
    _usage_lock = threading.Lock()
    _prune_running = False
    _key_locks: dict[str, list] = {}
    _key_locks_guard = threading.Lock()

    def log_message(self, format, *args):
        logger.info(format, *args)

    def log_error(self, format, *args):
        """Log parser errors without a capability-bearing request line."""
        logger.warning(format, *args)

    def log_request(self, code="-", size="-"):
        """Suppress BaseHTTPRequestHandler's capability-bearing request line."""

    def _error(self, status: int, message: str) -> None:
        body = (message + "\n").encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _route(self) -> tuple[str, list[str], str] | None:
        if not self.peer_rate_limiter.check(self.client_address[0]):
            self._error(429, "artifact cache peer request limit exceeded")
            return None
        parsed = urlparse(self.path)
        if parsed.query or parsed.fragment:
            self._error(400, "cache URLs do not accept query strings or fragments")
            return None
        parts = parsed.path.split("/", 4)
        if len(parts) < 4 or parts[:2] != ["", "cache"]:
            self._error(404, "unknown cache route")
            return None
        token, route = parts[2], parts[3]
        suffix = parts[4] if len(parts) == 5 else ""
        if not _TOKEN_RE.fullmatch(token) or not _SAFE_ROUTE_RE.fullmatch(route):
            self._error(403, "invalid cache capability")
            return None
        lower = suffix.lower()
        if "%2f" in lower or "%5c" in lower or "%2e" in lower or "\\" in suffix:
            self._error(400, "encoded path separators are not allowed")
            return None
        if any(part in (".", "..") for part in suffix.split("/")):
            self._error(400, "dot path segments are not allowed")
            return None
        if any(ord(ch) < 32 or ord(ch) == 127 for ch in suffix):
            self._error(400, "control characters are not allowed")
            return None
        if len(suffix) > 4096 or not re.fullmatch(r"[A-Za-z0-9._~%/-]*", suffix):
            self._error(400, "cache path contains unsupported characters")
            return None
        info = self.token_registry.lookup(token)
        routes = info.get("routes") if info else None
        endpoints = routes.get(route) if isinstance(routes, dict) else None
        if not isinstance(endpoints, list) or not endpoints:
            self._error(403, "cache route is not granted by this capability")
            return None
        container = str(info.get("container", "unknown"))
        if not self.rate_limiter.check(container):
            self._error(429, "artifact cache request limit exceeded")
            return None
        return container, endpoints, suffix

    @classmethod
    @contextmanager
    def _locked_key(cls, key: str):
        with cls._key_locks_guard:
            entry = cls._key_locks.get(key)
            if entry is None:
                entry = [threading.Lock(), 0]
                cls._key_locks[key] = entry
            entry[1] += 1
        try:
            with entry[0]:
                yield
        finally:
            with cls._key_locks_guard:
                entry[1] -= 1
                if entry[1] == 0 and cls._key_locks.get(key) is entry:
                    cls._key_locks.pop(key, None)

    def _send_open_file(self, source, metadata: dict, cache_state: str) -> None:
        self.send_response(200)
        for name, value in metadata.get("headers", {}).items():
            if name.lower() not in _STRIPPED_HEADERS and name.lower() != "content-length":
                self.send_header(name, str(value))
        self.send_header("Content-Length", str(os.fstat(source.fileno()).st_size))
        self.send_header("X-Bubble-Cache", cache_state)
        self.end_headers()
        if self.command != "HEAD":
            shutil.copyfileobj(source, self.wfile, length=1024 * 1024)

    def _send_upstream_error(self, response) -> None:
        status = getattr(response, "status_code", getattr(response, "status", 502))
        data = b""
        if self.command != "HEAD":
            chunks = []
            remaining = MAX_ERROR_BODY
            for chunk in response.iter_bytes(chunk_size=min(64 * 1024, remaining)):
                chunks.append(chunk[:remaining])
                remaining -= len(chunks[-1])
                if remaining <= 0:
                    break
            data = b"".join(chunks)
        self.send_response(status)
        content_type = response.headers.get("Content-Type")
        if content_type:
            self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Bubble-Cache", "MISS")
        self.end_headers()
        if data:
            self.wfile.write(data)

    def _fetch(self, endpoints: list[str], suffix: str, body: Path, meta: Path) -> int | None:
        body.parent.mkdir(parents=True, exist_ok=True)
        try:
            old_size = body.stat().st_size
        except OSError:
            old_size = 0
        for index, endpoint in enumerate(endpoints):
            url = endpoint if not suffix else f"{endpoint}/{suffix}"
            try:
                response_context = _upstream_client().stream("GET", url)
                response = response_context.__enter__()
            except (OSError, httpx.RequestError) as exc:
                if index + 1 < len(endpoints):
                    continue
                self._error(502, f"artifact cache upstream failed: {exc}")
                return None

            try:
                status = response.status_code
                if 300 <= status <= 399:
                    self._error(502, "artifact upstream redirect refused by cache policy")
                    return None
                if (status in (403, 404, 410) or 500 <= status <= 599) and index + 1 < len(
                    endpoints
                ):
                    continue
                if status != 200:
                    self._send_upstream_error(response)
                    return None

                try:
                    content_length = int(response.headers.get("Content-Length", 0))
                except (TypeError, ValueError):
                    content_length = 0
                free_bytes = shutil.disk_usage(body.parent).free
                if content_length > self.max_object_bytes or (
                    content_length and content_length + 256 * 1024**2 > free_bytes
                ):
                    self._error(507, "artifact is too large for the host cache")
                    return None

                tmp_body = body.with_name(f".{body.name}.{os.getpid()}.{threading.get_ident()}.tmp")
                tmp_meta = meta.with_name(f".{meta.name}.{os.getpid()}.{threading.get_ident()}.tmp")
                try:
                    with open(tmp_body, "wb") as target:
                        # Preserve the exact stored representation. ``iter_bytes``
                        # transparently decompresses content encodings, which
                        # would mismatch the forwarded Content-Encoding header.
                        for chunk in response.iter_raw(chunk_size=1024 * 1024):
                            target.write(chunk)
                            if target.tell() > self.max_object_bytes:
                                raise ValueError("artifact exceeds the per-object cache limit")
                    headers = {
                        name: value
                        for name, value in response.headers.items()
                        if name.lower() not in _STRIPPED_HEADERS
                        and name.lower() not in ("content-length", "set-cookie")
                    }
                    tmp_meta.write_text(json.dumps({"url": url, "headers": headers}))
                    os.replace(tmp_body, body)
                    os.replace(tmp_meta, meta)
                    return body.stat().st_size - old_size
                except (OSError, ValueError, httpx.RequestError) as exc:
                    tmp_body.unlink(missing_ok=True)
                    tmp_meta.unlink(missing_ok=True)
                    self._error(502, f"could not store artifact cache response: {exc}")
                    return None
            finally:
                response_context.__exit__(None, None, None)
        return None

    @classmethod
    def _note_stored(cls, added_bytes: int) -> None:
        """Schedule one background LRU scan only when the in-memory total is over budget."""
        with cls._usage_lock:
            cls.cache_bytes += added_bytes
            if cls.cache_bytes <= cls.max_bytes or cls._prune_running:
                return
            cls._prune_running = True

        def prune() -> None:
            try:
                prune_cache(cls.max_bytes)
                _, total = cache_stats()
                with cls._usage_lock:
                    cls.cache_bytes = total
            finally:
                with cls._usage_lock:
                    cls._prune_running = False

        threading.Thread(target=prune, name="bubble-cache-prune", daemon=True).start()

    def _serve(self) -> None:
        if self.command not in ("GET", "HEAD"):
            # Methods with request bodies have not been consumed. Do not keep
            # their HTTP/1.1 connection alive and parse body bytes as a request.
            self.close_connection = True
            self._error(405, "artifact cache is GET/HEAD only")
            return
        routed = self._route()
        if routed is None:
            return
        container, endpoints, suffix = routed
        # Include the complete fallback chain in the key. If Bubble changes a
        # route's origin policy, old objects cannot be confused with new ones.
        cache_identity = json.dumps([endpoints, suffix], separators=(",", ":"))
        body, meta = _cache_paths(cache_identity)
        key = hashlib.sha256(cache_identity.encode()).hexdigest()
        with self._locked_key(key):
            metadata = _load_cached(body, meta)
            cache_state = "HIT"
            added_bytes = 0
            if metadata is None:
                cache_state = "MISS"
                fetched = self._fetch(endpoints, suffix, body, meta)
                if fetched is None:
                    logger.info("MISS container=%s path=%s", container, suffix)
                    return
                added_bytes = fetched
                metadata = _load_cached(body, meta)
            if metadata is None:
                self._error(502, "artifact cache entry became unavailable")
                return
            logger.info("%s container=%s path=%s", cache_state, container, suffix)
            try:
                source = open(body, "rb")
            except OSError:
                self._error(503, "artifact cache entry became unavailable")
                return
        try:
            self._send_open_file(source, metadata, cache_state)
        except OSError:
            pass
        finally:
            source.close()
        if added_bytes:
            self._note_stored(added_bytes)

    do_GET = _serve
    do_HEAD = _serve
    do_POST = _serve
    do_PUT = _serve
    do_PATCH = _serve
    do_DELETE = _serve


class _BoundedServerMixin:
    """Queue accepted sockets before spawning at most 256 request threads."""

    _request_slots = threading.BoundedSemaphore(256)
    request_queue_size = 256

    def process_request(self, request, client_address):
        self._request_slots.acquire()
        try:
            super().process_request(request, client_address)
        except Exception:
            self._request_slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._request_slots.release()


class ArtifactCacheServer(_BoundedServerMixin, ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 256


def _resolve_bind() -> tuple[str, str | None]:
    if os.uname().sysname == "Darwin":
        from .runtime.colima import colima_bind_ip

        address = colima_bind_ip()
        if address.startswith("127."):
            raise RuntimeError("could not determine a container-reachable Colima bridge address")
        return address, None
    from .incus_bridge import BRIDGE_INTERFACE, bridge_gateway_ipv4

    return bridge_gateway_ipv4(), BRIDGE_INTERFACE


def _write_endpoint(host: str, port: int) -> None:
    HOST_DATA_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "tcp": {"host": host, "port": port},
        "version": 1,
        "bubble_version": __version__,
        "capabilities": ["get-only", "fallback-origins", "lake-services"],
        "pid": os.getpid(),
    }
    tmp = ARTIFACT_CACHE_ENDPOINT_FILE.with_name(
        f".{ARTIFACT_CACHE_ENDPOINT_FILE.name}.{os.getpid()}.tmp"
    )
    tmp.write_text(json.dumps(payload))
    os.chmod(tmp, 0o600)
    os.replace(tmp, ARTIFACT_CACHE_ENDPOINT_FILE)


def run_daemon(port: int = 0, max_bytes: int | None = None) -> None:
    """Run the bridge-bound host artifact cache daemon."""
    setup_file_logging(logger, ARTIFACT_CACHE_LOG)
    config = load_host_artifact_config()
    if not port:
        port = int(config.get("port", DEFAULT_PORT))
    if max_bytes is None:
        max_bytes = parse_size(config.get("max_size", "50GiB"))
    ARTIFACT_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cleanup_stale_temps(max_age_seconds=0)
    registry = CacheTokenRegistry()
    rate_limiter = CacheRateLimiter()
    ArtifactCacheHandler.token_registry = registry
    ArtifactCacheHandler.rate_limiter = rate_limiter
    ArtifactCacheHandler.peer_rate_limiter = CacheRateLimiter(
        windows=((60, 120_000), (3600, 400_000))
    )
    ArtifactCacheHandler.max_bytes = max_bytes
    ArtifactCacheHandler.max_object_bytes = min(max_bytes, DEFAULT_MAX_OBJECT_BYTES)
    _, ArtifactCacheHandler.cache_bytes = cache_stats()
    host, device = _resolve_bind()
    if device:
        from .auth_proxy import BridgeBoundHTTPServer

        class BridgeArtifactCacheServer(_BoundedServerMixin, BridgeBoundHTTPServer):
            pass

        server = BridgeArtifactCacheServer((host, port), ArtifactCacheHandler, bind_device=device)
    else:
        server = ArtifactCacheServer((host, port), ArtifactCacheHandler)
    _write_endpoint(host, port)
    print(f"Artifact cache listening on {host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        _close_upstream_client()


def parse_size(value: str | int) -> int:
    """Parse a byte count with optional KiB/MiB/GiB/TiB suffix."""
    if isinstance(value, int) and not isinstance(value, bool):
        if value < 0:
            raise ValueError("cache size cannot be negative")
        return value
    match = re.fullmatch(r"\s*(\d+)\s*([KMGT]?i?B)?\s*", str(value), re.IGNORECASE)
    if not match:
        raise ValueError(f"invalid cache size: {value!r}")
    number = int(match.group(1))
    suffix = (match.group(2) or "B").upper()
    powers = {"B": 0, "KB": 1, "KIB": 1, "MB": 2, "MIB": 2, "GB": 3, "GIB": 3, "TB": 4, "TIB": 4}
    return number * 1024 ** powers[suffix]


def endpoint_alive(endpoint: dict) -> bool:
    tcp = endpoint.get("tcp") if isinstance(endpoint, dict) else None
    pid = endpoint.get("pid") if isinstance(endpoint, dict) else None
    if not isinstance(tcp, dict) or not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    host, port = tcp.get("host"), tcp.get("port")
    if not isinstance(host, str) or not isinstance(port, int) or isinstance(port, bool):
        return False
    try:
        os.kill(pid, 0)
        with socket.create_connection((host, port), timeout=1):
            pass
    except OSError:
        return False
    return bool(host and 1 <= port <= 65535)


def read_live_endpoint() -> dict | None:
    try:
        endpoint = json.loads(ARTIFACT_CACHE_ENDPOINT_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return endpoint if endpoint_alive(endpoint) else None


def endpoint_compatible(endpoint: dict | None) -> bool:
    """Whether a live daemon implements this client's cache contract."""
    if endpoint is None or endpoint.get("version") != 1:
        return False
    capabilities = endpoint.get("capabilities")
    required = {"get-only", "fallback-origins", "lake-services"}
    return isinstance(capabilities, list) and required.issubset(capabilities)


def wait_for_endpoint(
    attempts: int = 20, delay: float = 0.5, *, compatible: bool = False
) -> dict | None:
    for _ in range(attempts):
        endpoint = read_live_endpoint()
        if endpoint is not None and (not compatible or endpoint_compatible(endpoint)):
            return endpoint
        time.sleep(delay)
    return None


def ensure_daemon_endpoint() -> dict | None:
    """Start or refresh the daemon and return its live bridge endpoint."""
    from .automation import install_artifact_cache_daemon, is_artifact_cache_installed

    installed = is_artifact_cache_installed()
    if not installed:
        install_artifact_cache_daemon(skip_if=lambda: endpoint_compatible(read_live_endpoint()))
    endpoint = wait_for_endpoint(
        attempts=20 if not installed else 1,
        delay=0.5 if not installed else 0,
        compatible=True,
    )
    if endpoint is not None:
        return endpoint
    # Refresh stale service definitions after Bubble upgrades.
    install_artifact_cache_daemon(skip_if=lambda: endpoint_compatible(read_live_endpoint()))
    return wait_for_endpoint(compatible=True)


def _write_container_config(
    runtime,
    container: str,
    *,
    host: str,
    port: int,
    token: str,
    mathlib: bool,
    lake_service_names: tuple[str, ...],
) -> bool:
    """Write endpoint-dependent client configuration without exposing the token in argv."""
    import ipaddress

    try:
        if ipaddress.ip_address(host).version != 4:
            return False
    except ValueError:
        return False
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        return False

    from .github_token import _allow_bridge_egress

    if not _allow_bridge_egress(runtime, container, host, port):
        return False

    base = f"http://{host}:{port}/cache/$TOKEN"
    profile_lines: list[str] = []
    if mathlib:
        profile_lines.append(f"export MATHLIB_CACHE_GET_URL={base}/mathlib")
    config_lines: list[str] = []
    if lake_service_names:
        # Lake/Load/Toml.lean consumes this schema; Lake's help documents
        # ~/.lake/config.toml as the default system configuration path.
        config_lines.append(f"cache.defaultService = {json.dumps(lake_service_names[0])}")
        for index, name in enumerate(lake_service_names):
            config_lines.extend(
                [
                    "",
                    "[[cache.service]]",
                    f"name = {json.dumps(name)}",
                    'kind = "s3"',
                    f'artifactEndpoint = "{base}/lake-{index}-artifacts"',
                    f'revisionEndpoint = "{base}/lake-{index}-revisions"',
                ]
            )
        profile_lines.extend(
            [
                "export LAKE_ARTIFACT_CACHE=true",
                "export LAKE_RESTORE_ARTIFACTS=true",
            ]
        )

    script_lines = [
        "set -e",
        "IFS= read -r TOKEN",
        "mkdir -p /etc/profile.d",
        "install -d -m 0755 -o user -g user /home/user/.lake",
    ]
    if config_lines:
        script_lines.extend(
            [
                "cat > /home/user/.lake/config.toml <<CACHE",
                *config_lines,
                "CACHE",
                "chown user:user /home/user/.lake/config.toml /home/user/.lake",
                "chmod 600 /home/user/.lake/config.toml",
            ]
        )
    script_lines.extend(
        [
            "cat > /etc/profile.d/bubble-artifact-cache.sh <<PROFILE",
            *profile_lines,
            "PROFILE",
            "chmod 644 /etc/profile.d/bubble-artifact-cache.sh",
        ]
    )
    try:
        runtime.exec(container, ["bash", "-c", "\n".join(script_lines)], input=token + "\n")
    except RuntimeError:
        return False
    return True


def refresh_container_cache(runtime, container: str) -> bool:
    """Refresh a stopped/restarted bubble for the daemon's current bridge endpoint."""
    capability = container_cache_capability(container)
    if capability is None:
        return True
    token, info = capability
    endpoint = ensure_daemon_endpoint()
    tcp = endpoint.get("tcp") if endpoint else None
    if not isinstance(tcp, dict):
        return False
    names = info.get("lake_service_names") or []
    if not isinstance(names, list) or not all(isinstance(name, str) for name in names):
        return False
    return _write_container_config(
        runtime,
        container,
        host=tcp.get("host"),
        port=tcp.get("port"),
        token=token,
        mathlib=bool(info.get("mathlib")),
        lake_service_names=tuple(names),
    )


def setup_container_cache(
    runtime,
    container: str,
    *,
    mathlib: bool,
    lake_services: tuple[LakeCacheService, ...] = (),
) -> bool:
    """Grant and configure a container's Mathlib and Lake download routes."""
    if load_host_artifact_config().get("enabled", True) is False:
        return False
    routes: dict[str, list[str] | tuple[str, ...]] = {}
    if mathlib:
        routes["mathlib"] = MATHLIB_CACHE_ENDPOINTS
    for index, service in enumerate(lake_services):
        routes[f"lake-{index}-artifacts"] = [service.artifact_endpoint]
        routes[f"lake-{index}-revisions"] = [service.revision_endpoint]
    if not routes:
        return True

    endpoint = ensure_daemon_endpoint()
    if endpoint is None:
        return False
    tcp = endpoint.get("tcp") or {}
    host, port = tcp.get("host"), tcp.get("port")
    if not isinstance(host, str) or not isinstance(port, int) or host.startswith("127."):
        return False

    # A crashed prior creation with the same name may have left a grant behind.
    remove_cache_tokens(container)
    token = generate_cache_token(
        container,
        routes,
        mathlib=mathlib,
        lake_service_names=tuple(service.name for service in lake_services),
    )
    if not _write_container_config(
        runtime,
        container,
        host=host,
        port=port,
        token=token,
        mathlib=mathlib,
        lake_service_names=tuple(service.name for service in lake_services),
    ):
        remove_cache_tokens(container)
        return False
    return True
