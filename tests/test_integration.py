"""Integration tests against real Incus containers.

These tests require:
- Incus installed and available
- lean-base image built (run: bubble images build lean-base)

Mark: @pytest.mark.integration
Skip locally with: pytest -m "not integration"
"""

import uuid

import pytest

from lean_bubbles.network import apply_allowlist, check_allowlist_active, remove_allowlist
from lean_bubbles.runtime.incus import IncusRuntime

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def runtime():
    """IncusRuntime instance (shared across all integration tests)."""
    rt = IncusRuntime()
    if not rt.is_available():
        pytest.skip("Incus not available")
    return rt


@pytest.fixture(scope="session")
def _check_lean_base(runtime):
    """Skip all integration tests if lean-base image isn't built."""
    if not runtime.image_exists("lean-base"):
        pytest.skip("lean-base image not built")


@pytest.fixture
def container(runtime, _check_lean_base):
    """Launch a fresh container from lean-base, delete on teardown."""
    name = f"ci-test-{uuid.uuid4().hex[:8]}"
    runtime.launch(name, "lean-base")
    # Wait for container to be ready
    for _ in range(15):
        try:
            runtime.exec(name, ["true"])
            break
        except Exception:
            import time

            time.sleep(0.5)
    yield name
    try:
        runtime.delete(name, force=True)
    except Exception:
        pass


@pytest.fixture
def container_with_allowlist(runtime, container):
    """Container with network allowlist applied (waits for DNS readiness)."""
    import time

    # Wait for DNS resolver to be available (needed for allowlist)
    for _ in range(20):
        try:
            resolver = runtime.exec(
                container,
                ["bash", "-c", "grep -m1 nameserver /etc/resolv.conf | awk '{print $2}'"],
            ).strip()
            if resolver:
                break
        except Exception:
            pass
        time.sleep(0.5)
    apply_allowlist(runtime, container, ["github.com", "*.githubusercontent.com"])
    return container


# ---------------------------------------------------------------------------
# Security: No Privilege Escalation
# ---------------------------------------------------------------------------


class TestNoPrivilegeEscalation:
    def test_lean_user_cannot_sudo(self, runtime, container):
        """lean user has no sudo access."""
        with pytest.raises(RuntimeError):
            runtime.exec(
                container,
                [
                    "su",
                    "-",
                    "lean",
                    "-c",
                    "sudo whoami",
                ],
            )

    def test_password_locked(self, runtime, container):
        """lean user's password is locked (! or * prefix in shadow)."""
        shadow = runtime.exec(
            container,
            [
                "grep",
                "^lean:",
                "/etc/shadow",
            ],
        )
        # Locked password has ! or * after the first colon
        password_field = shadow.split(":")[1]
        assert password_field.startswith("!") or password_field.startswith("*")

    def test_no_sudoers_file(self, runtime, container):
        """No sudoers entry for lean user."""
        with pytest.raises(RuntimeError):
            runtime.exec(
                container,
                [
                    "cat",
                    "/etc/sudoers.d/lean",
                ],
            )


# ---------------------------------------------------------------------------
# Security: Network Allowlist
# ---------------------------------------------------------------------------


class TestNetworkAllowlist:
    def test_iptables_default_deny(self, runtime, container_with_allowlist):
        """OUTPUT chain default policy is DROP."""
        output = runtime.exec(
            container_with_allowlist,
            [
                "iptables",
                "-L",
                "OUTPUT",
                "-n",
            ],
        )
        assert "policy DROP" in output

    def test_ipv6_blocked(self, runtime, container_with_allowlist):
        """IPv6 OUTPUT is DROP."""
        output = runtime.exec(
            container_with_allowlist,
            [
                "ip6tables",
                "-L",
                "OUTPUT",
                "-n",
            ],
        )
        assert "policy DROP" in output

    def test_no_outbound_ssh_rule(self, runtime, container_with_allowlist):
        """No iptables rules for port 22."""
        output = runtime.exec(
            container_with_allowlist,
            [
                "iptables",
                "-L",
                "OUTPUT",
                "-n",
            ],
        )
        assert "dpt:22" not in output

    def test_dns_restricted_to_resolver(self, runtime, container_with_allowlist):
        """DNS rules only target the container's resolver."""
        # Use -S for machine-readable output (shows --dport 53)
        output = runtime.exec(
            container_with_allowlist,
            [
                "iptables",
                "-S",
                "OUTPUT",
            ],
        )
        resolver = runtime.exec(
            container_with_allowlist,
            [
                "bash",
                "-c",
                "grep -m1 nameserver /etc/resolv.conf | awk '{print $2}'",
            ],
        ).strip()
        # All --dport 53 rules should reference the resolver
        lines = [ln for ln in output.splitlines() if "--dport 53" in ln]
        assert len(lines) > 0, "Expected DNS rules"
        for line in lines:
            assert resolver in line, f"DNS rule targets non-resolver: {line}"

    def test_blocked_destination_unreachable(self, runtime, container_with_allowlist):
        """Connections to non-allowlisted hosts are blocked."""
        result = runtime.exec(
            container_with_allowlist,
            [
                "bash",
                "-c",
                "timeout 3 bash -c 'echo > /dev/tcp/1.1.1.1/80' 2>&1 || echo BLOCKED",
            ],
        )
        assert "BLOCKED" in result

    def test_check_allowlist_active(self, runtime, container_with_allowlist):
        """check_allowlist_active returns True when allowlist is applied."""
        assert check_allowlist_active(runtime, container_with_allowlist)

    def test_allowlisted_destination_has_accept_rules(self, runtime, container_with_allowlist):
        """iptables has ACCEPT rules for allowlisted domain IPs."""
        output = runtime.exec(
            container_with_allowlist,
            ["iptables", "-S", "OUTPUT"],
        )
        # The allowlist resolves domains to IPs and adds -d <ip> -j ACCEPT rules.
        # Filter out loopback, ESTABLISHED, and DNS rules to find domain IP rules.
        ip_accept = [
            ln
            for ln in output.splitlines()
            if "-j ACCEPT" in ln and "-d " in ln and "--dport 53" not in ln and "-o lo" not in ln
        ]
        assert len(ip_accept) > 0, f"Expected ACCEPT rules for domain IPs, got:\n{output}"

    def test_check_allowlist_inactive_by_default(self, runtime, container):
        """check_allowlist_active returns False on a fresh container."""
        assert not check_allowlist_active(runtime, container)


# ---------------------------------------------------------------------------
# Security: SSH Configuration
# ---------------------------------------------------------------------------


class TestSSHConfiguration:
    def test_password_auth_disabled(self, runtime, container):
        """SSH password authentication is disabled."""
        config = runtime.exec(
            container,
            [
                "bash",
                "-c",
                "grep -E '^PasswordAuthentication' /etc/ssh/sshd_config || echo 'NOT_SET'",
            ],
        )
        if "NOT_SET" not in config:
            assert "no" in config.lower()

    def test_root_login_disabled(self, runtime, container):
        """SSH root login is disabled."""
        config = runtime.exec(
            container,
            [
                "bash",
                "-c",
                "grep -E '^PermitRootLogin' /etc/ssh/sshd_config || echo 'NOT_SET'",
            ],
        )
        if "NOT_SET" not in config:
            assert "no" in config.lower()


# ---------------------------------------------------------------------------
# Functional: Container Lifecycle
# ---------------------------------------------------------------------------


class TestContainerLifecycle:
    def test_launch_exec_delete(self, runtime, _check_lean_base):
        """Can launch a container, execute commands, and delete it."""
        name = f"ci-test-lifecycle-{uuid.uuid4().hex[:8]}"
        try:
            runtime.launch(name, "lean-base")
            output = runtime.exec(name, ["echo", "hello"])
            assert "hello" in output
        finally:
            try:
                runtime.delete(name, force=True)
            except Exception:
                pass

    def test_lean_user_exists(self, runtime, container):
        """Container has a 'lean' user."""
        output = runtime.exec(container, ["id", "lean"])
        assert "uid=" in output

    def test_network_allowlist_apply_remove(self, runtime, container):
        """Can apply and remove network allowlist."""
        apply_allowlist(runtime, container, ["github.com"])
        assert check_allowlist_active(runtime, container)

        remove_allowlist(runtime, container)
        output = runtime.exec(
            container,
            [
                "iptables",
                "-L",
                "OUTPUT",
                "-n",
            ],
        )
        assert "policy ACCEPT" in output
