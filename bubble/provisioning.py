"""Container provisioning: launch, mount setup, network configuration."""

import platform
import shlex
import shutil
import subprocess
from pathlib import Path

import click

from .config import DATA_DIR
from .security import SETTINGS, get_setting, is_enabled


def cache_copies_dir(container_name: str) -> Path:
    """Host directory holding a bubble's per-container shared-cache copies.

    Used by the macOS/Colima seeded-copy path (see :func:`seed_cache_copy`) and
    cleaned up on ``bubble pop``.
    """
    return DATA_DIR / "shared-cache-copies" / container_name


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


def _setup_overlay(runtime, container: str, lower_path: str, mount_path: str):
    """Set up an overlayfs mount combining a read-only lower with a writable upper.

    Creates per-container upper/work directories and mounts overlayfs so that
    reads come from lower_path (the shared cache) and writes go to the
    container-local upper directory.

    The overlay survives pause/resume (frozen state preserves mounts) but not
    stop/start. Bubbles are rarely stopped — they're typically paused or
    destroyed — so this is acceptable.
    """
    overlay_base = f"{mount_path}-overlay"
    upper = f"{overlay_base}/upper"
    work = f"{overlay_base}/work"
    q_lower = shlex.quote(lower_path)
    q_mount = shlex.quote(mount_path)
    q_upper = shlex.quote(upper)
    q_work = shlex.quote(work)
    runtime.exec(
        container,
        [
            "bash",
            "-c",
            # Create directories, mount overlayfs, fix upper ownership.
            # workdir stays root-owned (overlayfs internal use only).
            f"mkdir -p {q_upper} {q_work} {q_mount}"
            f" && mount -t overlay overlay"
            f" -o lowerdir={q_lower},upperdir={q_upper},workdir={q_work}"
            f" {q_mount}"
            f" && chown user:user {q_upper}",
        ],
    )


def seed_cache_copy(host_path: Path, container_name: str, host_dir_name: str) -> Path:
    """Create a per-bubble writable copy of a shared cache, seeded from it.

    Used on macOS/Colima, where the shared cache reaches the container over
    virtiofs and overlayfs-over-virtiofs can't perform copy-up (issue #306) —
    writes through the overlay fail with EACCES even though the mount is rw.
    Instead we hand the container its own writable copy of the cache.

    The copy is seeded with ``cp -c`` so APFS clonefile makes it cheap: it is
    created instantly and shares storage with the shared cache until a file is
    modified, preserving the speedup that overlay mode exists for. On a
    filesystem without clone support we fall back to a plain recursive copy.
    Writes stay isolated to this bubble and are dropped on ``bubble pop``.
    """
    seed_path = cache_copies_dir(container_name) / host_dir_name
    # Start fresh in case a stale copy lingers from a crashed run; cp needs the
    # destination to not exist so it clones host_path *as* seed_path.
    if seed_path.exists():
        shutil.rmtree(seed_path)
    seed_path.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        ["cp", "-cR", str(host_path), str(seed_path)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        # Some cp builds reject -c on unsupported filesystems instead of
        # falling back to a normal copy; retry without clonefile.
        shutil.rmtree(seed_path, ignore_errors=True)
        subprocess.run(["cp", "-R", str(host_path), str(seed_path)], check=True)
    # Match the shared cache's group-writable mode so the UID-mapped container
    # user can write to the copy.
    seed_path.chmod(0o770)
    return seed_path


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
    skip_auth_setup=False,
):
    """Launch container, wait for readiness, apply network allowlist, mount git repos."""
    from .output import detail

    detail("Launching container...", nl=False)
    runtime.launch(name, image_name)
    click.echo(" done.")

    detail("Waiting for network...", nl=False)
    from .images.builder import wait_for_container

    try:
        wait_for_container(runtime, name)
        click.echo(" done.")
    except RuntimeError:
        click.echo(" timeout (continuing anyway).")

    # Apply network allowlist early, before clone or any hook code runs
    if network:
        from .container_helpers import apply_network

        extra_domains = hook.network_domains() if hook else None
        apply_network(runtime, name, config, extra_domains, keep_github_domains=skip_auth_setup)

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
    # Modes: on = read-write, off = read-only, overlay = read-only + overlayfs
    shared_cache_setting = get_setting(config, "shared_cache")
    if shared_cache_setting == "auto":
        shared_cache_setting = SETTINGS["shared_cache"].auto_default
    use_overlay = shared_cache_setting == "overlay"
    shared_cache_writable = shared_cache_setting not in ("off", "overlay")
    if hook:
        env_lines = []
        for host_dir_name, container_path, env_var in hook.shared_mounts():
            host_path = DATA_DIR / host_dir_name
            host_path.mkdir(parents=True, exist_ok=True)
            # Make group-writable so container user can write with UID mapping
            host_path.chmod(0o770)
            if use_overlay and platform.system() == "Darwin":
                # macOS/Colima: overlayfs-over-virtiofs can't copy-up, so a
                # real overlay is unwritable (issue #306). Give the container a
                # per-bubble writable copy seeded (cheaply, via clonefile) from
                # the shared cache instead — same read-from-cache + isolated-
                # writes behaviour overlay mode promises.
                from .output import detail

                detail("Seeding per-bubble cache copy (overlay unavailable on macOS)...", nl=False)
                seed_path = seed_cache_copy(host_path, name, host_dir_name)
                click.echo(" done.")
                runtime.add_disk(
                    name,
                    f"shared-{host_dir_name}",
                    str(seed_path),
                    container_path,
                    readonly=False,
                )
            elif use_overlay:
                # Mount shared cache read-only at a staging path; overlayfs
                # will provide writable access at the expected container_path
                lower_path = f"{container_path}-ro"
                runtime.add_disk(
                    name,
                    f"shared-{host_dir_name}",
                    str(host_path),
                    lower_path,
                    readonly=True,
                )
                _setup_overlay(runtime, name, lower_path, container_path)
            else:
                runtime.add_disk(
                    name,
                    f"shared-{host_dir_name}",
                    str(host_path),
                    container_path,
                    readonly=not shared_cache_writable,
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
        # other's sessions, but all accumulate under ~/.bubble/ai-projects/
        # on the host (useful for backing up with a git repo).
        # Skip if a user mount overlaps with this target.
        projects_target = Path("/home/user/.claude/projects")
        user_targets = {Path(m.target) for m in (user_mounts or [])}
        if not mount_overlaps(projects_target, user_targets):
            projects_dir = DATA_DIR / "ai-projects" / name
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
        from .runtime.colima import colima_bind_ip

        # macOS/Colima: Unix sockets can't traverse virtio-fs, use TCP.
        # Use the bridge IP where the relay daemon actually listens
        # (colima_bind_ip), not host.lima.internal which may resolve to
        # a different interface.
        if platform.system() == "Darwin" and RELAY_PORT_FILE.exists():
            port = RELAY_PORT_FILE.read_text().strip()
            host_ip = colima_bind_ip()
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
