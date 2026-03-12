"""Dependency installation and runtime setup for bubble."""

import json
import os
import platform
import subprocess
import sys
from pathlib import Path

import click

from .runtime.base import ContainerRuntime
from .runtime.incus import IncusRuntime


def _is_command_available(cmd: str) -> bool:
    """Check if a command is available on PATH."""
    try:
        subprocess.run([cmd, "--version"], capture_output=True, timeout=5)
        return True
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _ensure_homebrew_in_path():
    """Add Homebrew's bin directory to PATH if brew exists but isn't on PATH.

    On macOS, non-interactive SSH sessions may have a minimal PATH that
    doesn't include /usr/local/bin (Intel) or /opt/homebrew/bin (Apple Silicon).
    """
    if _is_command_available("brew"):
        return
    for brew_path in ["/opt/homebrew/bin", "/usr/local/bin"]:
        brew_bin = Path(brew_path) / "brew"
        if brew_bin.exists():
            os.environ["PATH"] = brew_path + ":" + os.environ.get("PATH", "")
            return


def _is_nixos() -> bool:
    """Check if we're running on NixOS."""
    return Path("/etc/nixos").is_dir() or Path("/etc/NIXOS").exists()


def _is_debian_based() -> bool:
    """Check if we're on a Debian/Ubuntu system with apt."""
    return _is_command_available("apt-get") and Path("/etc/os-release").exists()


def _install_incus_debian():
    """Install Incus on Debian/Ubuntu via the Zabbly repository."""
    click.echo("Installing Incus from the Zabbly repository...")

    try:
        # Add GPG key
        subprocess.run(
            ["sudo", "mkdir", "-p", "/etc/apt/keyrings/"],
            check=True,
        )
        key_data = subprocess.run(
            ["curl", "-fsSL", "https://pkgs.zabbly.com/key.asc"],
            capture_output=True,
            check=True,
        )
        subprocess.run(
            ["sudo", "tee", "/etc/apt/keyrings/zabbly.asc"],
            input=key_data.stdout,
            capture_output=True,
            check=True,
        )

        # Determine codename and architecture
        os_release = {}
        for line in Path("/etc/os-release").read_text().splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                os_release[k] = v.strip('"')
        codename = os_release.get("VERSION_CODENAME", "jammy")
        arch = subprocess.run(
            ["dpkg", "--print-architecture"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

        # Add repository
        sources_content = (
            f"Enabled: yes\n"
            f"Types: deb\n"
            f"URIs: https://pkgs.zabbly.com/incus/stable\n"
            f"Suites: {codename}\n"
            f"Components: main\n"
            f"Architectures: {arch}\n"
            f"Signed-By: /etc/apt/keyrings/zabbly.asc\n"
        )
        subprocess.run(
            ["sudo", "tee", "/etc/apt/sources.list.d/zabbly-incus-stable.sources"],
            input=sources_content.encode(),
            capture_output=True,
            check=True,
        )

        # Install
        subprocess.run(["sudo", "apt-get", "update"], check=True)
        subprocess.run(["sudo", "apt-get", "install", "-y", "incus"], check=True)
    except subprocess.CalledProcessError as e:
        cmd_str = " ".join(e.cmd) if isinstance(e.cmd, list) else str(e.cmd)
        detail = (getattr(e, "stderr", None) or b"").decode("utf-8", errors="replace").strip()
        msg = f"Failed to install Incus: '{cmd_str}' exited with code {e.returncode}"
        if detail:
            msg += f"\n{detail}"
        raise click.ClickException(msg)

    _post_install_incus()


def _install_incus_snap():
    """Install Incus via snap."""
    click.echo("Installing Incus via snap...")
    try:
        subprocess.run(["sudo", "snap", "install", "incus", "--channel=latest/stable"], check=True)
    except subprocess.CalledProcessError as e:
        raise click.ClickException(f"Failed to install Incus via snap (exit {e.returncode}).")
    _post_install_incus()


def _user_can_sudo() -> bool:
    """Check if current user is likely able to use sudo (in wheel or sudo group)."""
    import getpass
    import grp

    try:
        username = getpass.getuser()
        user_groups = [g.gr_name for g in grp.getgrall() if username in g.gr_mem]
        return "wheel" in user_groups or "sudo" in user_groups
    except Exception:
        return False


def _nixos_incus_snippet(username: str) -> str:
    """Return the NixOS configuration snippet for enabling Incus."""
    return (
        "    virtualisation.incus.enable = true;\n"
        "    networking.nftables.enable = true;\n"
        f'    users.users.{username}.extraGroups = [ "incus-admin" ];'
    )


def _install_incus_nixos():
    """Install Incus on NixOS by editing configuration.nix or providing guidance."""
    import getpass

    username = getpass.getuser()
    config_path = Path("/etc/nixos/configuration.nix")
    snippet = _nixos_incus_snippet(username)

    # Case 1: No sudo — tell the user to ask their admin
    if not _user_can_sudo():
        click.echo("Incus is required but not installed.")
        click.echo("  Your system administrator needs to add to the NixOS configuration:")
        click.echo()
        click.echo(snippet)
        click.echo()
        click.echo("  Then rebuild: sudo nixos-rebuild switch")
        click.echo("  And initialize: sudo incus admin init --minimal")
        sys.exit(1)

    # Case 2: Has sudo + configuration.nix exists — offer to auto-edit
    if config_path.exists():
        content = config_path.read_text()

        if "virtualisation.incus.enable" in content:
            click.echo("Incus appears configured in NixOS but is not available.")
            click.echo("  Try running: sudo nixos-rebuild switch")
            sys.exit(1)

        click.echo("Incus is required but not installed.")
        click.echo(f"  Will add to {config_path}:")
        click.echo()
        click.echo(snippet)
        click.echo()

        if click.confirm(
            "  Edit configuration.nix and run nixos-rebuild switch? (requires sudo)",
            default=True,
        ):
            # Insert before the last closing brace
            last_brace = content.rfind("}")
            if last_brace == -1:
                click.echo(f"  Error: could not find closing '}}' in {config_path}", err=True)
                click.echo(f"  Edit {config_path} manually.")
                sys.exit(1)

            insert = (
                "\n"
                "  # bubble: containerized dev environments\n"
                "  virtualisation.incus.enable = true;\n"
                "  networking.nftables.enable = true;\n"
                f'  users.users.{username}.extraGroups = [ "incus-admin" ];\n'
            )
            new_content = content[:last_brace] + insert + content[last_brace:]

            click.echo(f"  Backing up to {config_path}.bak...")
            try:
                subprocess.run(
                    ["sudo", "cp", str(config_path), str(config_path) + ".bak"],
                    check=True,
                )
                subprocess.run(
                    ["sudo", "tee", str(config_path)],
                    input=new_content.encode(),
                    capture_output=True,
                    check=True,
                )
            except subprocess.CalledProcessError as e:
                cmd_str = " ".join(e.cmd) if isinstance(e.cmd, list) else str(e.cmd)
                raise click.ClickException(
                    f"Failed to update NixOS configuration: "
                    f"'{cmd_str}' exited with code {e.returncode}."
                )

            click.echo("  Running nixos-rebuild switch (this may take a few minutes)...")
            try:
                subprocess.run(["sudo", "nixos-rebuild", "switch"], check=True)
            except subprocess.CalledProcessError:
                click.echo()
                click.echo("  nixos-rebuild failed.", err=True)
                click.echo(f"  Your original configuration is backed up at {config_path}.bak")
                click.echo(f"  To restore: sudo cp {config_path}.bak {config_path}")
                sys.exit(1)

            _post_install_nixos()
        else:
            click.echo(f"  Edit {config_path} manually, then run: sudo nixos-rebuild switch")
            sys.exit(1)

    # Case 3: Has sudo but no configuration.nix (flake setup)
    else:
        click.echo("Incus is required but not installed.")
        click.echo("  Add to your NixOS configuration:")
        click.echo()
        click.echo(snippet)
        click.echo()
        click.echo("  Then run: sudo nixos-rebuild switch")
        click.echo("  And then: sudo incus admin init --minimal")
        sys.exit(1)


def _post_install_nixos():
    """Post-install steps for Incus on NixOS."""
    click.echo("Initializing Incus...")
    try:
        subprocess.run(["sudo", "incus", "admin", "init", "--minimal"], check=True)
    except subprocess.CalledProcessError as e:
        raise click.ClickException(f"Failed to initialize Incus (exit {e.returncode}).")

    click.echo()
    click.echo("Incus installed successfully.")
    click.echo("NOTE: You need to log out and back in for group membership to take effect.")
    click.echo("  Or run: newgrp incus-admin")
    click.echo("  Then re-run your bubble command.")
    sys.exit(0)


def _post_install_incus():
    """Common post-install steps for Incus on Linux."""
    # Initialize with minimal defaults
    click.echo("Initializing Incus...")
    try:
        subprocess.run(["sudo", "incus", "admin", "init", "--minimal"], check=True)
    except subprocess.CalledProcessError as e:
        raise click.ClickException(f"Failed to initialize Incus (exit {e.returncode}).")

    # Add current user to incus-admin group
    import getpass

    username = getpass.getuser()
    click.echo(f"Adding {username} to the incus-admin group...")
    try:
        subprocess.run(["sudo", "usermod", "-aG", "incus-admin", username], check=True)
    except subprocess.CalledProcessError as e:
        raise click.ClickException(
            f"Failed to add {username} to incus-admin group (exit {e.returncode})."
        )

    click.echo()
    click.echo("Incus installed successfully.")
    click.echo("NOTE: You need to log out and back in for group membership to take effect.")
    click.echo("  Or run: newgrp incus-admin")
    click.echo("  Then re-run your bubble command.")
    sys.exit(0)


def _ensure_dependencies():
    """Check for required dependencies and offer to install them interactively."""
    system = platform.system()

    if system == "Darwin":
        _ensure_homebrew_in_path()

        # Check Homebrew
        if not _is_command_available("brew"):
            click.echo("Homebrew is required but not installed.")
            click.echo("  Install it with:")
            click.echo(
                '  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"'
            )
            sys.exit(1)

        # Check Colima, Incus, and QEMU (Intel Macs need QEMU; Apple Silicon uses vz)
        missing = []
        if not _is_command_available("colima"):
            missing.append("colima")
        if not _is_command_available("incus"):
            missing.append("incus")
        if platform.machine() == "x86_64" and not _is_command_available("qemu-img"):
            missing.append("qemu")

        if missing:
            names = " and ".join(missing)
            cmd = "brew install " + " ".join(missing)
            click.echo(
                f"{names} {'is' if len(missing) == 1 else 'are'} required but not installed."
            )
            # Auto-install if no TTY (remote/non-interactive), otherwise confirm
            if not sys.stdin.isatty() or click.confirm(
                f"  Install via Homebrew? ({cmd})", default=True
            ):
                click.echo(f"  Installing: {cmd}")
                try:
                    subprocess.run(["brew", "install"] + missing, check=True)
                except subprocess.CalledProcessError as e:
                    raise click.ClickException(
                        f"Failed to install {names} via Homebrew (exit {e.returncode})."
                    )
            else:
                click.echo(f"  To install manually: {cmd}")
                sys.exit(1)

    elif system == "Linux":
        if not _is_command_available("incus"):
            if _is_nixos():
                _install_incus_nixos()
            else:
                click.echo("Incus is required but not installed.")
                has_snap = _is_command_available("snap")
                has_apt = _is_debian_based()

                if has_apt and has_snap:
                    choice = click.prompt(
                        "  Install via [1] Zabbly apt repository or [2] snap?",
                        type=click.Choice(["1", "2"]),
                        default="1",
                    )
                    if choice == "1":
                        _install_incus_debian()
                    else:
                        _install_incus_snap()
                elif has_apt:
                    if click.confirm(
                        "  Install via the Zabbly repository? (requires sudo)", default=True
                    ):
                        _install_incus_debian()
                    else:
                        click.echo("  See: https://linuxcontainers.org/incus/docs/main/installing/")
                        sys.exit(1)
                elif has_snap:
                    if click.confirm("  Install via snap? (requires sudo)", default=True):
                        _install_incus_snap()
                    else:
                        click.echo("  See: https://linuxcontainers.org/incus/docs/main/installing/")
                        sys.exit(1)
                else:
                    click.echo("  See: https://linuxcontainers.org/incus/docs/main/installing/")
                    sys.exit(1)

        # Incus is installed — check it's initialized (has a storage pool)
        _ensure_incus_initialized()


def _ensure_incus_initialized():
    """Check that Incus has a storage pool; run 'incus admin init --auto' if not."""
    try:
        result = subprocess.run(
            ["incus", "storage", "list", "--format=json"],
            capture_output=True,
            text=True,
            timeout=10,
            stdin=subprocess.DEVNULL,
        )
        if result.returncode == 0:
            pools = json.loads(result.stdout) if result.stdout.strip() else []
            if pools:
                return  # Already initialized
    except (subprocess.TimeoutExpired, FileNotFoundError, json.JSONDecodeError):
        return  # Can't check — proceed optimistically

    click.echo("Incus is installed but not initialized (no storage pool).")
    click.echo("  Running: incus admin init --auto")
    try:
        subprocess.run(
            ["incus", "admin", "init", "--auto"],
            check=True,
            timeout=30,
            stdin=subprocess.DEVNULL,
        )
        click.echo("  Incus initialized.")
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        detail = getattr(e, "stderr", "") or ""
        click.echo(f"  Failed to initialize Incus: {detail}".strip(), err=True)
        click.echo("  Run manually: incus admin init --auto", err=True)
        sys.exit(1)


def colima_host_ip() -> str:
    """Get the host IP as seen from the Colima VM.

    Resolves host.lima.internal from the VM's /etc/hosts.
    Falls back to 192.168.5.2 (the default vz networking address).
    """
    try:
        result = subprocess.run(
            ["colima", "ssh", "--", "getent", "hosts", "host.lima.internal"],
            capture_output=True,
            text=True,
            timeout=10,
            stdin=subprocess.DEVNULL,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip().split()[0]
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return "192.168.5.2"


def get_runtime(config: dict, ensure_ready: bool = True) -> ContainerRuntime:
    """Get the configured container runtime. Ensures platform is ready by default."""
    if ensure_ready:
        _ensure_dependencies()
        if platform.system() == "Darwin":
            from .runtime.colima import ensure_colima

            rt = config["runtime"]
            ensure_colima(
                cpu=rt["colima_cpu"],
                memory=rt["colima_memory"],
                disk=rt.get("colima_disk", 60),
                vm_type=rt.get("colima_vm_type", "vz"),
            )
    backend = config["runtime"]["backend"]
    if backend == "incus":
        return IncusRuntime()
    raise ValueError(f"Unknown runtime backend: {backend}")
