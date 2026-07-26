"""Container image construction."""

from __future__ import annotations

import fcntl
import hashlib
import re
import shlex
import subprocess
import time
from contextlib import ExitStack, contextmanager
from pathlib import Path

from ..config import DATA_DIR, HOST_DATA_DIR, load_config
from ..lean import LEAN_VERSION_RE
from ..runtime.base import ContainerRuntime
from ..tools import resolve_tools, tool_script, tools_hash

PROGRESS_PREFIX = "BUBBLE_PROGRESS: "

CUSTOMIZE_SCRIPT = DATA_DIR / "customize.sh"

SCRIPTS_DIR = Path(__file__).parent / "scripts"

BUILD_LOCK_DIR = HOST_DATA_DIR / "locks" / "images"
IMAGE_BUILD_SCHEMA = "1"
BASE_PARENT_REVISION = "ubuntu-24.04-2026-07"


@contextmanager
def _build_lock(image_name: str, *, shared: bool = False):
    """Acquire a file lock for an image build.

    With ``shared=False`` (default): exclusive lock that prevents
    concurrent builds of the same image from racing on the shared
    builder container name.

    With ``shared=True``: shared/read lock used by derived-image builds
    to hold the parent stable while they build from it.  Multiple shared
    locks coexist, but an exclusive lock blocks until all shared locks
    are released (and vice-versa).
    """
    BUILD_LOCK_DIR.mkdir(parents=True, exist_ok=True)
    lock_path = BUILD_LOCK_DIR / f"{image_name}.lock"
    fd = lock_path.open("w")
    try:
        fcntl.flock(fd, fcntl.LOCK_SH if shared else fcntl.LOCK_EX)
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
    "python": {"script": "python.sh", "parent": "base"},
}


def _get_bridge_dns_ip(runtime: ContainerRuntime) -> str | None:
    """Get the IPv4 address of the default incus bridge (for DNS proxy workaround)."""
    cidr = _get_bridge_cidr(runtime)
    if cidr:
        return cidr.split("/")[0]
    return None


def _get_bridge_cidr(runtime: ContainerRuntime) -> str | None:
    """Get the full CIDR of the incus bridge (e.g. '10.228.152.1/24')."""
    try:
        cidr = runtime.network_get("incusbr0", "ipv4.address").strip()
        if "/" in cidr:
            return cidr
    except (subprocess.TimeoutExpired, FileNotFoundError, RuntimeError):
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
    cidr = _get_bridge_cidr(runtime)
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
    dns_ip = _get_bridge_dns_ip(runtime)
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
    except (RuntimeError, OSError, subprocess.SubprocessError):
        # Clean up on failure
        try:
            runtime.exec(name, ["systemctl", "start", "systemd-resolved"])
        except (RuntimeError, OSError, subprocess.SubprocessError):
            pass
        return False


def wait_for_container(runtime: ContainerRuntime, name: str, timeout: int = 60):
    """Wait for a container to be ready, including network (IPv4 + DNS).

    Handles systems where the firewall blocks bridge DHCP and/or DNS
    (common on NixOS with nftables and bridge-nf-call-iptables=1).
    """
    # Phase 1: wait for container to be exec-able
    for _ in range(timeout):
        try:
            runtime.exec(name, ["true"])
            break
        except (RuntimeError, OSError, subprocess.SubprocessError):
            time.sleep(1)
    else:
        raise RuntimeError(f"Container '{name}' not exec-able after {timeout}s")

    # Phase 2: wait for IPv4 + DNS (give DHCP a chance first)
    for i in range(min(timeout, 15)):
        try:
            runtime.exec(name, ["timeout", "3", "getent", "hosts", "github.com"])
            return  # Everything works
        except (RuntimeError, OSError, subprocess.SubprocessError):
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
    except (RuntimeError, OSError, subprocess.SubprocessError):
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
    if re.fullmatch(r"bubble-[a-z0-9-]+-[0-9a-f]{16}-builder", name):
        return True
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


def _ancestor_chain(image_name: str) -> list[str]:
    """Return the chain of ancestor images (root first) that are our own images.

    For example, ``_ancestor_chain("lean-vscode")`` returns ``["base", "lean"]``
    because lean-vscode's parent is lean, and lean's parent is base.
    External parents (e.g. ``images:ubuntu/24.04``) are excluded.
    """
    ancestors: list[str] = []
    current = image_name
    while current in IMAGES:
        parent = IMAGES[current]["parent"]
        if parent not in IMAGES:
            break
        ancestors.append(parent)
        current = parent
    ancestors.reverse()  # root first for deterministic lock ordering
    return ancestors


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
    vscode_commit = get_vscode_commit()
    total = len(enabled)
    for i, name in enumerate(enabled, 1):
        print(f"  Installing tools: {name} ({i}/{total})...")
        script = tool_script(name)
        if name == "vscode" and vscode_commit:
            script = f"export VSCODE_COMMIT={shlex.quote(vscode_commit)}\n" + script
        runtime.exec(build_name, ["bash", "-c", script])
    return enabled


def customize_hash() -> str | None:
    """Compute a hash of the user customization script, or None if it doesn't exist."""
    if not CUSTOMIZE_SCRIPT.exists():
        return None
    return hashlib.sha256(CUSTOMIZE_SCRIPT.read_bytes()).hexdigest()[:16]


def _hash_parts(*parts: str) -> str:
    digest = hashlib.sha256()
    for part in parts:
        encoded = part.encode()
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return digest.hexdigest()[:16]


def image_build_key(runtime: ContainerRuntime, image_name: str) -> str:
    """Return the deterministic build key for a logical Bubble image."""
    if image_name.startswith("lean-v"):
        version = image_name.removeprefix("lean-")
        if not LEAN_VERSION_RE.fullmatch(version):
            raise ValueError(f"Invalid Lean toolchain version: {version!r}")
        parent_alias = desired_image_alias(runtime, "lean")
        return _hash_parts(
            IMAGE_BUILD_SCHEMA,
            image_name,
            parent_alias,
            (SCRIPTS_DIR / "lean-toolchain.sh").read_text(),
            customize_hash() or "",
        )

    if image_name not in IMAGES:
        available = ", ".join(IMAGES)
        raise ValueError(f"Unknown image: {image_name}. Available: {available}")
    spec = IMAGES[image_name]
    parent = spec["parent"]
    parent_identity = desired_image_alias(runtime, parent) if parent in IMAGES else parent
    extra = ""
    if image_name == "base":
        parent_identity += f":{BASE_PARENT_REVISION}"
        config = load_config()
        enabled = resolve_tools(config)
        extra = tools_hash(enabled)
        if "vscode" in enabled:
            extra += ":" + (get_vscode_commit() or "unavailable")
    return _hash_parts(
        IMAGE_BUILD_SCHEMA,
        image_name,
        parent_identity,
        (SCRIPTS_DIR / spec["script"]).read_text(),
        customize_hash() or "",
        extra,
    )


def desired_image_alias(runtime: ContainerRuntime, image_name: str) -> str:
    """Map a logical image name to its host-global build-keyed alias."""
    key = image_build_key(runtime, image_name)
    safe = image_name.replace(".", "-")
    return f"bubble-{safe}-{key}"


def _image_properties(logical_name: str, build_key: str, parent: str) -> dict[str, str]:
    return {
        "user.bubble.managed": "true",
        "user.bubble.logical_name": logical_name,
        "user.bubble.build_key": build_key,
        "user.bubble.build_schema": IMAGE_BUILD_SCHEMA,
        "user.bubble.parent": parent,
    }


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


def build_image(
    runtime: ContainerRuntime,
    image_name: str,
    *,
    force: bool = False,
    quiet: bool = False,
):
    """Build any known image by name. Builds parent images recursively if needed.

    With ``force=True``, deletes the existing image first so it gets rebuilt
    from scratch. Used by rebuild paths that detect configuration drift
    (tools hash, VS Code commit, customize script).

    ``quiet`` suppresses the initial "Building X image..." announcement.
    Use when the caller already printed its own progress message.
    """
    if image_name not in IMAGES:
        available = ", ".join(IMAGES.keys())
        raise ValueError(f"Unknown image: {image_name}. Available: {available}")

    spec = IMAGES[image_name]
    logical_parent = spec["parent"]

    # Ensure parent image exists (recursive for our own images)
    if logical_parent in IMAGES:
        parent = desired_image_alias(runtime, logical_parent)
        if not runtime.image_exists(parent):
            build_image(runtime, logical_parent)
    else:
        parent = logical_parent

    alias = desired_image_alias(runtime, image_name)
    build_key = alias.rsplit("-", 1)[-1]

    # Immutable parents do not need shared ancestor locks.  The physical
    # alias lock is enough to collapse identical builds while allowing
    # different configurations to build in parallel.
    with ExitStack() as stack:
        for ancestor in _ancestor_chain(image_name):
            stack.enter_context(
                _build_lock(runtime.qualify(desired_image_alias(runtime, ancestor)), shared=True)
            )
        stack.enter_context(_build_lock(runtime.qualify(alias)))
        if force and runtime.image_exists(alias):
            print(f"Deleting existing {image_name} image for rebuild...")
            runtime.image_delete(alias)

        if runtime.image_exists(alias):
            print(f"{image_name} image already built (by concurrent process).")
            return alias

        build_name = f"{alias.replace('.', '-')}-builder"
        if not quiet:
            print(f"Building {image_name} image...")

        # Clean up any leftover builder from a previous failed attempt
        _cleanup_builder(runtime, build_name)

        # Launch from parent
        runtime.launch(build_name, parent)
        try:
            wait_for_container(runtime, build_name)

            # Run setup script
            script = (SCRIPTS_DIR / spec["script"]).read_text()

            def _on_progress(line: str, _label: str = image_name) -> None:
                if line.startswith(PROGRESS_PREFIX):
                    print(f"  {line[len(PROGRESS_PREFIX) :]}")

            runtime.exec_streaming(build_name, ["bash", "-c", script], on_line=_on_progress)

            # Install configured tools (only on base image — derived images inherit them)
            _install_tools_if_base(runtime, build_name, image_name)

            # Run user customization script as the final build step
            _run_customize_script(runtime, build_name)

            # Publish as image
            runtime.stop(build_name)
            runtime.publish(
                build_name,
                alias,
                properties=_image_properties(image_name, build_key, parent),
            )
        finally:
            try:
                runtime.delete(build_name, force=True)
            except Exception:
                pass

        print(f"{image_name} image built successfully.")
        return alias


def build_lean_toolchain_image(
    runtime: ContainerRuntime,
    version: str,
    base_lean_image: str = "lean",
    *,
    force: bool = False,
):
    """Build a toolchain-specific Lean image (e.g. lean-v4.16.0).

    Launches from the base lean image and installs one specific toolchain.
    With ``force=True``, deletes and rebuilds even if the image exists.
    """
    if not LEAN_VERSION_RE.fullmatch(version):
        raise ValueError(f"Invalid Lean toolchain version: {version!r}")

    if base_lean_image in IMAGES:
        parent = desired_image_alias(runtime, base_lean_image)
        if not runtime.image_exists(parent):
            parent = build_image(runtime, base_lean_image, quiet=True)
    elif runtime.image_exists(base_lean_image):
        parent = base_lean_image
    else:
        base_lean_image = "lean"
        parent = desired_image_alias(runtime, base_lean_image)
        if not runtime.image_exists(parent):
            parent = build_image(runtime, base_lean_image, quiet=True)

    logical_name = f"lean-{version}"
    alias = desired_image_alias(runtime, logical_name)
    build_key = alias.rsplit("-", 1)[-1]
    build_name = f"{alias}-builder"

    with ExitStack() as stack:
        if base_lean_image in IMAGES:
            for ancestor in [*_ancestor_chain(base_lean_image), base_lean_image]:
                stack.enter_context(
                    _build_lock(
                        runtime.qualify(desired_image_alias(runtime, ancestor)), shared=True
                    )
                )
        stack.enter_context(_build_lock(runtime.qualify(alias)))
        if force and runtime.image_exists(alias):
            print(f"Deleting existing {logical_name} image for rebuild...")
            runtime.image_delete(alias)
        if runtime.image_exists(alias):
            print(f"{logical_name} image already built (by concurrent process).")
            return alias

        print(f"Building {logical_name} image...")
        _cleanup_builder(runtime, build_name)
        runtime.launch(build_name, parent)
        try:
            wait_for_container(runtime, build_name)

            script = (SCRIPTS_DIR / "lean-toolchain.sh").read_text()
            script = f"export LEAN_TOOLCHAIN={shlex.quote(version)}\n" + script

            def _on_tc_progress(line: str) -> None:
                if line.startswith(PROGRESS_PREFIX):
                    print(f"  {line[len(PROGRESS_PREFIX) :]}")

            runtime.exec_streaming(build_name, ["bash", "-c", script], on_line=_on_tc_progress)

            # Run user customization script as the final build step
            _run_customize_script(runtime, build_name)

            runtime.stop(build_name)
            runtime.publish(
                build_name,
                alias,
                properties=_image_properties(logical_name, build_key, parent),
            )
        finally:
            try:
                runtime.delete(build_name, force=True)
            except Exception:
                pass

    print(f"{logical_name} image built successfully.")
    return alias
