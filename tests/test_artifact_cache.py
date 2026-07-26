"""Tests for the host-global, capability-scoped artifact cache."""

import gzip
import io
import json
import os
import threading
import urllib.error
import urllib.request

import pytest
from click import ClickException
from click.testing import CliRunner

from bubble import artifact_cache
from bubble.cli import _require_explicit_lake_cache, main


class FakeResponse(io.BytesIO):
    def __init__(
        self,
        body: bytes,
        *,
        content_type: str = "application/octet-stream",
        content_encoding: str | None = None,
        status: int = 200,
    ):
        super().__init__(body)
        self.status = status
        self.status_code = status
        self.headers = {
            "Content-Type": content_type,
            "Content-Length": str(len(body)),
            "ETag": '"abc"',
            "Date": "yesterday",
            "Server": "upstream",
        }
        if content_encoding:
            self.headers["Content-Encoding"] = content_encoding

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()

    def iter_bytes(self, chunk_size):
        body = self.getvalue()
        if self.headers.get("Content-Encoding") == "gzip":
            body = gzip.decompress(body)
        for offset in range(0, len(body), chunk_size):
            yield body[offset : offset + chunk_size]

    def iter_raw(self, chunk_size):
        body = self.getvalue()
        for offset in range(0, len(body), chunk_size):
            yield body[offset : offset + chunk_size]


class FakeClient:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.urls = []

    def stream(self, method, url):
        assert method == "GET"
        self.urls.append(url)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


@pytest.fixture
def cache_paths(tmp_path, monkeypatch):
    root = tmp_path / "cache"
    monkeypatch.setattr(artifact_cache, "ARTIFACT_CACHE_DIR", root)
    monkeypatch.setattr(artifact_cache, "ARTIFACT_CACHE_OBJECTS", root / "objects")
    monkeypatch.setattr(artifact_cache, "ARTIFACT_CACHE_TOKENS", tmp_path / "tokens.json")
    monkeypatch.setattr(artifact_cache, "ARTIFACT_CACHE_LOCK", tmp_path / "cache.lock")
    return root


@pytest.fixture
def cache_server(cache_paths, monkeypatch):
    monkeypatch.setattr(
        artifact_cache.ArtifactCacheHandler,
        "token_registry",
        artifact_cache.CacheTokenRegistry(),
        raising=False,
    )
    monkeypatch.setattr(
        artifact_cache.ArtifactCacheHandler,
        "rate_limiter",
        artifact_cache.CacheRateLimiter(),
        raising=False,
    )
    monkeypatch.setattr(
        artifact_cache.ArtifactCacheHandler,
        "peer_rate_limiter",
        artifact_cache.CacheRateLimiter(),
        raising=False,
    )
    monkeypatch.setattr(artifact_cache.ArtifactCacheHandler, "max_bytes", 1024**3)
    monkeypatch.setattr(artifact_cache.ArtifactCacheHandler, "max_object_bytes", 1024**3)
    monkeypatch.setattr(artifact_cache.ArtifactCacheHandler, "cache_bytes", 0)
    monkeypatch.setattr(artifact_cache.ArtifactCacheHandler, "_prune_running", False)
    server = artifact_cache.ArtifactCacheServer(
        ("127.0.0.1", 0), artifact_cache.ArtifactCacheHandler
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def _http(url: str, *, method: str = "GET"):
    request = urllib.request.Request(url, method=method)
    with urllib.request.urlopen(request, timeout=5) as response:
        return response.status, response.headers, response.read()


def test_endpoint_and_service_validation():
    service = artifact_cache.validate_lake_service(
        "tauceti-public", "https://cache.example/artifacts/", "https://cache.example/revisions"
    )
    assert service.artifact_endpoint == "https://cache.example/artifacts"
    with pytest.raises(ValueError):
        artifact_cache.validate_lake_service("../bad", "https://a.example", "https://r.example")
    with pytest.raises(ValueError):
        artifact_cache.normalize_endpoint("http://cache.example")
    with pytest.raises(ValueError):
        artifact_cache.normalize_endpoint("https://user:secret@cache.example")


def test_open_help_exposes_three_argument_lake_service():
    result = CliRunner().invoke(main, ["open", "--help"])
    assert result.exit_code == 0
    assert "--lake-cache-service NAME ARTIFACT_URL REVISION_URL" in result.output


def test_requested_lake_service_setup_fails_closed():
    with pytest.raises(ClickException, match="requested Lake cache service setup failed"):
        _require_explicit_lake_cache(False, ("tauceti-public",))
    _require_explicit_lake_cache(False, ())
    _require_explicit_lake_cache(True, ("tauceti-public",))


def test_upstream_requests_disable_content_encoding():
    client = artifact_cache._upstream_client()
    try:
        assert client.headers["Accept-Encoding"] == "identity"
    finally:
        artifact_cache._close_upstream_client()


def test_capability_is_scoped_to_its_routes(cache_paths):
    token = artifact_cache.generate_cache_token(
        "one", {"mathlib": ["https://cache.example/mathlib"]}
    )
    info = artifact_cache.CacheTokenRegistry().lookup(token)
    assert info["container"] == "one"
    assert info["routes"] == {"mathlib": ["https://cache.example/mathlib"]}
    artifact_cache.remove_cache_tokens("one")
    assert artifact_cache.CacheTokenRegistry().lookup(token) is None


def test_get_miss_then_host_global_hit(cache_server, monkeypatch):
    client = FakeClient([FakeResponse(b"artifact", content_type="application/test-artifact")])
    monkeypatch.setattr(artifact_cache, "_UPSTREAM_CLIENT", client)
    token = artifact_cache.generate_cache_token(
        "one", {"lake-0-artifacts": ["https://cache.example/artifacts"]}
    )
    url = f"{cache_server}/cache/{token}/lake-0-artifacts/owner/repo/abc.art"

    status, headers, body = _http(url)
    assert (status, body, headers["X-Bubble-Cache"]) == (200, b"artifact", "MISS")
    assert headers["Content-Type"] == "application/test-artifact"
    assert headers.get_all("Date") is not None and len(headers.get_all("Date")) == 1
    assert headers.get_all("Server") is not None and len(headers.get_all("Server")) == 1
    status, headers, body = _http(url)
    assert (status, body, headers["X-Bubble-Cache"]) == (200, b"artifact", "HIT")
    assert client.urls == ["https://cache.example/artifacts/owner/repo/abc.art"]


def test_mathlib_route_falls_back_on_404(cache_server, monkeypatch):
    client = FakeClient([FakeResponse(b"missing", status=404), FakeResponse(b"legacy")])
    monkeypatch.setattr(artifact_cache, "_UPSTREAM_CLIENT", client)
    token = artifact_cache.generate_cache_token(
        "one", {"mathlib": ["https://new.example", "https://legacy.example"]}
    )
    _, _, body = _http(f"{cache_server}/cache/{token}/mathlib/f/hash.tar.gz")
    assert body == b"legacy"
    assert client.urls == [
        "https://new.example/f/hash.tar.gz",
        "https://legacy.example/f/hash.tar.gz",
    ]


def test_mathlib_route_falls_back_on_connection_error(cache_server, monkeypatch):
    client = FakeClient([OSError("offline"), FakeResponse(b"legacy")])
    monkeypatch.setattr(artifact_cache, "_UPSTREAM_CLIENT", client)
    token = artifact_cache.generate_cache_token(
        "one", {"mathlib": ["https://new.example", "https://legacy.example"]}
    )

    _, _, body = _http(f"{cache_server}/cache/{token}/mathlib/f/hash.tar.gz")

    assert body == b"legacy"
    assert client.urls == [
        "https://new.example/f/hash.tar.gz",
        "https://legacy.example/f/hash.tar.gz",
    ]


def test_upstream_redirect_is_refused(cache_server, monkeypatch):
    client = FakeClient([FakeResponse(b"redirect", status=302)])
    monkeypatch.setattr(artifact_cache, "_UPSTREAM_CLIENT", client)
    token = artifact_cache.generate_cache_token("one", {"mathlib": ["https://cache.example"]})

    with pytest.raises(urllib.error.HTTPError) as denied:
        _http(f"{cache_server}/cache/{token}/mathlib/f/hash")

    assert denied.value.code == 502
    assert client.urls == ["https://cache.example/f/hash"]


def test_success_preserves_content_encoding(cache_server, monkeypatch):
    encoded = gzip.compress(b"artifact")
    client = FakeClient([FakeResponse(encoded, content_encoding="gzip")])
    monkeypatch.setattr(artifact_cache, "_UPSTREAM_CLIENT", client)
    token = artifact_cache.generate_cache_token("one", {"mathlib": ["https://cache.example"]})

    status, headers, body = _http(f"{cache_server}/cache/{token}/mathlib/f/hash")

    assert status == 200
    assert headers["Content-Encoding"] == "gzip"
    assert body == encoded


def test_concurrent_same_key_fetches_upstream_once(cache_server, monkeypatch):
    started = threading.Event()
    release = threading.Event()

    class BlockingResponse(FakeResponse):
        def iter_raw(self, chunk_size):
            started.set()
            assert release.wait(5)
            yield from super().iter_raw(chunk_size)

    client = FakeClient([BlockingResponse(b"artifact")])
    monkeypatch.setattr(artifact_cache, "_UPSTREAM_CLIENT", client)
    token = artifact_cache.generate_cache_token("one", {"mathlib": ["https://cache.example"]})
    url = f"{cache_server}/cache/{token}/mathlib/f/hash"
    results = []

    first = threading.Thread(target=lambda: results.append(_http(url)[2]))
    first.start()
    assert started.wait(5)
    second = threading.Thread(target=lambda: results.append(_http(url)[2]))
    second.start()
    release.set()
    first.join(timeout=5)
    second.join(timeout=5)

    assert not first.is_alive() and not second.is_alive()
    assert results == [b"artifact", b"artifact"]
    assert client.urls == ["https://cache.example/f/hash"]


def test_proxy_rejects_write_and_ungranted_routes(cache_server, monkeypatch):
    client = FakeClient([])
    monkeypatch.setattr(artifact_cache, "_UPSTREAM_CLIENT", client)
    token = artifact_cache.generate_cache_token("one", {"mathlib": ["https://cache.example"]})
    with pytest.raises(urllib.error.HTTPError) as write:
        _http(f"{cache_server}/cache/{token}/mathlib/f/hash", method="PUT")
    assert write.value.code == 405
    with pytest.raises(urllib.error.HTTPError) as denied:
        _http(f"{cache_server}/cache/{token}/lake-0-artifacts/hash")
    assert denied.value.code == 403
    assert client.urls == []


def test_proxy_rejects_encoded_traversal_and_queries(cache_server, monkeypatch):
    monkeypatch.setattr(artifact_cache, "_UPSTREAM_CLIENT", FakeClient([]))
    token = artifact_cache.generate_cache_token("one", {"mathlib": ["https://cache.example"]})
    bad_urls = [
        f"{cache_server}/cache/{token}/mathlib/f/%2e%2e/secret",
        f"{cache_server}/cache/{token}/mathlib/f/%2fsecret",
        f"{cache_server}/cache/{token}/mathlib/f/hash?origin=other",
    ]
    for url in bad_urls:
        with pytest.raises(urllib.error.HTTPError) as denied:
            _http(url)
        assert denied.value.code == 400


def test_head_populates_without_returning_a_body(cache_server, monkeypatch):
    monkeypatch.setattr(artifact_cache, "_UPSTREAM_CLIENT", FakeClient([FakeResponse(b"artifact")]))
    token = artifact_cache.generate_cache_token("one", {"mathlib": ["https://cache.example"]})
    status, headers, body = _http(f"{cache_server}/cache/{token}/mathlib/f/hash", method="HEAD")
    assert status == 200 and body == b""
    assert headers["Content-Length"] == str(len(b"artifact"))


def test_oversize_response_is_not_cached(cache_server, monkeypatch):
    monkeypatch.setattr(artifact_cache.ArtifactCacheHandler, "max_object_bytes", 3)
    monkeypatch.setattr(artifact_cache, "_UPSTREAM_CLIENT", FakeClient([FakeResponse(b"four")]))
    token = artifact_cache.generate_cache_token("one", {"mathlib": ["https://cache.example"]})
    with pytest.raises(urllib.error.HTTPError) as denied:
        _http(f"{cache_server}/cache/{token}/mathlib/f/hash")
    assert denied.value.code == 507
    assert artifact_cache.cache_stats() == (0, 0)


def test_under_budget_miss_does_not_scan_for_pruning(cache_server, monkeypatch):
    monkeypatch.setattr(artifact_cache, "_UPSTREAM_CLIENT", FakeClient([FakeResponse(b"artifact")]))
    monkeypatch.setattr(
        artifact_cache,
        "prune_cache",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("unexpected scan")),
    )
    token = artifact_cache.generate_cache_token("one", {"mathlib": ["https://cache.example"]})
    _, _, body = _http(f"{cache_server}/cache/{token}/mathlib/f/hash")
    assert body == b"artifact"


def test_capability_token_is_not_written_to_request_log(cache_server, cache_paths, monkeypatch):
    import logging

    log_path = cache_paths / "requests.log"
    logger = logging.getLogger("bubble.artifact_cache")
    existing = list(logger.handlers)
    artifact_cache.setup_file_logging(logger, log_path)
    monkeypatch.setattr(artifact_cache, "_UPSTREAM_CLIENT", FakeClient([FakeResponse(b"artifact")]))
    token = artifact_cache.generate_cache_token("one", {"mathlib": ["https://cache.example"]})
    _http(f"{cache_server}/cache/{token}/mathlib/f/hash")
    for handler in logger.handlers:
        handler.flush()
    assert token not in log_path.read_text()
    assert (log_path.stat().st_mode & 0o777) == 0o600
    for handler in list(logger.handlers):
        if handler not in existing:
            handler.close()
            logger.removeHandler(handler)


def test_prune_uses_explicit_access_mtime(cache_paths):
    bodies = []
    for index in range(3):
        body, meta = artifact_cache._cache_paths(f"object-{index}")
        body.parent.mkdir(parents=True, exist_ok=True)
        body.write_bytes(b"x" * 10)
        meta.write_text(json.dumps({"headers": {}}))
        os.utime(body, (index + 1, index + 1))
        bodies.append(body)
    count, size = artifact_cache.prune_cache(20, execute=False)
    assert (count, size) == (1, 10)
    assert all(path.exists() for path in bodies)
    artifact_cache.prune_cache(20)
    assert not bodies[0].exists()
    assert bodies[1].exists() and bodies[2].exists()


def test_cleanup_stale_temps(cache_paths, monkeypatch):
    directory = artifact_cache.ARTIFACT_CACHE_OBJECTS / "ab"
    directory.mkdir(parents=True)
    stale = directory / ".old.body.1.2.tmp"
    fresh = directory / ".new.body.1.2.tmp"
    stale.write_bytes(b"old")
    fresh.write_bytes(b"newer")
    os.utime(stale, (1, 1))
    monkeypatch.setattr(artifact_cache.time, "time", lambda: 4000)

    assert artifact_cache.cleanup_stale_temps(3600) == (1, 3)
    assert not stale.exists()
    assert fresh.exists()


def test_cache_rate_limiter_uses_independent_constant_time_windows(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(artifact_cache.time, "time", lambda: now[0])
    limiter = artifact_cache.CacheRateLimiter(windows=((1, 2), (10, 3)))
    assert limiter.check("one")
    assert limiter.check("one")
    assert not limiter.check("one")
    now[0] += 2
    assert limiter.check("one")
    assert not limiter.check("one")
    queues = limiter._requests["one"]
    assert [len(queue) for queue in queues] == [1, 3]


@pytest.mark.parametrize(
    ("value", "expected"),
    [("1KiB", 1024), ("2 MB", 2 * 1024**2), (3, 3), ("0", 0)],
)
def test_parse_size(value, expected):
    assert artifact_cache.parse_size(value) == expected


@pytest.mark.parametrize("value", [-1, True, "-2GiB", "garbage"])
def test_parse_size_rejects_invalid_values(value):
    with pytest.raises(ValueError):
        artifact_cache.parse_size(value)


def test_setup_writes_mathlib_and_lake_download_config(mock_runtime, monkeypatch):
    endpoint = {"tcp": {"host": "10.1.2.3", "port": 7655}}
    monkeypatch.setattr(artifact_cache, "ensure_daemon_endpoint", lambda: endpoint)
    monkeypatch.setattr("bubble.github_token._allow_bridge_egress", lambda *_args: True)
    monkeypatch.setattr(artifact_cache, "generate_cache_token", lambda *_args, **_kwargs: "a" * 64)
    service = artifact_cache.validate_lake_service(
        "tauceti-public", "https://cache.example/artifacts", "https://cache.example/revisions"
    )
    assert artifact_cache.setup_container_cache(
        mock_runtime, "c", mathlib=True, lake_services=(service,)
    )
    command = next(call[2] for call in mock_runtime.calls if call[0] == "exec")
    script = command[-1]
    assert "MATHLIB_CACHE_GET_URL=http://10.1.2.3:7655/cache/$TOKEN/mathlib" in script
    assert "/home/user/.lake/config.toml" in script
    assert "LAKE_CONFIG" not in script
    assert 'name = "tauceti-public"' in script
    assert "/lake-0-artifacts" in script and "/lake-0-revisions" in script


def test_mathlib_only_setup_creates_user_owned_lake_directory(mock_runtime, monkeypatch):
    endpoint = {"tcp": {"host": "10.1.2.3", "port": 7655}}
    monkeypatch.setattr(artifact_cache, "ensure_daemon_endpoint", lambda: endpoint)
    monkeypatch.setattr("bubble.github_token._allow_bridge_egress", lambda *_args: True)
    monkeypatch.setattr(artifact_cache, "generate_cache_token", lambda *_args, **_kwargs: "a" * 64)

    assert artifact_cache.setup_container_cache(mock_runtime, "c", mathlib=True)

    command = next(call[2] for call in mock_runtime.calls if call[0] == "exec")
    script = command[-1]
    assert "install -d -m 0755 -o user -g user /home/user/.lake" in script
    assert "/home/user/.lake/config.toml" not in script


def test_artifact_cache_can_be_disabled(mock_runtime, monkeypatch):
    monkeypatch.setattr(artifact_cache, "load_host_artifact_config", lambda: {"enabled": False})
    monkeypatch.setattr(
        artifact_cache,
        "ensure_daemon_endpoint",
        lambda: (_ for _ in ()).throw(AssertionError("daemon should not start")),
    )

    assert not artifact_cache.setup_container_cache(mock_runtime, "c", mathlib=True)


def test_setup_failure_revokes_issued_capability(mock_runtime, monkeypatch):
    monkeypatch.setattr(
        artifact_cache,
        "ensure_daemon_endpoint",
        lambda: {"tcp": {"host": "10.1.2.3", "port": 7655}},
    )
    monkeypatch.setattr(artifact_cache, "_write_container_config", lambda *_args, **_kwargs: False)
    removed = []
    monkeypatch.setattr(artifact_cache, "remove_cache_tokens", removed.append)
    assert not artifact_cache.setup_container_cache(mock_runtime, "c", mathlib=True)
    assert removed == ["c", "c"]


def test_endpoint_compatibility_requires_current_contract(monkeypatch):
    endpoint = {
        "version": 1,
        "bubble_version": artifact_cache.__version__,
        "capabilities": ["get-only", "fallback-origins", "lake-services"],
    }
    assert artifact_cache.endpoint_compatible(endpoint)
    assert artifact_cache.endpoint_compatible({**endpoint, "bubble_version": "old"})
    assert not artifact_cache.endpoint_compatible({**endpoint, "version": 2})
    assert not artifact_cache.endpoint_compatible({**endpoint, "capabilities": ["get-only"]})


def test_installed_daemon_health_is_retried_before_restart(monkeypatch):
    endpoint = {
        "version": 1,
        "capabilities": ["get-only", "fallback-origins", "lake-services"],
    }
    probes = iter([None, endpoint])
    monkeypatch.setattr(artifact_cache, "read_live_endpoint", lambda: next(probes))
    monkeypatch.setattr(artifact_cache.time, "sleep", lambda _delay: None)
    monkeypatch.setattr("bubble.automation.is_artifact_cache_installed", lambda: True)
    installs = []
    monkeypatch.setattr(
        "bubble.automation.install_artifact_cache_daemon",
        lambda **_kwargs: installs.append(True),
    )

    assert artifact_cache.ensure_daemon_endpoint() == endpoint
    assert installs == []
