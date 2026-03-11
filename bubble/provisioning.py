"""Container provisioning: launch, mount setup, network configuration."""

import platform
import shlex
from pathlib import Path

import click

from .config import DATA_DIR
from .security import is_enabled


def mount_overlaps(target: Path, user_targets: set[Path]) -> bool:
    """Check if target overlaps with any user mount (exact match or ancestry)."""
    for ut in user_targets:
        # Exact match, or one is an ancestor of the other
        if target == ut:
            return True
        try:
            target.relative_to(ut)
            return True  # target is inside a user mount
        except ValueError:
            pass
        try:
            ut.relative_to(target)
            return True  # user mount is inside this auto mount
        except ValueError:
            pass
    return False


def provision_container(
    runtime,
    name,
    image_name,
    ref_path,
    mount_name,
    config,
    hook=None,
    dep_mounts=None,
    network=False,
    user_mounts=None,
    claude_mounts=None,
    codex_mounts=None,
    editor_mounts=None,
):
    """Launch container, wait for readiness, apply network allowlist, mount git repos."""
    click.echo("  Launching container...", nl=False)
    runtime.launch(name, image_name)
    click.echo(" done.")

    click.echo("  Waiting for network...", nl=False)
    from .images.builder import _wait_for_container

    try:
        _wait_for_container(runtime, name)
        click.echo(" done.")
    except RuntimeError:
        click.echo(" timeout (continuing anyway).")

    # Apply network allowlist early, before clone or any hook code runs
    if network:
        from .container_helpers import apply_network

        extra_domains = hook.network_domains() if hook else None
        apply_network(runtime, name, config, extra_domains)

    mount_source = str(ref_path)
    if Path(mount_source).exists():
        runtime.add_disk(
            name, "shared-git", mount_source, f"/shared/git/{mount_name}", readonly=True
        )

    # Mount dependency bare repos for Lake pre-population via alternates
    if dep_mounts:
        for repo_name, dep_path in dep_mounts.items():
            if str(dep_path) == mount_source:
                continue  # Don't double-mount the main repo
            device_name = f"dep-{repo_name}".replace(".", "-").replace("_", "-")[:63]
            runtime.add_disk(
                name,
                device_name,
                str(dep_path),
                f"/shared/git/{repo_name}.git",
                readonly=True,
            )

    # Add shared mounts from hook (e.g. mathlib cache)
    # When shared_cache is off, mount read-only so containers can't poison the cache
    shared_cache_enabled = is_enabled(config, "shared_cache")
    if hook:
        env_lines = []
        for host_dir_name, container_path, env_var in hook.shared_mounts():
            host_path = DATA_DIR / host_dir_name
            host_path.mkdir(parents=True, exist_ok=True)
            # Make group-writable so container user can write with UID mapping
            host_path.chmod(0o770)
            runtime.add_disk(
                name,
                f"shared-{host_dir_name}",
                str(host_path),
                container_path,
                readonly=not shared_cache_enabled,
            )
            if env_var:
                env_lines.append(f"export {env_var}={shlex.quote(container_path)}")
        if env_lines:
            # Set env vars globally via /etc/profile.d so all shells see them
            script = "\\n".join(env_lines)
            runtime.exec(
                name,
                [
                    "bash",
                    "-c",
                    f"printf '{script}\\n' > /etc/profile.d/bubble-shared.sh",
                ],
            )

    # Mount Claude Code config (read-only individual files/dirs from ~/.claude)
    if claude_mounts:
        runtime.exec(
            name,
            ["bash", "-c", "mkdir -p /home/user/.claude && chown user:user /home/user/.claude"],
        )
        for i, m in enumerate(claude_mounts):
            runtime.add_disk(
                name,
                f"claude-config-{i}",
                m.source,
                m.target,
                readonly=m.readonly,
            )
        # Writable projects directory — per-bubble subdirectory, persists on host.
        # Each bubble gets its own isolated subdir so bubbles can't see each
        # other's sessions, but all accumulate under ~/.bubble/claude-projects/
        # on the host (useful for backing up with a git repo).
        # Skip if a user mount overlaps with this target.
        projects_target = Path("/home/user/.claude/projects")
        user_targets = {Path(m.target) for m in (user_mounts or [])}
        if not mount_overlaps(projects_target, user_targets):
            projects_dir = DATA_DIR / "claude-projects" / name
            projects_dir.mkdir(parents=True, exist_ok=True)
            projects_dir.chmod(0o770)
            runtime.add_disk(
                name,
                "claude-projects",
                str(projects_dir),
                "/home/user/.claude/projects",
            )

    # Mount Codex config (read-only individual files from ~/.codex)
    if codex_mounts:
        runtime.exec(
            name,
            ["bash", "-c", "mkdir -p /home/user/.codex && chown user:user /home/user/.codex"],
        )
        for i, m in enumerate(codex_mounts):
            runtime.add_disk(
                name,
                f"codex-config-{i}",
                m.source,
                m.target,
                readonly=m.readonly,
            )

    # Mount editor config directories (read-only config, read-write data/state)
    if editor_mounts:
        for i, m in enumerate(editor_mounts):
            # Ensure parent directories exist in the container
            parent = str(Path(m.target).parent)
            runtime.exec(
                name,
                [
                    "bash",
                    "-c",
                    f"mkdir -p {shlex.quote(parent)} && chown -R user:user {shlex.quote(parent)}",
                ],
            )
            runtime.add_disk(
                name,
                f"editor-config-{i}",
                m.source,
                m.target,
                readonly=m.readonly,
            )
            # Apply exclusions by overmounting with writable tmpfs (same pattern
            # as user mounts). This lets the editor write to plugin/cache subdirs
            # within a read-only config mount.
            for excluded in m.exclude:
                exc_path = f"{m.target.rstrip('/')}/{excluded}"
                runtime.exec(
                    name,
                    [
                        "bash",
                        "-c",
                        f"mkdir -p {shlex.quote(exc_path)}"
                        f" && mount -t tmpfs tmpfs {shlex.quote(exc_path)}"
                        f" && chown user:user {shlex.quote(exc_path)}",
                    ],
                )

    # Add user-specified mounts (from --mount flags and [[mounts]] config)
    if user_mounts:
        for i, m in enumerate(user_mounts):
            device_name = f"user-mount-{i}"
            runtime.add_disk(
                name,
                device_name,
                m.source,
                m.target,
                readonly=m.readonly,
            )
            # Apply exclusions by overmounting with empty tmpfs
            for excluded in m.exclude:
                exc_path = f"{m.target.rstrip('/')}/{excluded}"
                # Mount a tmpfs to hide the excluded subdirectory.
                # mkdir -p runs as root inside the container, so it works
                # even on RO mounts (the dir already exists on the host,
                # or we create it in the container's overlay).
                runtime.exec(
                    name,
                    [
                        "bash",
                        "-c",
                        f"mkdir -p {shlex.quote(exc_path)}"
                        f" && mount -t tmpfs tmpfs {shlex.quote(exc_path)}",
                    ],
                )

    if is_enabled(config, "relay"):
        from .relay import RELAY_PORT_FILE, RELAY_SOCK
        from .setup import colima_host_ip

        # macOS/Colima: Unix sockets can't traverse virtio-fs, use TCP.
        # incus proxy needs an IP (not hostname), so resolve host.lima.internal
        # from the VM — this is the host's IP as seen from incusd.
        if platform.system() == "Darwin" and RELAY_PORT_FILE.exists():
            port = RELAY_PORT_FILE.read_text().strip()
            host_ip = colima_host_ip()
            connect_addr = f"tcp:{host_ip}:{port}"
        elif RELAY_SOCK.exists():
            connect_addr = f"unix:{RELAY_SOCK}"
        else:
            connect_addr = None

        if connect_addr:
            runtime.add_device(
                name,
                "bubble-relay",
                "proxy",
                connect=connect_addr,
                listen="unix:/bubble/relay.sock",
                bind="container",
                uid="1001",
                gid="1001",
            )
            from .relay import generate_relay_token

            token = generate_relay_token(name)
            runtime.exec(
                name,
                [
                    "bash",
                    "-c",
                    f"echo {shlex.quote(token)} > /bubble/relay-token"
                    " && chown user:user /bubble/relay-token"
                    " && chmod 600 /bubble/relay-token",
                ],
            )
