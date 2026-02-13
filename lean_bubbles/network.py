"""Network allowlisting for containers.

Restricts container network access to only allowed domains.
Uses iptables rules inside the container since Incus network ACLs
may not be available in all configurations (e.g., through Colima).
"""

import subprocess

from .runtime.base import ContainerRuntime


def apply_allowlist(runtime: ContainerRuntime, container: str, domains: list[str]):
    """Apply network allowlist to a container using iptables.

    Resolves domain names to IPs and creates iptables rules that only
    allow outbound connections to those IPs. DNS (port 53) is always allowed.
    """
    # Install iptables if not present
    try:
        runtime.exec(container, ["which", "iptables"])
    except Exception:
        runtime.exec(container, [
            "bash", "-c",
            "apt-get update -qq && apt-get install -y -qq iptables < /dev/null",
        ])

    # Build the allowlist script
    # We resolve domains and allow their IPs, plus always allow DNS and localhost
    script = _build_allowlist_script(domains)
    runtime.exec(container, ["bash", "-c", script])


def remove_allowlist(runtime: ContainerRuntime, container: str):
    """Remove network restrictions from a container."""
    runtime.exec(container, [
        "bash", "-c",
        "iptables -F OUTPUT 2>/dev/null; iptables -P OUTPUT ACCEPT 2>/dev/null; true",
    ])


def _build_allowlist_script(domains: list[str]) -> str:
    """Build a shell script that sets up iptables allowlist rules."""
    lines = [
        "#!/bin/bash",
        "set -e",
        "",
        "# Flush existing OUTPUT rules",
        "iptables -F OUTPUT 2>/dev/null || true",
        "",
        "# Allow loopback",
        "iptables -A OUTPUT -o lo -j ACCEPT",
        "",
        "# Allow established connections",
        "iptables -A OUTPUT -m state --state ESTABLISHED,RELATED -j ACCEPT",
        "",
        "# Allow DNS (needed to resolve allowed domains)",
        "iptables -A OUTPUT -p udp --dport 53 -j ACCEPT",
        "iptables -A OUTPUT -p tcp --dport 53 -j ACCEPT",
        "",
        "# Allow SSH (for VSCode Remote SSH)",
        "iptables -A OUTPUT -p tcp --dport 22 -j ACCEPT",
        "iptables -A OUTPUT -p tcp --sport 22 -j ACCEPT",
        "",
        "# Resolve and allow each domain",
    ]

    for domain in domains:
        # Handle wildcard domains (*.example.com → just resolve example.com)
        resolve_domain = domain.lstrip("*.")
        lines.append(f"for ip in $(getent ahosts {resolve_domain} 2>/dev/null | awk '{{print $1}}' | sort -u); do")
        lines.append(f"  iptables -A OUTPUT -d $ip -j ACCEPT")
        lines.append("done")

    lines.extend([
        "",
        "# Default: drop everything else",
        "iptables -P OUTPUT DROP",
        "",
        "echo 'Network allowlist applied.'",
    ])

    return "\n".join(lines)


def check_allowlist_active(runtime: ContainerRuntime, container: str) -> bool:
    """Check if network allowlisting is active on a container."""
    try:
        output = runtime.exec(container, [
            "bash", "-c",
            "iptables -L OUTPUT -n 2>/dev/null | grep -c DROP || echo 0",
        ])
        return int(output.strip()) > 0
    except Exception:
        return False
