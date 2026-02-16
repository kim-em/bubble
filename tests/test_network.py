"""Tests for network allowlisting — security-critical."""

import pytest

from bubble.network import _DOMAIN_RE, _build_allowlist_script, apply_allowlist


class TestBuildAllowlistScript:
    """Verify the generated iptables script has correct security properties."""

    def test_ipv6_blocked(self):
        script = _build_allowlist_script(["github.com"])
        assert "ip6tables -P OUTPUT DROP" in script

    def test_ipv4_default_deny(self):
        script = _build_allowlist_script(["github.com"])
        assert "iptables -P OUTPUT DROP" in script

    def test_uses_ahostsv4_not_ahosts(self):
        script = _build_allowlist_script(["github.com"])
        assert "getent ahostsv4" in script
        # Should not have bare "getent ahosts " (without v4)
        lines = script.splitlines()
        for line in lines:
            if "getent" in line:
                assert "ahostsv4" in line

    def test_dns_restricted_to_resolver(self):
        script = _build_allowlist_script(["github.com"])
        assert "RESOLVER=" in script
        assert "$RESOLVER" in script
        assert "dport 53" in script

    def test_no_ssh_rules(self):
        script = _build_allowlist_script(["github.com"])
        assert "--dport 22" not in script
        assert "--sport 22" not in script

    def test_domain_appears_in_getent_call(self):
        script = _build_allowlist_script(["github.com"])
        assert "getent ahostsv4 github.com" in script

    def test_uses_cidr_blocks_not_individual_ips(self):
        """CDN domains rotate IPs; /24 CIDR blocks handle this."""
        script = _build_allowlist_script(["github.com"])
        assert ".0/24" in script
        # Should not have bare "$ip" rules (old individual-IP approach)
        assert "-d $ip " not in script

    def test_wildcard_uses_cidr_blocks(self):
        script = _build_allowlist_script(["*.example.com"])
        assert ".0/24" in script
        assert "-d $cidr " in script

    def test_wildcard_resolves_base_domain(self):
        script = _build_allowlist_script(["*.example.com"])
        assert "getent ahostsv4 example.com" in script

    def test_loopback_allowed(self):
        script = _build_allowlist_script(["github.com"])
        assert "-o lo -j ACCEPT" in script

    def test_established_connections_allowed(self):
        script = _build_allowlist_script(["github.com"])
        assert "ESTABLISHED,RELATED" in script


class TestDomainValidation:
    """Verify domain regex rejects injection attempts."""

    @pytest.mark.parametrize(
        "domain",
        [
            "github.com",
            "*.githubusercontent.com",
            "releases.lean-lang.org",
            "objects.githubusercontent.com",
        ],
    )
    def test_valid_domains_accepted(self, domain):
        assert _DOMAIN_RE.match(domain)

    @pytest.mark.parametrize(
        "domain",
        [
            "evil.com; rm -rf /",
            "foo$(whoami)",
            "a b",
            "",
            "test`id`",
            "foo\nbar",
            "domain|cat /etc/passwd",
        ],
    )
    def test_injection_attempts_rejected(self, domain):
        assert not _DOMAIN_RE.match(domain)


def test_apply_allowlist_rejects_invalid_domain(mock_runtime):
    with pytest.raises(ValueError, match="Invalid domain"):
        apply_allowlist(mock_runtime, "test", ["evil.com; rm -rf /"])


def test_apply_allowlist_calls_exec(mock_runtime):
    apply_allowlist(mock_runtime, "test", ["github.com"])
    exec_calls = [c for c in mock_runtime.calls if c[0] == "exec"]
    assert len(exec_calls) == 1
    assert exec_calls[0][1] == "test"
