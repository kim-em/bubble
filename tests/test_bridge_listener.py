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
    return {"tcp": {"host": "10.156.104.1", "port": 7654}, "version": 3}


def _launch(mock_runtime, *, container="c", ip="10.156.104.42"):
    mock_runtime.launch(container, "base")
    mock_runtime._containers[container].ipv4 = ip


def test_setup_auth_proxy_skips_proxy_devices(mock_runtime, bridge_endpoint):
    """Local setup must not add any incus proxy-type device (the leak source)."""
    from bubble.github_token import setup_auth_proxy

    _launch(mock_runtime)
    with (
        patch("bubble.github_token._ensure_auth_proxy_endpoint", return_value=bridge_endpoint),
        patch("bubble.auth_proxy.generate_auth_token", return_value="tok-bridge"),
    ):
        result = setup_auth_proxy(mock_runtime, "c", "kim-em", "bubble", gh_enabled=True, config={})

    assert result is True
    added_proxy_devices = [
        call for call in mock_runtime.calls if call[0] == "add_device" and call[3] == "proxy"
    ]
    assert added_proxy_devices == [], "local flow must not add a proxy device"


def test_setup_auth_proxy_gh_records_bridge_endpoint_no_disk(mock_runtime, bridge_endpoint):
    """gh setup records the bridge endpoint for the in-container forwarder
    and adds NO disk/proxy device."""
    from bubble.github_token import setup_auth_proxy

    _launch(mock_runtime)
    with (
        patch("bubble.github_token._ensure_auth_proxy_endpoint", return_value=bridge_endpoint),
        patch("bubble.auth_proxy.generate_auth_token", return_value="tok-bridge"),
        patch("bubble.github_token._resolve_rest_api", return_value=True),
    ):
        setup_auth_proxy(mock_runtime, "c", "kim-em", "bubble", gh_enabled=True, config={})

    # No devices of any kind for auth/gh (no disk mount, no proxy device).
    assert [c for c in mock_runtime.calls if c[0] == "add_disk"] == []
    assert [c for c in mock_runtime.calls if c[0] == "add_device"] == []
    # gh config writes the bridge endpoint + the forwarder socket path.
    payload = " ".join(str(c[2]) for c in mock_runtime.calls if c[0] == "exec")
    assert "/etc/bubble/gh/bridge" in payload
    assert "10.156.104.1:7654" in payload
    assert "/home/user/.bubble/gh.sock" in payload


def test_setup_auth_proxy_git_config_uses_bridge_url(mock_runtime, bridge_endpoint):
    """The .gitconfig payload routes through the bridge URL, not 127.0.0.1."""
    from bubble.github_token import setup_auth_proxy

    _launch(mock_runtime)
    with (
        patch("bubble.github_token._ensure_auth_proxy_endpoint", return_value=bridge_endpoint),
        patch("bubble.auth_proxy.generate_auth_token", return_value="tok-bridge"),
    ):
        setup_auth_proxy(mock_runtime, "c", "kim-em", "bubble", gh_enabled=False, config={})

    payload = " ".join(str(c[2]) for c in mock_runtime.calls if c[0] == "exec")
    assert "http://10.156.104.1:7654/git/" in payload
    assert "http://127.0.0.1:7654/git/" not in payload


def test_setup_auth_proxy_returns_false_without_endpoint(mock_runtime):
    """No daemon endpoint => fail (no legacy proxy-device fallback for local)."""
    from bubble.github_token import setup_auth_proxy

    _launch(mock_runtime)
    with patch("bubble.github_token._ensure_auth_proxy_endpoint", return_value=None):
        result = setup_auth_proxy(
            mock_runtime, "c", "kim-em", "bubble", gh_enabled=False, config={}
        )

    assert result is False
    proxy_devices = [c for c in mock_runtime.calls if c[0] == "add_device" and c[3] == "proxy"]
    assert proxy_devices == [], "local flow must never add a proxy device"


def test_token_has_no_ip_binding(tmp_path, monkeypatch):
    """Tokens are plain bearer credentials with no source-IP field."""
    from bubble import auth_proxy

    monkeypatch.setattr(auth_proxy, "AUTH_PROXY_TOKENS", tmp_path / "tokens.json")
    token = auth_proxy.generate_auth_token("c", "kim-em", "bubble")
    info = auth_proxy.AuthTokenRegistry().lookup(token)
    assert info is not None
    assert "container_ip" not in info
    assert info["owner"] == "kim-em"
    assert info["repo"] == "bubble"


def test_authenticate_accepts_valid_token_any_source(tmp_path, monkeypatch):
    """_authenticate validates the bearer token and ignores source address."""
    from bubble import auth_proxy

    monkeypatch.setattr(auth_proxy, "AUTH_PROXY_TOKENS", tmp_path / "tokens.json")
    token = auth_proxy.generate_auth_token("c", "kim-em", "bubble")

    class StubHandler:
        token_registry = auth_proxy.AuthTokenRegistry()

        def __init__(self, source_ip, tok):
            self.client_address = (source_ip, 12345)
            self.headers = {"X-Bubble-Token": tok}
            self._errors = []

        def _get_container_token(self):
            return self.headers.get("X-Bubble-Token")

        def _send_error(self, code, msg):
            self._errors.append((code, msg))

    # Any source IP with a valid token authenticates.
    for ip in ("10.156.104.42", "10.156.104.99", ""):
        h = StubHandler(ip, token)
        assert auth_proxy.AuthProxyHandler._authenticate(h) is not None
        assert h._errors == []

    # An unknown token is rejected.
    bad = StubHandler("10.156.104.42", "not-a-real-token")
    assert auth_proxy.AuthProxyHandler._authenticate(bad) is None
    assert bad._errors and bad._errors[0][0] == 403
