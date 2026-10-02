"""Discover the incus bridge gateway IP and validate it.

On Linux, the auth proxy binds to the gateway IP of incusbr0 so that
containers can reach it directly without per-container forkproxy
devices. The gateway IP is the address assigned to the host's
incusbr0 interface (typically 10.156.104.1/24 in a default
incus install).

Discovery cross-checks two sources to guard against misconfiguration
or hostile incus state:

  1. ``ip -j addr show incusbr0`` — what the kernel actually has
     bound on the host interface.
  2. ``incus network show incusbr0`` — what incus's configuration
     says the network's ipv4 gateway is.

If they disagree, or if the interface doesn't exist, callers should
fail closed: the daemon refuses to start, and bubble falls back to
the legacy proxy-device path.
"""

from __future__ import annotations

import json
import os
import re
import subprocess

BRIDGE_INTERFACE = "incusbr0"

# A Linux interface name: at most 15 characters, none of them a slash or whitespace.
_INTERFACE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,14}")


def bridge_interface() -> str:
    """The bridge this user's containers attach to, which the auth proxy and the artifact cache must
    listen on: the network of the default profile's ``eth0`` in the current Incus project.

    An administrator works in the ``default`` project, whose containers attach to ``incusbr0``. A user
    who reaches Incus through ``incus-user`` (group ``incus`` rather than ``incus-admin``) is confined
    to a restricted project ``user-<uid>`` whose only network is its own bridge, ``incusbr-<uid>``.
    A listener bound to ``incusbr0`` is unreachable from there, since it is restricted to that
    interface (``SO_BINDTODEVICE``). ``BUBBLE_INCUS_BRIDGE`` overrides the lookup; ``incusbr0`` is the
    answer whenever the profile cannot be read.
    """
    override = os.environ.get("BUBBLE_INCUS_BRIDGE", "").strip()
    if override:
        return override
    try:
        result = subprocess.run(
            ["incus", "profile", "device", "get", "default", "eth0", "network"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return BRIDGE_INTERFACE
    name = result.stdout.strip()
    if result.returncode == 0 and _INTERFACE_NAME.fullmatch(name):
        return name
    return BRIDGE_INTERFACE


class BridgeDiscoveryError(RuntimeError):
    """Raised when we cannot reliably determine the bridge gateway IP."""


def _ip_addr_show_ipv4(interface: str) -> str | None:
    """Return the first IPv4 address assigned to *interface*, or None."""
    try:
        result = subprocess.run(
            ["ip", "-j", "addr", "show", "dev", interface],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    for entry in data:
        for addr in entry.get("addr_info", []):
            if addr.get("family") == "inet":
                return addr.get("local")
    return None


def _incus_network_ipv4_gateway(interface: str) -> str | None:
    """Return the ipv4.address (cidr) of an incus managed network, or None."""
    try:
        result = subprocess.run(
            ["incus", "network", "show", interface],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    # YAML-ish output; we only need the ipv4.address line.
    for line in result.stdout.splitlines():
        line = line.strip()
        if line.startswith("ipv4.address:"):
            value = line.split(":", 1)[1].strip()
            # Strip CIDR suffix if present (e.g. "10.156.104.1/24" -> "10.156.104.1")
            return value.split("/", 1)[0]
    return None


def bridge_gateway_ipv4(interface: str | None = None) -> str:
    """Return the validated IPv4 gateway address of the incus bridge.

    Cross-checks the kernel-bound address against incus's configured
    network gateway. Raises :class:`BridgeDiscoveryError` if the
    interface isn't present, lacks an IPv4 address, or the two
    sources disagree.
    """
    interface = interface or bridge_interface()
    kernel_ip = _ip_addr_show_ipv4(interface)
    if kernel_ip is None:
        raise BridgeDiscoveryError(
            f"Interface {interface!r} has no IPv4 address (is incus installed?)"
        )
    incus_ip = _incus_network_ipv4_gateway(interface)
    if incus_ip is None:
        # Best-effort fallback: kernel says it's there, incus is silent.
        # Could be permissions or a non-default incus setup. Trust the
        # kernel side but warn.
        return kernel_ip
    if incus_ip != kernel_ip:
        raise BridgeDiscoveryError(
            f"Inconsistent bridge IP: kernel says {kernel_ip!r}, "
            f"incus says {incus_ip!r}. Refusing to bind."
        )
    return kernel_ip
