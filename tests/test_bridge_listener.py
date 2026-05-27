"""Tests for the bridge-listener auth-proxy flow.

These cover the new path where bubbles reach the auth proxy directly
via the incus bridge IP (no per-container forkproxy device), plus the
helpers that support it: bridge discovery, token IP-binding, and the
network allowlist's bridge ACCEPT rule.
"""

from unittest.mock import patch

import pytest

from bubble.incus_bridge import BridgeDiscoveryError, bridge_gateway_ipv4
from bubble.network import _build_allowlist_script

# ---------------------------------------------------------------------------
# Bridge discovery
# ---------------------------------------------------------------------------


def _ip_addr_show_result(*, ipv4: str | None = None, returncode: int = 0):
    """Build a mock subprocess.run result for `ip -j addr show`."""

    class Result:
        pass

    result = Result()
    result.returncode = returncode
    if ipv4:
        result.stdout = (
            '[{"ifname":"incusbr0","addr_info":[{"family":"inet","local":"' + ipv4 + '"}]}]'
        )
    else:
        result.stdout = "[]"
    result.stderr = ""
    return result


def _incus_network_show_result(*, ipv4: str | None = None, returncode: int = 0):
    """Build a mock subprocess.run result for `incus network show`."""

    class Result:
        pass

    result = Result()
    result.returncode = returncode
    result.stdout = f"ipv4.address: {ipv4}/24\n" if ipv4 else ""
    result.stderr = ""
    return result


def test_bridge_gateway_agrees():
    """Kernel and incus agree → returns the IP."""
    with patch("bubble.incus_bridge.subprocess.run") as mock_run:
        mock_run.side_effect = [
            _ip_addr_show_result(ipv4="10.156.104.1"),
            _incus_network_show_result(ipv4="10.156.104.1"),
        ]
        assert bridge_gateway_ipv4() == "10.156.104.1"


def test_bridge_gateway_kernel_only():
    """Kernel reports an IP, `incus network show` is unavailable → use kernel."""
    with patch("bubble.incus_bridge.subprocess.run") as mock_run:
        mock_run.side_effect = [
            _ip_addr_show_result(ipv4="10.156.104.1"),
            _incus_network_show_result(returncode=1),
        ]
        assert bridge_gateway_ipv4() == "10.156.104.1"


def test_bridge_gateway_disagree_fails_closed():
    """Kernel and incus disagree → refuse to start (BridgeDiscoveryError)."""
    with patch("bubble.incus_bridge.subprocess.run") as mock_run:
        mock_run.side_effect = [
            _ip_addr_show_result(ipv4="10.156.104.1"),
            _incus_network_show_result(ipv4="192.168.100.1"),
        ]
        with pytest.raises(BridgeDiscoveryError):
            bridge_gateway_ipv4()


def test_bridge_gateway_no_interface_fails():
    """No IPv4 on incusbr0 → fail closed."""
    with patch("bubble.incus_bridge.subprocess.run") as mock_run:
        mock_run.return_value = _ip_addr_show_result(ipv4=None, returncode=0)
        with pytest.raises(BridgeDiscoveryError):
            bridge_gateway_ipv4()


# ---------------------------------------------------------------------------
# Network allowlist bridge ACCEPT
# ---------------------------------------------------------------------------


def test_allowlist_without_endpoint_omits_bridge_rule():
    script = _build_allowlist_script(["github.com"])
    assert "auth proxy" not in script
    assert "10.156.104.1" not in script


def test_allowlist_with_endpoint_emits_bridge_rule():
    script = _build_allowlist_script(["github.com"], auth_proxy_endpoint=("10.156.104.1", 7654))
    assert "# Allow direct access to the host's auth proxy (bridge flow)" in script
    assert "iptables -A OUTPUT -d 10.156.104.1 -p tcp --dport 7654 -j ACCEPT" in script


def test_allowlist_rejects_malformed_endpoint(mock_runtime):
    from bubble.network import apply_allowlist

    with pytest.raises(ValueError, match="Invalid auth proxy IP"):
        apply_allowlist(
            mock_runtime,
            "c",
            ["github.com"],
            auth_proxy_endpoint=("not-an-ip", 7654),
        )
    with pytest.raises(ValueError, match="Invalid auth proxy port"):
        apply_allowlist(
            mock_runtime,
            "c",
            ["github.com"],
            auth_proxy_endpoint=("10.156.104.1", -1),
        )


# ---------------------------------------------------------------------------
# setup_auth_proxy bridge flow
# ---------------------------------------------------------------------------


@pytest.fixture
def bridge_endpoint():
    return {
        "tcp": {"host": "10.156.104.1", "port": 7654},
        "unix_socket": "/home/kim/.bubble/proxy-sockets/gh.sock",
        "version": 2,
    }


def test_setup_auth_proxy_bridge_skips_proxy_devices(mock_runtime, bridge_endpoint):
    """Bridge flow must not add bubble-auth-proxy or bubble-gh-proxy."""
    from bubble.github_token import setup_auth_proxy

    mock_runtime.launch("c", "base")
    mock_runtime._containers["c"].ipv4 = "10.156.104.42"

    with (
        patch("bubble.github_token._ensure_auth_proxy_endpoint", return_value=bridge_endpoint),
        patch("bubble.auth_proxy.generate_auth_token", return_value="tok-bridge"),
    ):
        result = setup_auth_proxy(mock_runtime, "c", "kim-em", "bubble", gh_enabled=True, config={})

    assert result is True
    added_proxy_devices = [
        call for call in mock_runtime.calls if call[0] == "add_device" and call[3] == "proxy"
    ]
    assert added_proxy_devices == [], "bridge flow added a proxy device"


def test_setup_auth_proxy_bridge_issues_ip_bound_token(mock_runtime, bridge_endpoint):
    """Token must carry the container's eth0 IPv4 in container_ip."""
    from bubble.github_token import setup_auth_proxy

    mock_runtime.launch("c", "base")
    mock_runtime._containers["c"].ipv4 = "10.156.104.42"

    with (
        patch("bubble.github_token._ensure_auth_proxy_endpoint", return_value=bridge_endpoint),
        patch("bubble.auth_proxy.generate_auth_token", return_value="tok-bridge") as mock_gen,
    ):
        setup_auth_proxy(mock_runtime, "c", "kim-em", "bubble", gh_enabled=False, config={})

    mock_gen.assert_called_once()
    kwargs = mock_gen.call_args.kwargs
    assert kwargs["container_ip"] == "10.156.104.42"


def test_setup_auth_proxy_bridge_adds_gh_socket_disk_mount(mock_runtime, bridge_endpoint):
    """When gh+rest are enabled, the host socket dir is bind-mounted."""
    from bubble.github_token import setup_auth_proxy

    mock_runtime.launch("c", "base")
    mock_runtime._containers["c"].ipv4 = "10.156.104.42"

    with (
        patch("bubble.github_token._ensure_auth_proxy_endpoint", return_value=bridge_endpoint),
        patch("bubble.auth_proxy.generate_auth_token", return_value="tok-bridge"),
        # Force REST+gh on.
        patch("bubble.github_token._resolve_rest_api", return_value=True),
    ):
        setup_auth_proxy(mock_runtime, "c", "kim-em", "bubble", gh_enabled=True, config={})

    disk_mounts = [c for c in mock_runtime.calls if c[0] == "add_disk"]
    socket_mounts = [c for c in disk_mounts if c[2] == "bubble-proxy-sockets"]
    assert len(socket_mounts) == 1
    name, device, src, dst, *_ = socket_mounts[0][1:]
    assert dst == "/run/bubble-proxy"


def test_setup_auth_proxy_bridge_git_config_uses_bridge_url(mock_runtime, bridge_endpoint):
    """The .gitconfig payload routes through the bridge URL, not 127.0.0.1."""
    from bubble.github_token import setup_auth_proxy

    mock_runtime.launch("c", "base")
    mock_runtime._containers["c"].ipv4 = "10.156.104.42"

    with (
        patch("bubble.github_token._ensure_auth_proxy_endpoint", return_value=bridge_endpoint),
        patch("bubble.auth_proxy.generate_auth_token", return_value="tok-bridge"),
    ):
        setup_auth_proxy(mock_runtime, "c", "kim-em", "bubble", gh_enabled=False, config={})

    exec_calls = [c for c in mock_runtime.calls if c[0] == "exec"]
    assert exec_calls, "no exec call recorded"
    payload = " ".join(str(c[2]) for c in exec_calls)
    assert "http://10.156.104.1:7654/git/" in payload
    assert "http://127.0.0.1:7654/git/" not in payload, "should not use loopback in bridge flow"


def test_token_persists_container_ip(tmp_path, monkeypatch):
    """generate_auth_token writes container_ip into the token store."""
    from bubble import auth_proxy

    monkeypatch.setattr(auth_proxy, "AUTH_PROXY_TOKENS", tmp_path / "tokens.json")
    token = auth_proxy.generate_auth_token("c", "kim-em", "bubble", container_ip="10.156.104.42")
    info = auth_proxy.AuthTokenRegistry().lookup(token)
    assert info is not None
    assert info["container_ip"] == "10.156.104.42"


def test_token_without_container_ip_is_legacy(tmp_path, monkeypatch):
    """Tokens minted without container_ip are still accepted (back-compat)."""
    from bubble import auth_proxy

    monkeypatch.setattr(auth_proxy, "AUTH_PROXY_TOKENS", tmp_path / "tokens.json")
    token = auth_proxy.generate_auth_token("c", "kim-em", "bubble")
    info = auth_proxy.AuthTokenRegistry().lookup(token)
    assert info is not None
    assert info["container_ip"] is None


def test_authenticate_rejects_ip_mismatch(tmp_path, monkeypatch):
    """_authenticate sends 403 when a TCP request's source IP doesn't match."""
    from bubble import auth_proxy

    monkeypatch.setattr(auth_proxy, "AUTH_PROXY_TOKENS", tmp_path / "tokens.json")
    token = auth_proxy.generate_auth_token("c", "kim-em", "bubble", container_ip="10.156.104.42")

    # Build a minimal handler-shaped stub with just enough surface to
    # exercise _authenticate.
    class StubHandler:
        token_registry = auth_proxy.AuthTokenRegistry()

        def __init__(self, source_ip, token):
            self.client_address = (source_ip, 12345)
            self.headers = {"X-Bubble-Token": token}
            self._errors = []

        def _get_container_token(self):
            return self.headers.get("X-Bubble-Token")

        def _send_error(self, code, msg):
            self._errors.append((code, msg))

    h = StubHandler("10.156.104.42", token)
    info = auth_proxy.AuthProxyHandler._authenticate(h)
    assert info is not None, "matching IP must authenticate"

    h_bad = StubHandler("10.156.104.99", token)
    info_bad = auth_proxy.AuthProxyHandler._authenticate(h_bad)
    assert info_bad is None
    assert h_bad._errors and h_bad._errors[0][0] == 403


def test_authenticate_skips_ip_check_for_unix_socket(tmp_path, monkeypatch):
    """Unix-socket peers (empty client_address) skip the IP check."""
    from bubble import auth_proxy

    monkeypatch.setattr(auth_proxy, "AUTH_PROXY_TOKENS", tmp_path / "tokens.json")
    token = auth_proxy.generate_auth_token("c", "kim-em", "bubble", container_ip="10.156.104.42")

    class StubHandler:
        token_registry = auth_proxy.AuthTokenRegistry()

        def __init__(self):
            # UnixHTTPServer.get_request returns ("", 0) so source IP is "".
            self.client_address = ("", 0)
            self.headers = {"X-Bubble-Token": token}
            self._errors = []

        def _get_container_token(self):
            return self.headers.get("X-Bubble-Token")

        def _send_error(self, code, msg):
            self._errors.append((code, msg))

    h = StubHandler()
    info = auth_proxy.AuthProxyHandler._authenticate(h)
    assert info is not None
    assert h._errors == []


def test_setup_auth_proxy_falls_back_to_legacy_when_no_endpoint(mock_runtime):
    """If the daemon hasn't written the v2 endpoint file, use the proxy-device path."""
    from bubble.github_token import setup_auth_proxy

    with (
        patch("bubble.github_token._ensure_auth_proxy_endpoint", return_value=None),
        patch("bubble.github_token._ensure_auth_proxy_running", return_value=7654),
        patch("bubble.auth_proxy.generate_auth_token", return_value="tok-legacy"),
    ):
        result = setup_auth_proxy(
            mock_runtime, "c", "kim-em", "bubble", gh_enabled=False, config={}
        )

    assert result is True
    proxy_devices = [c for c in mock_runtime.calls if c[0] == "add_device" and c[3] == "proxy"]
    assert any(c[2] == "bubble-auth-proxy" for c in proxy_devices), (
        "legacy fallback must add bubble-auth-proxy device"
    )
