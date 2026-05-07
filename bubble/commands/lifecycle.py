"""Lifecycle commands: pause, pop, cleanup."""

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import click

from ..clean import check_clean, check_native_clean, format_reasons
from ..config import NATIVE_DIR, load_config
from ..lifecycle import get_bubble_info, unregister_bubble
from ..setup import get_runtime
from ..vscode import remove_ssh_config


def destroy_bubble(name: str, info: dict | None = None, on_progress=None) -> tuple[bool, str]:
    """Destroy a bubble unconditionally (no confirmation, no cleanness check).

    Routes to the right backend (remote / native / local container) based on
    the registry entry. Returns (success, error_message).

    On success (including "already gone"), cleans up the local SSH config,
    tokens, and registry entry. On failure, leaves local state intact so the
    user can retry cleanup with `bubble pop -f`.

    `on_progress` is an optional callable that receives status strings (e.g.
    "Container busy, retrying (1/3)...") so the CLI can stream progress.

    Used by `bubble pop -f` and by --ephemeral after a --command exits.
    """
    if info is None:
        info = get_bubble_info(name)

    # Remote-hosted bubble: destroy on the remote, then clean up locally
    if info and info.get("remote_host"):
        from ..remote import RemoteHost, apply_cloud_ssh_options, remote_command

        host = RemoteHost.parse(info["remote_host"])
        apply_cloud_ssh_options(host)
        try:
            result = remote_command(host, ["pop", "-f", name])
        except Exception as e:
            return False, f"Failed to pop on {host.ssh_destination}: {e}"
        if result.returncode != 0:
            return False, (
                f"Failed to pop on {host.ssh_destination}: "
                f"{(result.stderr or '').strip() or 'remote pop returned nonzero'}"
            )
        remove_ssh_config(name)
        _cleanup_tokens(name, remote_host_spec=info["remote_host"])
        unregister_bubble(name)
        return True, ""

    # Native workspace: rmtree under NATIVE_DIR (refuse paths outside the root)
    if info and info.get("native"):
        native_path = info.get("native_path", "")
        if native_path and Path(native_path).is_dir():
            resolved = Path(native_path).resolve()
            native_dir_resolved = NATIVE_DIR.resolve()
            if not str(resolved).startswith(str(native_dir_resolved) + os.sep):
                return False, (
                    f"Refusing to delete: path '{native_path}' is not under {NATIVE_DIR}"
                )
            try:
                shutil.rmtree(resolved)
            except OSError as e:
                return False, f"Failed to remove native workspace: {e}"
        _cleanup_tokens(name)
        unregister_bubble(name)
        return True, ""

    # Local container
    config = load_config()
    runtime = get_runtime(config, ensure_ready=False)
    last_error = ""
    deleted = False
    for attempt in range(3):
        try:
            runtime.delete(name, force=True)
            deleted = True
            break
        except subprocess.CalledProcessError as e:
            msg = ((e.stderr or "") + " " + (e.stdout or "")).strip()
            last_error = msg or str(e)
            if "not found" in msg.lower() or "does not exist" in msg.lower():
                deleted = True  # Already gone — proceed with cleanup
                break
            if "busy" in msg.lower() and attempt < 2:
                if on_progress:
                    on_progress(f"Container busy, retrying ({attempt + 1}/3)...")
                time.sleep(3 * (attempt + 1))
                continue
            break
        except Exception as e:
            last_error = str(e)
            break

    if not deleted:
        return False, last_error or "unknown error"

    remove_ssh_config(name)
    _cleanup_tokens(name)
    unregister_bubble(name)
    return True, ""


def _cleanup_tokens(name: str, remote_host_spec: str = ""):
    """Remove relay and auth proxy tokens for a container.

    If remote_host_spec is provided, also stops the SSH tunnel to
    that host if no other bubbles are using it.
    """
    from ..auth_proxy import remove_auth_tokens
    from ..relay import remove_relay_token

    remove_relay_token(name)
    remove_auth_tokens(name)

    if remote_host_spec:
        from ..tunnel import stop_tunnel_if_unused

        stop_tunnel_if_unused(remote_host_spec)


def register_lifecycle_commands(main):
    """Register pause, pop, and cleanup commands on the main CLI group."""

    @main.command()
    @click.argument("name")
    def pause(name):
        """Pause (freeze) a bubble."""
        info = get_bubble_info(name)
        if info and info.get("native"):
            click.echo(
                "Native workspaces don't support pause/resume (no container state).", err=True
            )
            sys.exit(1)
        # Auto-route to remote host if the bubble is registered there
        if info and info.get("remote_host"):
            from ..remote import RemoteHost, apply_cloud_ssh_options, remote_command

            host = RemoteHost.parse(info["remote_host"])
            apply_cloud_ssh_options(host)
            result = remote_command(host, ["pause", name])
            if result.returncode != 0:
                click.echo(f"Failed to pause on {host.ssh_destination}: {result.stderr}", err=True)
                sys.exit(1)
            click.echo(f"Bubble '{name}' paused on {host.ssh_destination}.")
            return

        config = load_config()
        runtime = get_runtime(config, ensure_ready=False)
        runtime.freeze(name)
        click.echo(f"Bubble '{name}' paused.")

    @main.command()
    @click.argument("name")
    @click.option("-f", "--force", is_flag=True, help="Skip confirmation prompt")
    def pop(name, force):
        """Pop a bubble (destroy it permanently)."""
        info = get_bubble_info(name)

        # Confirmation prompt (skipped with -f)
        if not force:
            if info and info.get("remote_host"):
                from ..remote import RemoteHost

                host = RemoteHost.parse(info["remote_host"])
                click.confirm(
                    f"Permanently pop bubble '{name}' on {host.ssh_destination}?",
                    abort=True,
                )
            elif info and info.get("native"):
                native_path = info.get("native_path", "")
                if native_path and Path(native_path).is_dir():
                    cs = check_native_clean(native_path, name)
                    if cs.clean:
                        click.echo(f"Native workspace '{name}' is clean. ", nl=False)
                    elif cs.error:
                        click.confirm(
                            f"Cannot verify cleanness ({cs.error}). "
                            f"Permanently pop native workspace '{name}'?",
                            abort=True,
                        )
                    else:
                        reasons = format_reasons(cs.reasons)
                        click.echo("Warning: workspace has unsaved work:")
                        for r in reasons:
                            click.echo(f"  - {r}")
                        click.confirm(f"Permanently pop native workspace '{name}'?", abort=True)
            else:
                config = load_config()
                runtime = get_runtime(config, ensure_ready=False)
                cs = check_clean(runtime, name)
                if cs.clean:
                    click.echo(f"Bubble '{name}' is clean. ", nl=False)
                elif cs.error:
                    click.confirm(
                        f"Cannot verify cleanness ({cs.error}). Permanently pop bubble '{name}'?",
                        abort=True,
                    )
                else:
                    reasons = format_reasons(cs.reasons)
                    click.echo("Warning: bubble has unsaved work:")
                    for r in reasons:
                        click.echo(f"  - {r}")
                    click.confirm(f"Permanently pop bubble '{name}'?", abort=True)

        success, error = destroy_bubble(name, info=info, on_progress=click.echo)
        if not success:
            click.echo(f"Failed to delete bubble '{name}': {error}", err=True)
            click.echo("Try 'bubble doctor' to diagnose and fix the issue.", err=True)
            sys.exit(1)

        if info and info.get("remote_host"):
            from ..remote import RemoteHost

            host = RemoteHost.parse(info["remote_host"])
            click.echo(f"Bubble '{name}' popped on {host.ssh_destination}.")
        elif info and info.get("native"):
            click.echo(f"Native workspace '{name}' popped.")
        else:
            click.echo(f"Bubble '{name}' popped.")

    @main.command()
    @click.option("-n", "--dry-run", is_flag=True, help="Show what would be popped")
    @click.option("-f", "--force", is_flag=True, help="Skip confirmation prompt")
    @click.option(
        "-a",
        "--all",
        "check_all",
        is_flag=True,
        help="Start stopped/frozen bubbles to check them",
    )
    @click.option("--age", type=int, default=0, help="Only clean up bubbles unused for N+ days")
    def cleanup(dry_run, force, check_all, age):
        """Pop all clean bubbles (safe, no unsaved work)."""
        config = load_config()
        runtime = get_runtime(config, ensure_ready=False)

        from ..images.builder import is_builder_container

        containers = runtime.list_containers(fast=True)

        # Clean up stale builder containers (leftover from interrupted image builds).
        # Skip running builders — they may be active image builds.
        builders = [c for c in containers if is_builder_container(c.name) and c.state != "running"]
        for c in builders:
            if dry_run:
                click.echo(f"  Would remove stale builder: {c.name}")
            else:
                try:
                    runtime.delete(c.name, force=True)
                    click.echo(f"  Removed stale builder: {c.name}")
                except Exception as e:
                    click.echo(f"  Could not remove builder {c.name}: {e}")

        # Filter out builder containers from the bubble check
        containers = [c for c in containers if not is_builder_container(c.name)]
        to_check = [c for c in containers if c.state == "running"]
        to_start = []
        if check_all:
            to_start = [c for c in containers if c.state in ("stopped", "frozen")]

        if not to_check and not to_start:
            click.echo("No bubbles to check.")
            return

        if age > 0:
            from datetime import datetime, timedelta, timezone

            cutoff = datetime.now(timezone.utc) - timedelta(days=age)
            to_check = [c for c in to_check if c.last_used_at and c.last_used_at < cutoff]
            to_start = [c for c in to_start if c.last_used_at and c.last_used_at < cutoff]
            if not to_check and not to_start:
                click.echo(f"No bubbles unused for {age}+ days.")
                return

        # Start stopped/frozen containers temporarily for checking
        started_containers = []
        for c in to_start:
            try:
                click.echo(f"  Starting {c.name} for inspection...")
                if c.state == "frozen":
                    runtime.unfreeze(c.name)
                else:
                    runtime.start(c.name)
                started_containers.append(c)
                to_check.append(c)
            except Exception as e:
                click.echo(f"  {c.name:<30} could not start: {e}")

        total = len(to_check)
        click.echo(f"Checking {total} bubble{'s' if total != 1 else ''}...")
        clean_list = []
        dirty_count = 0
        for c in to_check:
            cs = check_clean(runtime, c.name)
            if cs.clean:
                click.echo(f"  {c.name:<30} clean")
                clean_list.append(c.name)
            else:
                reasons = cs.summary
                click.echo(f"  {c.name:<30} {reasons}")
                dirty_count += 1

        # Re-stop containers that were started just for checking and are dirty
        started_names = {c.name for c in started_containers}
        clean_names = set(clean_list)
        for c in started_containers:
            if c.name not in clean_names:
                try:
                    runtime.stop(c.name)
                except Exception:
                    pass

        if not clean_list:
            click.echo("No clean bubbles to pop.")
            return

        if dry_run:
            n = len(clean_list)
            click.echo(f"\nWould pop {n} clean bubble{'s' if n != 1 else ''}.")
            # Re-stop clean containers that were started for checking
            for name in clean_list:
                if name in started_names:
                    try:
                        runtime.stop(name)
                    except Exception:
                        pass
            return

        if not force:
            click.confirm(
                f"\nPop {len(clean_list)} clean bubble{'s' if len(clean_list) != 1 else ''}?",
                abort=True,
            )

        for name in clean_list:
            runtime.delete(name, force=True)
            remove_ssh_config(name)
            unregister_bubble(name)
            click.echo(f"  Popped {name}")

        if dirty_count:
            click.echo(f"Kept {dirty_count} dirty bubble{'s' if dirty_count != 1 else ''}.")
