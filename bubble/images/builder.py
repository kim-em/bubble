"""Container image construction."""

import re
import subprocess
import time
from pathlib import Path

from ..config import DATA_DIR
from ..runtime.base import ContainerRuntime

VSCODE_COMMIT_FILE = DATA_DIR / "vscode-commit"

SCRIPTS_DIR = Path(__file__).parent / "scripts"

# Image hierarchy: name -> {"script": "...", "parent": "..."}
# Parent can be another image name (built recursively) or an Incus remote image.
IMAGES = {
    "base": {"script": "base.sh", "parent": "images:ubuntu/24.04"},
    "lean": {"script": "lean.sh", "parent": "base"},
}


def _get_bridge_dns_ip() -> str | None:
    """Get the IPv4 address of the default incus bridge (for DNS proxy workaround)."""
    cidr = _get_bridge_cidr()
    if cidr:
        return cidr.split("/")[0]
    return None


def _get_bridge_cidr() -> str | None:
    """Get the full CIDR of the incus bridge (e.g. '10.228.152.1/24')."""
    try:
        result = subprocess.run(
            ["incus", "network", "get", "incusbr0", "ipv4.address"],
            capture_output=True, text=True, timeout=5, stdin=subprocess.DEVNULL,
        )
        if result.returncode == 0:
            cidr = result.stdout.strip()
            if "/" in cidr:
                return cidr
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return None


def _container_has_ipv4(runtime: ContainerRuntime, name: str) -> bool:
    """Check if the container has an IPv4 address on eth0."""
    try:
        output = runtime.exec(name, ["ip", "-4", "addr", "show", "eth0"])
        return "inet " in output
    except Exception:
        return False


def _fix_ipv4_static(runtime: ContainerRuntime, name: str) -> bool:
    """Assign a static IPv4 when DHCP fails (e.g. NixOS nftables blocking bridge DHCP).

    Picks an address in the bridge subnet and configures it directly.
    Returns True if IPv4 was successfully configured.
    """
    cidr = _get_bridge_cidr()
    if not cidr:
        return False

    gateway = cidr.split("/")[0]
    prefix = cidr.split("/")[1]
    # Use .200 in the bridge subnet to avoid collisions with DHCP range
    parts = gateway.rsplit(".", 1)
    static_ip = f"{parts[0]}.200"

    try:
        runtime.exec(name, [
            "bash", "-c",
            f"ip addr replace {static_ip}/{prefix} dev eth0 && "
            f"ip route replace default via {gateway}",
        ])
        # Verify connectivity to gateway
        runtime.exec(name, ["ping", "-c1", "-W2", gateway])
        return True
    except Exception:
        return False


def _fix_dns_with_proxy(runtime: ContainerRuntime, name: str) -> bool:
    """Work around broken DNS by adding an incus proxy device for DNS.

    On some systems (e.g. NixOS with nftables), the firewall blocks DNS
    responses from dnsmasq on the bridge back to containers. An incus proxy
    device bypasses the kernel network stack entirely.

    Returns True if the fix was applied and DNS works.
    """
    dns_ip = _get_bridge_dns_ip()
    if not dns_ip:
        return False

    try:
        # Stop systemd-resolved so we can bind to 127.0.0.53:53
        runtime.exec(name, ["systemctl", "stop", "systemd-resolved"])
        runtime.exec(name, ["bash", "-c", "echo nameserver 127.0.0.53 > /etc/resolv.conf"])
        runtime.add_device(
            name, "dns-proxy", "proxy",
            connect=f"udp:{dns_ip}:53",
            listen="udp:127.0.0.53:53",
            bind="container",
        )
        runtime.add_device(
            name, "dns-proxy-tcp", "proxy",
            connect=f"tcp:{dns_ip}:53",
            listen="tcp:127.0.0.53:53",
            bind="container",
        )
        # Verify it works
        runtime.exec(name, ["timeout", "3", "getent", "hosts", "github.com"])
        return True
    except Exception:
        # Clean up on failure
        try:
            runtime.exec(name, ["systemctl", "start", "systemd-resolved"])
        except Exception:
            pass
        return False


def _wait_for_container(runtime: ContainerRuntime, name: str, timeout: int = 60):
    """Wait for a container to be ready, including network (IPv4 + DNS).

    Handles systems where the firewall blocks bridge DHCP and/or DNS
    (common on NixOS with nftables and bridge-nf-call-iptables=1).
    """
    # Phase 1: wait for container to be exec-able
    for _ in range(timeout):
        try:
            runtime.exec(name, ["true"])
            break
        except Exception:
            time.sleep(1)
    else:
        raise RuntimeError(f"Container '{name}' not exec-able after {timeout}s")

    # Phase 2: wait for IPv4 + DNS (give DHCP a chance first)
    for i in range(min(timeout, 15)):
        try:
            runtime.exec(name, ["timeout", "3", "getent", "hosts", "github.com"])
            return  # Everything works
        except Exception:
            time.sleep(1)

    # Phase 3: DHCP/DNS didn't come up — apply workarounds
    if not _container_has_ipv4(runtime, name):
        if _fix_ipv4_static(runtime, name):
            print("  IPv4 configured statically (DHCP blocked by firewall).")

    if _fix_dns_with_proxy(runtime, name):
        print("  DNS fixed via proxy (firewall blocking bridge DNS responses).")
        return

    # Final check
    try:
        runtime.exec(name, ["timeout", "3", "getent", "hosts", "github.com"])
        return
    except Exception:
        pass

    raise RuntimeError(f"Container '{name}' network not ready after {timeout}s")


def get_vscode_commit() -> str | None:
    """Get the VS Code commit hash from `code --version`. Returns None if unavailable."""
    try:
        result = subprocess.run(
            ["code", "--version"], capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            lines = result.stdout.strip().splitlines()
            if len(lines) >= 2 and re.fullmatch(r"[0-9a-f]{40}", lines[1]):
                return lines[1]
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return None


def build_image(runtime: ContainerRuntime, image_name: str):
    """Build any known image by name. Builds parent images recursively if needed."""
    if image_name not in IMAGES:
        available = ", ".join(IMAGES.keys())
        raise ValueError(f"Unknown image: {image_name}. Available: {available}")

    spec = IMAGES[image_name]
    parent = spec["parent"]

    # Ensure parent image exists (recursive for our own images)
    if parent in IMAGES and not runtime.image_exists(parent):
        build_image(runtime, parent)

    build_name = f"{image_name}-builder"
    print(f"Building {image_name} image...")

    # Clean up any leftover builder from a previous failed attempt
    try:
        runtime.delete(build_name, force=True)
    except Exception:
        pass

    # Launch from parent
    runtime.launch(build_name, parent)
    _wait_for_container(runtime, build_name)

    # Run setup script, injecting VS Code commit hash if available
    script = (SCRIPTS_DIR / spec["script"]).read_text()
    vscode_commit = get_vscode_commit()
    if vscode_commit:
        script = f"export VSCODE_COMMIT='{vscode_commit}'\n" + script
    runtime.exec(build_name, ["bash", "-c", script])

    # Publish as image
    runtime.stop(build_name)
    runtime.publish(build_name, image_name)
    runtime.delete(build_name)

    # Record the VS Code commit hash baked into the image
    if vscode_commit:
        VSCODE_COMMIT_FILE.parent.mkdir(parents=True, exist_ok=True)
        VSCODE_COMMIT_FILE.write_text(vscode_commit + "\n")

    print(f"{image_name} image built successfully.")


def build_lean_toolchain_image(runtime: ContainerRuntime, version: str):
    """Build a toolchain-specific Lean image (e.g. lean-v4.16.0).

    Launches from the base 'lean' image and installs one specific toolchain.
    """
    # Ensure base lean image exists
    if not runtime.image_exists("lean"):
        build_image(runtime, "lean")

    alias = f"lean-{version}"
    # Incus container names only allow alphanumeric + hyphens
    safe_version = version.replace(".", "-")
    build_name = f"lean-tc-{safe_version}-builder"
    print(f"Building {alias} image...")

    # Clean up any leftover builder from a previous failed attempt
    try:
        runtime.delete(build_name, force=True)
    except Exception:
        pass

    runtime.launch(build_name, "lean")
    try:
        _wait_for_container(runtime, build_name)

        script = (SCRIPTS_DIR / "lean-toolchain.sh").read_text()
        script = f"export LEAN_TOOLCHAIN='{version}'\n" + script
        runtime.exec(build_name, ["bash", "-c", script])

        runtime.stop(build_name)
        if runtime.image_exists(alias):
            runtime.image_delete(alias)
        runtime.publish(build_name, alias)
    finally:
        try:
            runtime.delete(build_name, force=True)
        except Exception:
            pass
        # Remove lock file so future builds can proceed
        lock_path = Path(f"/tmp/bubble-lean-{version}.lock")
        lock_path.unlink(missing_ok=True)

    print(f"{alias} image built successfully.")
