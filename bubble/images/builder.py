"""Container image construction."""

import fcntl
import hashlib
import re
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path

from ..config import DATA_DIR, load_config
from ..runtime.base import ContainerRuntime
from ..tools import combined_tool_script, resolve_tools, tools_hash

VSCODE_COMMIT_FILE = DATA_DIR / "vscode-commit"
TOOLS_HASH_FILE = DATA_DIR / "tools-hash"
CUSTOMIZE_SCRIPT = DATA_DIR / "customize.sh"
CUSTOMIZE_HASH_FILE = DATA_DIR / "customize-hash"

SCRIPTS_DIR = Path(__file__).parent / "scripts"

BUILD_LOCK_DIR = Path("/tmp/bubble-build-locks")


@contextmanager
def _build_lock(image_name: str):
    """Acquire an exclusive file lock for an image build.

    Prevents concurrent builds of the same image from racing on the
    shared builder container name. If another build is in progress,
    this blocks until it completes.
    """
    BUILD_LOCK_DIR.mkdir(parents=True, exist_ok=True)
    lock_path = BUILD_LOCK_DIR / f"{image_name}.lock"
    fd = lock_path.open("w")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        fd.close()


def is_build_locked(image_name: str) -> bool:
    """Check if an image build is currently in progress (non-blocking).

    Used by background spawn paths to avoid launching redundant processes
    when another build of the same image is already running.
    """
    BUILD_LOCK_DIR.mkdir(parents=True, exist_ok=True)
    lock_path = BUILD_LOCK_DIR / f"{image_name}.lock"
    try:
        fd = lock_path.open("w")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(fd, fcntl.LOCK_UN)
            return False
        except OSError:
            return True
        finally:
            fd.close()
    except OSError:
        return False


# Image hierarchy: name -> {"script": "...", "parent": "..."}
# Parent can be another image name (built recursively) or an Incus remote image.
# Editors (vscode, emacs, neovim) and elan are installed as pluggable tools on
# the base image, eliminating the need for editor-specific image variants.
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
            capture_output=True,
            text=True,
            timeout=5,
            stdin=subprocess.DEVNULL,
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
        runtime.exec(
            name,
            [
                "bash",
                "-c",
                f"ip addr replace {static_ip}/{prefix} dev eth0 && "
                f"ip route replace default via {gateway}",
            ],
        )
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
            name,
            "dns-proxy",
            "proxy",
            connect=f"udp:{dns_ip}:53",
            listen="udp:127.0.0.53:53",
            bind="container",
        )
        runtime.add_device(
            name,
            "dns-proxy-tcp",
            "proxy",
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
        result = subprocess.run(["code", "--version"], capture_output=True, text=True, timeout=5)
        if result.returncode == 0:
            lines = result.stdout.strip().splitlines()
            if len(lines) >= 2 and re.fullmatch(r"[0-9a-f]{40}", lines[1]):
                return lines[1]
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return None


def is_builder_container(name: str) -> bool:
    """Check if a container name matches the builder naming pattern.

    Builder containers are temporary containers created during image builds,
    named ``{image_name}-builder``. They should be cleaned up after the build
    completes, but may be left behind if the build is interrupted.

    Only matches known image builder names (from IMAGES keys) and the lean
    toolchain pattern (lean-*-builder), not arbitrary names ending in -builder.
    """
    if not name.endswith("-builder"):
        return False
    prefix = name[: -len("-builder")]
    # Known static image builders (e.g. "base-builder", "lean-builder")
    if prefix in IMAGES:
        return True
    # Lean toolchain builders (e.g. "lean-v4-16-0-builder")
    if re.match(r"^lean-v\d+", prefix):
        return True
    return False


def _cleanup_builder(runtime: ContainerRuntime, build_name: str):
    """Ensure no leftover builder container exists from a previous failed attempt."""
    try:
        runtime.delete(build_name, force=True)
    except Exception:
        pass

    # Verify the container is actually gone before proceeding
    if any(c.name == build_name for c in runtime.list_containers()):
        raise RuntimeError(
            f"Cannot remove leftover builder container '{build_name}'. Please delete it manually."
        )


def _collect_derived_images(base_name: str) -> list[str]:
    """Collect all images in IMAGES that transitively derive from base_name."""
    result = []
    direct = [name for name, spec in IMAGES.items() if spec["parent"] == base_name]
    for name in direct:
        result.append(name)
        result.extend(_collect_derived_images(name))
    return result


def _is_toolchain_alias(alias: str, purged_names: set[str]) -> bool:
    """Check if an alias is a dynamic toolchain image derived from a purged lean image.

    Toolchain aliases follow the pattern: <base>-v<digits>... where <base> is
    a purged lean-family image. We require a digit after 'v' to avoid false
    matches on unrelated images.
    """
    for name in purged_names:
        if name == "lean":
            prefix = "lean-v"
        elif name.startswith("lean-"):
            prefix = f"{name}-v"
        else:
            continue
        if alias.startswith(prefix) and len(alias) > len(prefix) and alias[len(prefix)].isdigit():
            return True
    return False


def _collect_dynamic_toolchain_aliases(
    runtime: ContainerRuntime, purged_names: set[str]
) -> list[str]:
    """Find dynamic toolchain image aliases that derive from purged lean images.

    Dynamic toolchain images (e.g. lean-v4.16.0, lean-emacs-v4.16.0) are not
    in IMAGES and must be discovered by scanning existing image aliases.
    """
    # Only look for toolchain images if a lean-family image is being purged
    if not any(n == "lean" or n.startswith("lean-") for n in purged_names):
        return []

    aliases = []
    try:
        for img in runtime.list_images():
            for alias_entry in img.get("aliases", []):
                alias = alias_entry["name"]
                if _is_toolchain_alias(alias, purged_names):
                    aliases.append(alias)
    except Exception:
        pass
    return aliases


def _purge_derived_images(runtime: ContainerRuntime, base_name: str):
    """Delete images that derive from base_name so they rebuild from the fresh base.

    When the base image is rebuilt (e.g. with new tools), derived images like
    lean are stale snapshots. Deleting them forces a rebuild on next use.

    Walks the full dependency tree (not just direct children) and also purges
    dynamic toolchain images (lean-v4.x.y, etc.).
    """
    static_derived = _collect_derived_images(base_name)

    # Also find dynamic toolchain images that derive from purged lean images
    dynamic_aliases = _collect_dynamic_toolchain_aliases(runtime, set(static_derived))

    for name in static_derived + dynamic_aliases:
        if runtime.image_exists(name):
            try:
                runtime.image_delete(name)
                print(f"  Deleted derived image '{name}' (will rebuild on next use).")
            except Exception:
                pass  # Best-effort; may fail if in use


def _install_tools_if_base(
    runtime: ContainerRuntime, build_name: str, image_name: str
) -> list[str] | None:
    """Install configured tools into a builder container if this is the base image.

    Tools are only installed on the 'base' image since all other images
    derive from it and inherit the tools automatically. This includes
    editors (vscode, emacs, neovim) and language tools (elan).

    Returns the list of enabled tools if tools were installed, None otherwise.
    """
    if image_name != "base":
        return None
    config = load_config()
    enabled = resolve_tools(config)
    if not enabled:
        return enabled
    script = combined_tool_script(enabled)
    if script:
        # Inject VS Code commit hash for the vscode tool script
        vscode_commit = get_vscode_commit()
        if vscode_commit and "vscode" in enabled:
            script = f"export VSCODE_COMMIT='{vscode_commit}'\n" + script
        print(f"  Installing tools: {', '.join(enabled)}")
        runtime.exec(build_name, ["bash", "-c", script])
    return enabled


def customize_hash() -> str | None:
    """Compute a hash of the user customization script, or None if it doesn't exist."""
    if not CUSTOMIZE_SCRIPT.exists():
        return None
    return hashlib.sha256(CUSTOMIZE_SCRIPT.read_bytes()).hexdigest()[:16]


def _run_customize_script(runtime: ContainerRuntime, build_name: str):
    """Run the user customization script (~/.bubble/customize.sh) if it exists.

    The script runs as root inside the builder container as the final
    build step, so it can apt-get install, copy dotfiles, etc.
    """
    if not CUSTOMIZE_SCRIPT.exists():
        return
    print("  Running user customization script...")
    script = CUSTOMIZE_SCRIPT.read_text()
    runtime.exec(build_name, ["bash", "-c", script])


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

    # Acquire exclusive lock to prevent concurrent builds of the same image.
    # After acquiring, re-check whether the image was built by another process.
    with _build_lock(image_name):
        if runtime.image_exists(image_name):
            print(f"{image_name} image already built (by concurrent process).")
            return

        build_name = f"{image_name}-builder"
        print(f"Building {image_name} image...")

        # Clean up any leftover builder from a previous failed attempt
        _cleanup_builder(runtime, build_name)

        # Launch from parent
        runtime.launch(build_name, parent)
        try:
            _wait_for_container(runtime, build_name)

            # Run setup script
            script = (SCRIPTS_DIR / spec["script"]).read_text()
            runtime.exec(build_name, ["bash", "-c", script])

            # Install configured tools (only on base image — derived images inherit them)
            enabled_tools = _install_tools_if_base(runtime, build_name, image_name)

            # Run user customization script as the final build step
            _run_customize_script(runtime, build_name)

            # Publish as image
            runtime.stop(build_name)
            runtime.publish(build_name, image_name)
        finally:
            try:
                runtime.delete(build_name, force=True)
            except Exception:
                pass

        # Record the VS Code commit hash baked into the image (when vscode is a tool)
        if enabled_tools and "vscode" in enabled_tools:
            vc = get_vscode_commit()
            if vc:
                VSCODE_COMMIT_FILE.parent.mkdir(parents=True, exist_ok=True)
                VSCODE_COMMIT_FILE.write_text(vc + "\n")

        # Record the tools hash baked into the image and purge stale derived images
        if enabled_tools is not None:
            TOOLS_HASH_FILE.parent.mkdir(parents=True, exist_ok=True)
            TOOLS_HASH_FILE.write_text(tools_hash(enabled_tools) + "\n")
            _purge_derived_images(runtime, image_name)

        # Record the customize script hash (only on base — derived images inherit it)
        if image_name == "base":
            c_hash = customize_hash()
            if c_hash:
                CUSTOMIZE_HASH_FILE.parent.mkdir(parents=True, exist_ok=True)
                CUSTOMIZE_HASH_FILE.write_text(c_hash + "\n")
            else:
                CUSTOMIZE_HASH_FILE.unlink(missing_ok=True)

        print(f"{image_name} image built successfully.")


def build_lean_toolchain_image(
    runtime: ContainerRuntime, version: str, base_lean_image: str = "lean"
):
    """Build a toolchain-specific Lean image (e.g. lean-v4.16.0).

    Launches from the base lean image and installs one specific toolchain.
    """
    # Ensure base lean image exists
    if not runtime.image_exists(base_lean_image):
        if base_lean_image in IMAGES:
            build_image(runtime, base_lean_image)
        elif not runtime.image_exists("lean"):
            build_image(runtime, "lean")
            base_lean_image = "lean"

    alias = f"lean-{version}"
    # Incus container names only allow alphanumeric + hyphens
    safe_alias = alias.replace(".", "-")
    build_name = f"{safe_alias}-builder"

    # Acquire exclusive lock to prevent concurrent builds of the same image.
    # After acquiring, re-check whether the image was built by another process.
    with _build_lock(safe_alias):
        if runtime.image_exists(alias):
            print(f"{alias} image already built (by concurrent process).")
            return

        print(f"Building {alias} image...")

        # Clean up any leftover builder from a previous failed attempt
        _cleanup_builder(runtime, build_name)

        runtime.launch(build_name, base_lean_image)
        try:
            _wait_for_container(runtime, build_name)

            script = (SCRIPTS_DIR / "lean-toolchain.sh").read_text()
            script = f"export LEAN_TOOLCHAIN='{version}'\n" + script
            runtime.exec(build_name, ["bash", "-c", script])

            # Run user customization script as the final build step
            _run_customize_script(runtime, build_name)

            runtime.stop(build_name)
            if runtime.image_exists(alias):
                runtime.image_delete(alias)
            runtime.publish(build_name, alias)
        finally:
            try:
                runtime.delete(build_name, force=True)
            except Exception:
                pass

    print(f"{alias} image built successfully.")
