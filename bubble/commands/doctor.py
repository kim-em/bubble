"""The 'doctor' diagnostic command."""

import json
import subprocess
import sys

import click

from ..config import load_config
from ..lifecycle import load_registry, unregister_bubble
from ..setup import get_runtime
from ..vscode import SSH_CONFIG_FILE, remove_ssh_config


def _save_terminal():
    """Save terminal settings so subprocess calls can't corrupt them."""
    try:
        import termios

        if sys.stdin.isatty():
            return termios.tcgetattr(sys.stdin)
    except (ImportError, termios.error):
        pass
    return None


def _restore_terminal(saved):
    """Restore terminal settings after a subprocess call."""
    if saved is not None:
        try:
            import termios

            termios.tcsetattr(sys.stdin, termios.TCSADRAIN, saved)
        except (ImportError, termios.error):
            pass


def register_doctor_command(main):
    """Register the 'doctor' command on the main CLI group."""

    @main.command()
    def doctor():
        """Diagnose and fix common bubble issues."""
        import platform
        import re

        from ..notices import maybe_print_welcome

        config = load_config()
        maybe_print_welcome()
        issues = 0
        fixed = 0
        saved_tty = _save_terminal()

        # 1. Check Colima (macOS only)
        if platform.system() == "Darwin":
            from ..runtime.colima import is_colima_running

            if is_colima_running():
                _restore_terminal(saved_tty)
                click.echo("Colima: running")
            else:
                _restore_terminal(saved_tty)
                click.echo("Colima: not running")
                issues += 1
                if click.confirm("  Start Colima?"):
                    try:
                        runtime_cfg = config.get("runtime", {})
                        from ..runtime.colima import start_colima

                        start_colima(
                            cpu=runtime_cfg.get("colima_cpu", 4),
                            memory=runtime_cfg.get("colima_memory", 16),
                            disk=runtime_cfg.get("colima_disk", 60),
                            vm_type=runtime_cfg.get("colima_vm_type", "vz"),
                        )
                        _restore_terminal(saved_tty)
                        click.echo("  Started.")
                        fixed += 1
                    except Exception as e:
                        click.echo(f"  Failed: {e}", err=True)

        # Get runtime (don't ensure ready — doctor should work even when things are broken)
        try:
            runtime = get_runtime(config, ensure_ready=False)
        except Exception as e:
            click.echo(f"Cannot connect to runtime: {e}", err=True)
            return

        # 2. Check for stuck incus operations
        click.echo("Checking for stuck operations...")
        try:
            result = subprocess.run(
                ["incus", "operation", "list", "--format=json"],
                capture_output=True,
                text=True,
                check=True,
                stdin=subprocess.DEVNULL,
            )
            _restore_terminal(saved_tty)

            all_ops = json.loads(result.stdout) if result.stdout.strip() else []
            # websocket ops are active exec/console sessions (e.g. VS Code SSH), not stuck
            # Only "Running" operations can be stuck; "Success"/"Failure"/"Cancelled" are
            # just completed history that Incus retains temporarily.
            stuck = [
                op
                for op in all_ops
                if op.get("class") != "websocket" and op.get("status") == "Running"
            ]
            if stuck:
                click.echo(f"  Found {len(stuck)} stuck operation(s):")
                for op in stuck:
                    desc = op.get("description", "unknown")
                    click.echo(f"    {desc}")
                issues += len(stuck)
                if click.confirm("  Cancel stuck operations?"):
                    cancelled = 0
                    errors = []
                    for op in stuck:
                        op_id = op.get("id", "")
                        if not op_id:
                            continue
                        try:
                            subprocess.run(
                                ["incus", "operation", "delete", op_id],
                                capture_output=True,
                                text=True,
                                check=True,
                                timeout=10,
                                stdin=subprocess.DEVNULL,
                            )
                            cancelled += 1
                        except subprocess.CalledProcessError as e:
                            msg = (e.stderr or "").strip()
                            errors.append(f"    {op.get('description', op_id)}: {msg}")
                        except Exception as e:
                            errors.append(f"    {op.get('description', op_id)}: {e}")
                    if cancelled:
                        click.echo(f"  Cancelled {cancelled} operation(s).")
                        fixed += cancelled
                    if errors:
                        click.echo("  Could not cancel some operations:", err=True)
                        for err_msg in errors:
                            click.echo(err_msg, err=True)
            else:
                click.echo("  No stuck operations.")
        except (subprocess.CalledProcessError, FileNotFoundError):
            click.echo("  Could not check operations (incus unavailable).")

        # 3. Check registry vs actual containers
        click.echo("Checking registry consistency...")
        registry = load_registry()
        registered = set(registry.get("bubbles", {}).keys())
        containers = None
        try:
            containers = {c.name for c in runtime.list_containers(fast=True)}
        except Exception:
            click.echo("  Could not list containers (skipping consistency checks).")

        if containers is not None:
            # Stale registry entries (registered but no container)
            stale = registered - containers
            if stale:
                click.echo(f"  {len(stale)} stale registry entries (no matching container):")
                for name in sorted(stale):
                    click.echo(f"    {name}")
                issues += len(stale)
                if click.confirm("  Remove stale entries?"):
                    for name in stale:
                        unregister_bubble(name)
                        remove_ssh_config(name)
                    click.echo(f"  Removed {len(stale)} stale entries.")
                    fixed += len(stale)
            else:
                click.echo("  Registry is consistent.")

            # 4. Check SSH config for orphaned entries
            click.echo("Checking SSH config...")
            ssh_config = SSH_CONFIG_FILE
            orphaned_ssh = []
            if ssh_config.exists():
                for line in ssh_config.read_text().splitlines():
                    m = re.match(r"^Host bubble-(.+)$", line.strip())
                    if m:
                        bubble_name = m.group(1)
                        if bubble_name not in containers:
                            orphaned_ssh.append(bubble_name)
            if orphaned_ssh:
                click.echo(f"  {len(orphaned_ssh)} orphaned SSH config entries:")
                for name in orphaned_ssh:
                    click.echo(f"    bubble-{name}")
                issues += len(orphaned_ssh)
                if click.confirm("  Remove orphaned SSH entries?"):
                    for name in orphaned_ssh:
                        remove_ssh_config(name)
                    click.echo(f"  Removed {len(orphaned_ssh)} entries.")
                    fixed += len(orphaned_ssh)
            else:
                click.echo("  SSH config is clean.")

        # Summary
        if issues == 0:
            click.echo("\nNo issues found.")
        else:
            click.echo(f"\nFound {issues} issue(s), fixed {fixed}.")
