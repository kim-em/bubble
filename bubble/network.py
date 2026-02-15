"""Network allowlisting for containers.

Restricts container network access to only allowed domains.
Uses iptables rules inside the container since Incus network ACLs
may not be available in all configurations (e.g., through Colima).

Security notes:
- Rules are applied by incus exec (as root), not by the lean user
- The lean user has no sudo, so cannot modify iptables rules
- IPv6 is blocked entirely via ip6tables
- DNS is restricted to the container's configured resolver only
- Outbound SSH is NOT allowed (VSCode uses incus exec ProxyCommand)
"""

import re

from .runtime.base import ContainerRuntime

# Valid domain pattern for allowlist entries
_DOMAIN_RE = re.compile(r"^[a-zA-Z0-9.*-]+$")


def apply_allowlist(runtime: ContainerRuntime, container: str, domains: list[str]):
    """Apply network allowlist to a container using iptables.

    Resolves domain names to IPs and creates iptables rules that only
    allow outbound connections to those IPs.
    """
    # Validate domains to prevent shell injection
    for domain in domains:
        if not _DOMAIN_RE.match(domain):
            raise ValueError(f"Invalid domain in allowlist: {domain!r}")

    # Build the allowlist script
    script = _build_allowlist_script(domains)
    runtime.exec(container, ["bash", "-c", script])


def remove_allowlist(runtime: ContainerRuntime, container: str):
    """Remove network restrictions from a container."""
    runtime.exec(
        container,
        [
            "bash",
            "-c",
            "iptables -F OUTPUT 2>/dev/null; iptables -P OUTPUT ACCEPT 2>/dev/null; "
            "ip6tables -F OUTPUT 2>/dev/null; ip6tables -P OUTPUT ACCEPT 2>/dev/null; true",
        ],
    )


def _build_allowlist_script(domains: list[str]) -> str:
    """Build a shell script that sets up iptables allowlist rules."""
    lines = [
        "#!/bin/bash",
        "set -e",
        "",
        "# --- IPv6: block entirely ---",
        "ip6tables -F OUTPUT 2>/dev/null || true",
        "ip6tables -A OUTPUT -o lo -j ACCEPT",
        "ip6tables -P OUTPUT DROP",
        "",
        "# --- IPv4 ---",
        "# Temporarily allow all output so DNS resolution works during setup",
        "iptables -P OUTPUT ACCEPT",
        "iptables -F OUTPUT 2>/dev/null || true",
        "",
        "# Allow loopback",
        "iptables -A OUTPUT -o lo -j ACCEPT",
        "",
        "# Allow established connections",
        "iptables -A OUTPUT -m state --state ESTABLISHED,RELATED -j ACCEPT",
        "",
        "# Allow DNS to container's configured resolver (stub)",
        "RESOLVER=$(grep -m1 nameserver /etc/resolv.conf | awk '{print $2}')",
        'if [ -n "$RESOLVER" ]; then',
        "  iptables -A OUTPUT -d $RESOLVER -p udp --dport 53 -j ACCEPT",
        "  iptables -A OUTPUT -d $RESOLVER -p tcp --dport 53 -j ACCEPT",
        "fi",
        "",
        "# Allow DNS to upstream servers (systemd-resolved forwards to these)",
        "for UPSTREAM in $(resolvectl dns 2>/dev/null"
        " | awk -F: '{print $2}'"
        " | grep -oE '[0-9]+\\.[0-9]+\\.[0-9]+\\.[0-9]+'); do",
        "  iptables -A OUTPUT -d $UPSTREAM -p udp --dport 53 -j ACCEPT",
        "  iptables -A OUTPUT -d $UPSTREAM -p tcp --dport 53 -j ACCEPT",
        "done",
        "",
        "# Resolve and allow each domain (IPv4 only)",
    ]

    for domain in domains:
        if domain.startswith("*."):
            # Wildcard domains: resolve the base domain, warn if it has no A record
            resolve_domain = domain[2:]
            lines.append(f"IPS=$(getent ahostsv4 {resolve_domain} 2>/dev/null"
                         " | awk '{print $1}' | sort -u)")
            lines.append('if [ -z "$IPS" ]; then')
            lines.append(f'  echo "Warning: wildcard domain {domain} did not resolve.'
                         f' Use explicit subdomains instead." >&2')
            lines.append("else")
            lines.append("  for ip in $IPS; do")
            lines.append("    iptables -A OUTPUT -d $ip -j ACCEPT")
            lines.append("  done")
            lines.append("fi")
        else:
            resolve_domain = domain
            lines.append(
                f"for ip in $(getent ahostsv4 {resolve_domain} 2>/dev/null "
                f"| awk '{{print $1}}' | sort -u); do"
            )
            lines.append("  iptables -A OUTPUT -d $ip -j ACCEPT")
            lines.append("done")

    lines.extend(
        [
            "",
            "# Default: drop everything else",
            "iptables -P OUTPUT DROP",
            "",
            "echo 'Network allowlist applied.'",
        ]
    )

    return "\n".join(lines)


def check_allowlist_active(runtime: ContainerRuntime, container: str) -> bool:
    """Check if network allowlisting is active on a container."""
    try:
        output = runtime.exec(
            container,
            [
                "bash",
                "-c",
                "iptables -L OUTPUT -n 2>/dev/null | grep -c DROP || echo 0",
            ],
        )
        return int(output.strip()) > 0
    except Exception:
        return False
