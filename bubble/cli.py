"""CLI entry point for bubble."""

import json
import platform
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import click

from . import __version__
from .clean import CleanStatus, check_clean, format_reasons
from .config import DATA_DIR, ensure_dirs, load_config, repo_short_name, save_config
from .git_store import (
    bare_repo_path,
    ensure_rev_available,
    fetch_ref,
    github_url,
    init_bare_repo,
    update_all_repos,
)
from .hooks import select_hook
from .images.builder import VSCODE_COMMIT_FILE, get_vscode_commit
from .lifecycle import get_bubble_info, load_registry, register_bubble, unregister_bubble
from .naming import deduplicate_name, generate_name
from .repo_registry import RepoRegistry
from .runtime.base import ContainerRuntime
from .runtime.incus import IncusRuntime
from .target import TargetParseError, parse_target
from .vscode import SSH_CONFIG_FILE, add_ssh_config, open_editor, open_vscode, remove_ssh_config


def _is_command_available(cmd: str) -> bool:
    """Check if a command is available on PATH."""
    try:
        subprocess.run([cmd, "--version"], capture_output=True, timeout=5)
        return True
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _is_nixos() -> bool:
    """Check if we're running on NixOS."""
    return Path("/etc/nixos").is_dir() or Path("/etc/NIXOS").exists()


def _is_debian_based() -> bool:
    """Check if we're on a Debian/Ubuntu system with apt."""
    return _is_command_available("apt-get") and Path("/etc/os-release").exists()


def _install_incus_debian():
    """Install Incus on Debian/Ubuntu via the Zabbly repository."""
    click.echo("Installing Incus from the Zabbly repository...")

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

    _post_install_incus()


def _install_incus_snap():
    """Install Incus via snap."""
    click.echo("Installing Incus via snap...")
    subprocess.run(["sudo", "snap", "install", "incus", "--channel=latest/stable"], check=True)
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
    subprocess.run(["sudo", "incus", "admin", "init", "--minimal"], check=True)

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
    subprocess.run(["sudo", "incus", "admin", "init", "--minimal"], check=True)

    # Add current user to incus-admin group
    import getpass

    username = getpass.getuser()
    click.echo(f"Adding {username} to the incus-admin group...")
    subprocess.run(["sudo", "usermod", "-aG", "incus-admin", username], check=True)

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
        # Check Homebrew
        if not _is_command_available("brew"):
            click.echo("Homebrew is required but not installed.")
            click.echo("  Install it with:")
            click.echo(
                '  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"'
            )
            sys.exit(1)

        # Check Colima and Incus
        missing = []
        if not _is_command_available("colima"):
            missing.append("colima")
        if not _is_command_available("incus"):
            missing.append("incus")

        if missing:
            names = " and ".join(missing)
            cmd = "brew install " + " ".join(missing)
            click.echo(
                f"{names} {'is' if len(missing) == 1 else 'are'} required but not installed."
            )
            if click.confirm(f"  Install via Homebrew? ({cmd})", default=True):
                subprocess.run(["brew", "install"] + missing, check=True)
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


def _colima_host_ip() -> str:
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


def _find_container(runtime: ContainerRuntime, name: str):
    """Find a container by name. Returns ContainerInfo or exits."""
    for c in runtime.list_containers():
        if c.name == name:
            return c
    click.echo(f"Bubble '{name}' not found.", err=True)
    sys.exit(1)


def _ensure_running(runtime: ContainerRuntime, name: str):
    """Ensure a container is running (unpause/start if needed)."""
    info = _find_container(runtime, name)
    if info.state == "frozen":
        click.echo(f"Unpausing '{name}'...")
        runtime.unfreeze(name)
    elif info.state == "stopped":
        click.echo(f"Starting '{name}'...")
        runtime.start(name)
    return info


def _setup_ssh(runtime: ContainerRuntime, name: str):
    """Start SSH and inject host public keys into a container."""
    runtime.exec(name, ["bash", "-c", "service ssh start || /usr/sbin/sshd"])

    ssh_dir = Path.home() / ".ssh"
    pub_keys = []
    for key_file in ["id_ed25519.pub", "id_rsa.pub", "id_ecdsa.pub"]:
        key_path = ssh_dir / key_file
        if key_path.exists():
            pub_keys.append(key_path.read_text().strip())
    if pub_keys:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".keys", delete=False) as f:
            f.write("\n".join(pub_keys) + "\n")
            tmp_keys = f.name
        try:
            runtime.exec(
                name,
                [
                    "su",
                    "-",
                    "user",
                    "-c",
                    "mkdir -p ~/.ssh && chmod 700 ~/.ssh",
                ],
            )
            runtime.push_file(name, tmp_keys, "/home/user/.ssh/authorized_keys")
            runtime.exec(
                name,
                [
                    "bash",
                    "-c",
                    "chown user:user /home/user/.ssh/authorized_keys"
                    " && chmod 600 /home/user/.ssh/authorized_keys",
                ],
            )
        finally:
            Path(tmp_keys).unlink(missing_ok=True)

    add_ssh_config(name)


def _apply_network(
    runtime: ContainerRuntime, name: str, config: dict, extra_domains: list[str] | None = None
):
    """Apply network allowlist to a container if configured."""
    domains = list(config.get("network", {}).get("allowlist", []))
    # Always include VSCode infrastructure domains — bubble is a VSCode-first tool
    from .vscode import VSCODE_NETWORK_DOMAINS

    for d in VSCODE_NETWORK_DOMAINS:
        if d not in domains:
            domains.append(d)
    if extra_domains:
        for d in extra_domains:
            if d not in domains:
                domains.append(d)
    if domains:
        try:
            from .network import apply_allowlist

            apply_allowlist(runtime, name, domains)
            click.echo("  Network allowlist applied.")
        except Exception as e:
            click.echo(f"  Warning: could not apply network allowlist: {e}")


def _detect_project_dir(runtime: ContainerRuntime, name: str) -> str:
    """Detect the project directory inside a container."""
    try:
        return (
            runtime.exec(name, ["bash", "-c", "ls -d /home/user/*/ 2>/dev/null | head -1"])
            .strip()
            .rstrip("/")
        )
    except Exception:
        return "/home/user"


def _maybe_install_automation():
    """Install automation jobs on first use if not already present."""
    from .automation import install_automation, is_automation_installed

    try:
        status = is_automation_installed()
        if status and not any(status.values()):
            click.echo("Installing automation (hourly git update, weekly image refresh)...")
            click.echo("  To remove later: bubble automation remove")
            installed = install_automation()
            for item in installed:
                click.echo(f"  {item}")
    except Exception:
        pass


def _find_existing_container(
    runtime: ContainerRuntime,
    target_str: str,
    generated_name: str | None = None,
    org_repo: str | None = None,
    kind: str | None = None,
    ref: str | None = None,
) -> str | None:
    """Find an existing container matching the target. Returns name or None."""
    containers = {c.name for c in runtime.list_containers()}

    # Check if raw target string matches a container name
    if target_str in containers:
        return target_str

    # Check by generated name
    if generated_name and generated_name in containers:
        return generated_name

    # Check registry for same org_repo + PR/branch
    if org_repo and kind and ref:
        registry = load_registry()
        for bname, binfo in registry.get("bubbles", {}).items():
            if binfo.get("org_repo") != org_repo:
                continue
            if bname not in containers:
                continue
            if kind == "pr" and str(binfo.get("pr", "")) == ref:
                return bname
            if kind == "branch" and binfo.get("branch") == ref:
                return bname

    return None


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


BASIC_COMMANDS = {"open", "list", "pause", "destroy"}


class BubbleGroup(click.Group):
    """Custom group that routes unknown first args to the implicit 'open' command."""

    def format_usage(self, ctx, formatter):
        formatter.write("Usage: bubble TARGET [OPTIONS]\n")
        formatter.write("       bubble COMMAND [ARGS]...\n")

    def format_commands(self, ctx, formatter):
        """Split commands into Basic and Advanced sections."""
        commands = []
        for subcommand in self.list_commands(ctx):
            cmd = self.commands.get(subcommand)
            if cmd is None or cmd.hidden:
                continue
            help_text = cmd.get_short_help_str(limit=formatter.width)
            commands.append((subcommand, help_text))

        basic = [(n, h) for n, h in commands if n in BASIC_COMMANDS]
        advanced = [(n, h) for n, h in commands if n not in BASIC_COMMANDS]

        if basic:
            with formatter.section("Commands"):
                formatter.write_dl(basic)
        if advanced:
            with formatter.section("Advanced"):
                formatter.write_dl(advanced)

    def parse_args(self, ctx, args):
        """If no known command is found among args, prepend 'open'.

        This supports both `bubble TARGET` and `bubble --ssh HOST TARGET`.
        """
        has_command = any(
            not a.startswith("-") and a in self.commands for a in args
        )
        if args and not has_command:
            args = ["open"] + args
        return super().parse_args(ctx, args)


@click.group(cls=BubbleGroup, context_settings=dict(help_option_names=["-h", "--help"]))
@click.version_option(__version__)
def main():
    """bubble: Open a containerized dev environment.

    Run bubble TARGET to create (or reattach to) an isolated container and
    open it in your preferred editor. Defaults to VSCode via Remote SSH.
    Use --emacs, --neovim, or --shell for alternatives, or set a default
    with: bubble editor neovim

    \b
    Examples:
      bubble .                                      Current directory
      bubble leanprover-community/mathlib4          GitHub repo
      bubble https://github.com/owner/repo/pull/42  Pull request
      bubble mathlib4/pull/123                      PR shorthand
      bubble 456                                    PR in current repo
    """


@main.command("help", hidden=True)
@click.argument("command", nargs=-1)
@click.pass_context
def help_cmd(ctx, command):
    """Show help for a command."""
    if not command:
        click.echo(main.get_help(ctx))
        return
    cmd = main
    for name in command:
        if isinstance(cmd, click.Group):
            cmd = cmd.get_command(ctx, name)
            if cmd is None:
                click.echo(f"Unknown command: {' '.join(command)}")
                raise SystemExit(1)
        else:
            click.echo(f"'{name}' is not a subcommand of '{command[command.index(name)-1]}'")
            raise SystemExit(1)
    # Build a proper context so the Usage line shows the right command name
    sub_ctx = click.Context(cmd, info_name=command[-1], parent=ctx.parent)
    click.echo(cmd.get_help(sub_ctx))


# ---------------------------------------------------------------------------
# open (the primary command, invoked implicitly)
# ---------------------------------------------------------------------------


def _spawn_background_bubble(args: list[str], log_path: str):
    """Spawn a background bubble command, detached from the current process.

    Tries `bubble` on PATH first, falls back to `sys.executable -m bubble`.
    """
    bubble_cmd = shutil.which("bubble")
    if bubble_cmd:
        cmd = [bubble_cmd] + args
    else:
        cmd = [sys.executable, "-m", "bubble"] + args
    log_file = open(log_path, "w")
    subprocess.Popen(
        cmd,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    log_file.close()


def _maybe_rebuild_base_image():
    """If VS Code has updated since the base image was built, rebuild in background."""
    commit = get_vscode_commit()
    if not commit:
        return
    if VSCODE_COMMIT_FILE.exists() and VSCODE_COMMIT_FILE.read_text().strip() == commit:
        return
    _spawn_background_bubble(
        ["images", "build", "base"],
        "/tmp/bubble-vscode-rebuild.log",
    )


def _generate_bubble_name(t, custom_name: str | None) -> str:
    """Generate a container name from a parsed target."""
    if custom_name:
        return custom_name
    if t.kind == "pr":
        return generate_name(t.short_name, "pr", t.ref)
    if t.kind == "branch":
        return generate_name(t.short_name, "branch", t.ref)
    if t.kind == "commit":
        return generate_name(t.short_name, "commit", t.ref[:12])
    return generate_name(t.short_name, "main", "")


def _resolve_ref_source(t, no_clone: bool) -> tuple[Path, str]:
    """Resolve the git reference source (bare repo or local .git).

    Returns (ref_path, mount_name).
    """
    if t.local_path:
        try:
            git_dir_result = subprocess.run(
                ["git", "-C", t.local_path, "rev-parse", "--absolute-git-dir"],
                capture_output=True,
                text=True,
                check=True,
            )
            ref_path = Path(git_dir_result.stdout.strip())
        except subprocess.CalledProcessError:
            ref_path = Path(t.local_path) / ".git"
        mount_name = f"{repo_short_name(t.org_repo)}.git"
    else:
        if no_clone:
            bare_path = bare_repo_path(t.org_repo)
            if not bare_path.exists():
                click.echo(f"Repo '{t.org_repo}' is not available in the git store.", err=True)
                sys.exit(1)
        else:
            bare_path = init_bare_repo(t.org_repo)
        ref_path = bare_path
        mount_name = ref_path.name

        if t.kind == "pr":
            click.echo(f"Fetching PR #{t.ref}...")
            try:
                fetch_ref(t.org_repo, f"refs/pull/{t.ref}/head:refs/pull/{t.ref}/head")
            except Exception:
                pass  # May already be available from a full fetch

    return ref_path, mount_name


def _detect_and_build_image(runtime, ref_path, t):
    """Detect language hook and ensure image exists. Returns (hook, image_name)."""
    if t.kind == "pr":
        hook_ref = f"refs/pull/{t.ref}/head"
    elif t.kind in ("branch", "commit"):
        hook_ref = t.ref
    else:
        hook_ref = "HEAD"

    hook = select_hook(ref_path, hook_ref)
    if hook:
        click.echo(f"  Detected: {hook.name()}")
        image_name = hook.image_name()
    else:
        image_name = "base"

    if not runtime.image_exists(image_name):
        if image_name.startswith("lean-v"):
            # Toolchain-specific image doesn't exist yet — fall back to base lean
            # and build the toolchain image in the background for next time
            version = image_name[len("lean-"):]
            click.echo(f"  Toolchain {version} image not cached, using base lean image (building {image_name} in background for next time)")
            _background_build_lean_toolchain(version)
            image_name = "lean"
        if not runtime.image_exists(image_name):
            click.echo(f"Building {image_name} image (one-time setup, may take a few minutes)...")
            from .images.builder import build_image

            build_image(runtime, image_name)
            click.echo(f"  {image_name} image ready.")
    elif image_name.startswith("lean-v"):
        version = image_name[len("lean-"):]
        click.echo(f"  Using cached toolchain image ({version})")

    return hook, image_name


def _background_build_lean_toolchain(version: str):
    """Fire off a background build of a toolchain-specific Lean image."""
    # Lock file prevents duplicate concurrent builds for the same version
    lock_path = Path(f"/tmp/bubble-lean-{version}.lock")
    try:
        lock_path.touch(exist_ok=False)
    except FileExistsError:
        # Stale lock from a killed build? Delete if older than 1 hour.
        try:
            age = time.time() - lock_path.stat().st_mtime
            if age < 3600:
                return  # Build likely still in progress
            lock_path.unlink(missing_ok=True)
            lock_path.touch(exist_ok=False)
        except (OSError, FileExistsError):
            return
    click.echo(f"  Building lean-{version} image in background for next time...")
    _spawn_background_bubble(
        ["images", "build", f"lean-{version}"],
        f"/tmp/bubble-lean-{version}-build.log",
    )


def _provision_container(
    runtime, name, image_name, ref_path, mount_name, config, hook=None, dep_mounts=None,
):
    """Launch container, wait for readiness, mount git repos, set up relay."""
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
                name, device_name, str(dep_path),
                f"/shared/git/{repo_name}.git", readonly=True,
            )

    # Add shared writable mounts from hook (e.g. mathlib cache)
    if hook:
        env_lines = []
        for host_dir_name, container_path, env_var in hook.shared_mounts():
            host_path = DATA_DIR / host_dir_name
            host_path.mkdir(parents=True, exist_ok=True)
            # Make world-writable so container user can write regardless of UID mapping
            host_path.chmod(0o777)
            runtime.add_disk(
                name, f"shared-{host_dir_name}", str(host_path), container_path,
            )
            if env_var:
                env_lines.append(f"export {env_var}={shlex.quote(container_path)}")
        if env_lines:
            # Set env vars globally via /etc/profile.d so all shells see them
            script = "\\n".join(env_lines)
            runtime.exec(name, [
                "bash", "-c", f"printf '{script}\\n' > /etc/profile.d/bubble-shared.sh",
            ])

    relay_enabled = config.get("relay", {}).get("enabled", False)
    if relay_enabled:
        from .relay import RELAY_PORT_FILE, RELAY_SOCK

        # macOS/Colima: Unix sockets can't traverse virtio-fs, use TCP.
        # incus proxy needs an IP (not hostname), so resolve host.lima.internal
        # from the VM — this is the host's IP as seen from incusd.
        if platform.system() == "Darwin" and RELAY_PORT_FILE.exists():
            port = RELAY_PORT_FILE.read_text().strip()
            host_ip = _colima_host_ip()
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


def _get_pr_metadata(owner: str, repo: str, pr_number: str) -> tuple[str, str, str] | None:
    """Query GitHub API for PR head branch info.

    Returns (head_ref, head_repo, clone_url) or None.
    """
    try:
        result = subprocess.run(
            [
                "gh", "api", f"repos/{owner}/{repo}/pulls/{pr_number}",
                "--jq", ".head.ref,.head.repo.full_name,.head.repo.clone_url",
            ],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            lines = result.stdout.strip().splitlines()
            if len(lines) == 3:
                return lines[0], lines[1], lines[2]
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return None


def _clone_and_checkout(runtime, name, t, mount_name, short) -> str:
    """Clone the repo and checkout the appropriate ref. Returns the checkout branch name."""
    url = github_url(t.org_repo)
    q_short = shlex.quote(short)
    click.echo(f"Cloning {t.org_repo} (using shared objects)...")
    runtime.exec(
        name,
        [
            "su",
            "-",
            "user",
            "-c",
            f"git clone --reference /shared/git/{mount_name} {url} /home/user/{q_short}",
        ],
    )

    checkout_branch = ""
    if t.kind == "pr":
        click.echo(f"Checking out PR #{t.ref}...")
        pr_meta = _get_pr_metadata(t.owner, t.repo, t.ref)
        pr_checkout_ok = False
        if pr_meta:
            head_ref, head_repo, clone_url = pr_meta
            is_fork = head_repo.lower() != t.org_repo.lower()
            q_head = shlex.quote(head_ref)

            try:
                if is_fork:
                    # Fork PR: add fork remote, fetch branch, checkout with tracking
                    fork_owner = head_repo.split("/")[0]
                    q_owner = shlex.quote(fork_owner)
                    q_url = shlex.quote(clone_url)
                    runtime.exec(
                        name,
                        [
                            "su", "-", "user", "-c",
                            f"cd /home/user/{q_short}"
                            f" && (git remote add {q_owner} {q_url} 2>/dev/null"
                            f" || git remote set-url {q_owner} {q_url})"
                            f" && git fetch {q_owner}"
                            f" +refs/heads/{q_head}:refs/remotes/{q_owner}/{q_head}"
                            f" && git checkout -b {q_head} --track {q_owner}/{q_head}",
                        ],
                    )
                else:
                    # Same-repo PR: fetch branch, checkout with tracking
                    runtime.exec(
                        name,
                        [
                            "su", "-", "user", "-c",
                            f"cd /home/user/{q_short}"
                            f" && git fetch origin"
                            f" +refs/heads/{q_head}:refs/remotes/origin/{q_head}"
                            f" && git checkout -b {q_head} --track origin/{q_head}",
                        ],
                    )
                checkout_branch = head_ref
                pr_checkout_ok = True
            except RuntimeError:
                # Branch may have been deleted; fall through to pull ref fallback
                pass

        if not pr_checkout_ok:
            # Fallback: gh unavailable or API error
            checkout_branch = f"pr-{t.ref}"
            q_branch = shlex.quote(checkout_branch)
            runtime.exec(
                name,
                [
                    "su", "-", "user", "-c",
                    f"cd /home/user/{q_short} && git fetch origin"
                    f" pull/{t.ref}/head:{q_branch} && git checkout {q_branch}",
                ],
            )
    elif t.kind == "branch":
        click.echo(f"Checking out branch '{t.ref}'...")
        checkout_branch = t.ref
        q_branch = shlex.quote(t.ref)
        try:
            runtime.exec(
                name,
                ["su", "-", "user", "-c", f"cd /home/user/{q_short} && git switch {q_branch}"],
            )
        except RuntimeError:
            if t.local_path:
                runtime.exec(
                    name,
                    [
                        "su",
                        "-",
                        "user",
                        "-c",
                        f"cd /home/user/{q_short} && git fetch /shared/git/{mount_name}"
                        f" {q_branch}:{q_branch} && git switch {q_branch}",
                    ],
                )
            else:
                raise
    elif t.kind == "commit":
        click.echo(f"Checking out commit {t.ref[:12]}...")
        q_commit = shlex.quote(t.ref)
        runtime.exec(
            name,
            ["su", "-", "user", "-c", f"cd /home/user/{q_short} && git checkout {q_commit}"],
        )

    return checkout_branch


def _finalize_bubble(
    runtime, name, t, hook, image_name, checkout_branch, short, network, config,
    editor, no_interactive, machine_readable=False,
):
    """Post-clone setup: hooks, SSH, network, registration, and attach."""
    q_short = shlex.quote(short)
    project_dir = f"/home/user/{short}"
    if hook:
        hook.post_clone(runtime, name, project_dir)

    if network:
        extra_domains = hook.network_domains() if hook else None
        _apply_network(runtime, name, config, extra_domains)

    if not machine_readable:
        click.echo("Setting up SSH access...")
    _setup_ssh(runtime, name)

    commit = ""
    try:
        commit = runtime.exec(
            name,
            ["su", "-", "user", "-c", f"cd /home/user/{q_short} && git rev-parse HEAD"],
        ).strip()
    except Exception:
        pass

    register_bubble(
        name,
        t.org_repo,
        branch=checkout_branch or (t.ref if t.kind == "branch" else ""),
        commit=commit,
        pr=int(t.ref) if t.kind == "pr" else 0,
        base_image=image_name,
    )

    if machine_readable:
        _machine_readable_output(
            "created", name,
            project_dir=project_dir,
            org_repo=t.org_repo,
            image=image_name,
            branch=checkout_branch or (t.ref if t.kind == "branch" else ""),
        )
        return

    _maybe_install_automation()

    click.echo(f"Bubble '{name}' created successfully.")
    click.echo(f"  SSH: ssh bubble-{name}")

    if not no_interactive:
        if editor == "vscode":
            click.echo("Opening VSCode...")
        elif editor == "shell":
            click.echo("Connecting via SSH...")
        else:
            click.echo(f"Opening {editor}...")
        open_editor(editor, name, project_dir)


def _machine_readable_output(status: str, name: str, **kwargs):
    """Output JSON for --machine-readable mode."""
    data = {"status": status, "name": name}
    data.update({k: v for k, v in kwargs.items() if v is not None})
    click.echo(json.dumps(data))


def _open_remote(remote_host, target, editor, no_interactive, network, custom_name, config):
    """Open a bubble on a remote host, then connect locally."""
    from .remote import remote_open

    try:
        result = remote_open(
            remote_host, target,
            network=network,
            custom_name=custom_name,
        )
    except RuntimeError as e:
        click.echo(str(e), err=True)
        sys.exit(1)

    name = result["name"]
    project_dir = result.get("project_dir", "/home/user")
    org_repo = result.get("org_repo", "")

    # Write local SSH config with chained ProxyCommand through the remote host
    add_ssh_config(name, remote_host=remote_host)

    # Register in local lifecycle registry with remote_host info
    register_bubble(
        name,
        org_repo,
        branch=result.get("branch", ""),
        base_image=result.get("image", ""),
        remote_host=remote_host.spec_string(),
    )

    click.echo(f"Bubble '{name}' ready on {remote_host.ssh_destination}.")
    click.echo(f"  SSH: ssh bubble-{name}")

    if not no_interactive:
        if editor == "vscode":
            click.echo("Opening VSCode...")
        elif editor == "shell":
            click.echo("Connecting via SSH...")
        else:
            click.echo(f"Opening {editor}...")
        open_editor(editor, name, project_dir)


@main.command("open")
@click.argument("target")
@click.option("--editor", "editor_choice", type=click.Choice(["vscode", "emacs", "neovim", "shell"]),
              default=None, help="Editor to use (default: from config or vscode)")
@click.option("--shell", is_flag=True, help="Drop into SSH session (shortcut for --editor shell)")
@click.option("--emacs", is_flag=True, help="Open Emacs over SSH (shortcut for --editor emacs)")
@click.option("--neovim", is_flag=True, help="Open Neovim over SSH (shortcut for --editor neovim)")
@click.option("--ssh", "ssh_host", type=str, default=None, metavar="HOST",
              help="Run on remote host (host, user@host, or user@host:port)")
@click.option("--cloud", "cloud", is_flag=True,
              help="Run on auto-provisioned Hetzner Cloud server")
@click.option("--local", "force_local", is_flag=True,
              help="Force local execution (override default remote/cloud)")
@click.option("--no-interactive", is_flag=True, help="Just create, don't attach")
@click.option("--machine-readable", is_flag=True, hidden=True,
              help="Output JSON (for remote orchestration)")
@click.option("--network/--no-network", default=True, help="Apply network allowlist")
@click.option("--name", "custom_name", type=str, help="Custom container name")
@click.option("--path", "force_path", is_flag=True, help="Interpret target as a local path")
@click.option(
    "--no-clone", is_flag=True, hidden=True, help="Fail if bare repo doesn't exist (used by relay)"
)
def open_cmd(target, editor_choice, shell, emacs, neovim, ssh_host, cloud, force_local,
             no_interactive, machine_readable, network, custom_name, force_path, no_clone):
    """Open a bubble for a target (GitHub URL, repo, local path, or PR number)."""
    if force_path and not target.startswith(("/", ".", "..")):
        target = "./" + target

    config = load_config()

    # Resolve editor: shortcut flags > --editor > config > vscode
    if shell:
        editor = "shell"
    elif emacs:
        editor = "emacs"
    elif neovim:
        editor = "neovim"
    elif editor_choice is not None:
        editor = editor_choice
    else:
        editor = config.get("editor", "vscode")

    # Priority: --local > --ssh > --cloud > [cloud] default > [remote] default_host
    remote_host = None
    if not force_local and not machine_readable:
        if ssh_host:
            from .remote import RemoteHost
            remote_host = RemoteHost.parse(ssh_host)
        elif cloud or config.get("cloud", {}).get("default", False):
            from .cloud import get_cloud_remote_host
            remote_host = get_cloud_remote_host(config)
        else:
            default = config.get("remote", {}).get("default_host", "")
            if default:
                from .remote import RemoteHost
                remote_host = RemoteHost.parse(default)

    if remote_host:
        _open_remote(remote_host, target, editor, no_interactive, network, custom_name, config)
        return

    # Local flow
    if not machine_readable:
        _maybe_rebuild_base_image()

    runtime = get_runtime(config)

    # Check if target matches an existing container
    existing = _find_existing_container(runtime, target)
    if existing:
        if machine_readable:
            project_dir = _detect_project_dir(runtime, existing)
            _machine_readable_output("reattached", existing, project_dir=project_dir)
            return
        _reattach(runtime, existing, editor, no_interactive)
        return

    # Parse and register target
    registry = RepoRegistry()
    try:
        t = parse_target(target, registry)
    except TargetParseError as e:
        if machine_readable:
            _machine_readable_output("error", "", message=str(e))
            sys.exit(1)
        click.echo(str(e), err=True)
        sys.exit(1)
    registry.register(t.owner, t.repo)

    # Generate name and check for existing container with same target
    name = _generate_bubble_name(t, custom_name)
    existing = _find_existing_container(
        runtime,
        target,
        generated_name=name,
        org_repo=t.org_repo,
        kind=t.kind,
        ref=t.ref,
    )
    if existing:
        if machine_readable:
            project_dir = _detect_project_dir(runtime, existing)
            _machine_readable_output("reattached", existing, project_dir=project_dir,
                                     org_repo=t.org_repo)
            return
        _reattach(runtime, existing, editor, no_interactive)
        return

    # Resolve git source, detect language, and build image
    ensure_dirs()
    ref_path, mount_name = _resolve_ref_source(t, no_clone)
    hook, image_name = _detect_and_build_image(runtime, ref_path, t)

    # Pre-fetch dependency bare repos for Lake pre-population
    dep_mounts = {}  # repo_name -> host_path
    if hook:
        deps = hook.git_dependencies()
        if deps:
            if not machine_readable:
                click.echo("  Preparing Lake dependency mirrors...")
            for dep in deps:
                try:
                    dep_path = init_bare_repo(dep.org_repo)
                    if not ensure_rev_available(dep.org_repo, dep.rev):
                        if not machine_readable:
                            click.echo(
                                f"  Warning: rev {dep.rev[:12]} not found"
                                f" for {dep.name}, skipping"
                            )
                        continue
                    repo_name = dep.org_repo.split("/")[-1]
                    dep_mounts[repo_name] = dep_path
                except Exception as e:
                    if not machine_readable:
                        click.echo(f"  Warning: could not prepare {dep.name}: {e}")

    # Deduplicate and create
    existing_names = {c.name for c in runtime.list_containers()}
    name = deduplicate_name(name, existing_names)
    if not machine_readable:
        click.echo(f"Creating bubble '{name}'...")

    # Provision, clone, and finalize
    short = repo_short_name(t.org_repo)
    _provision_container(
        runtime, name, image_name, ref_path, mount_name, config,
        hook=hook, dep_mounts=dep_mounts,
    )
    checkout_branch = _clone_and_checkout(runtime, name, t, mount_name, short)
    _finalize_bubble(
        runtime,
        name,
        t,
        hook,
        image_name,
        checkout_branch,
        short,
        network,
        config,
        editor,
        no_interactive,
        machine_readable,
    )


def _reattach(runtime: ContainerRuntime, name: str, editor: str, no_interactive: bool):
    """Re-attach to an existing container."""
    _ensure_running(runtime, name)

    if no_interactive:
        click.echo(f"Bubble '{name}' is running.")
        return

    project_dir = _detect_project_dir(runtime, name)

    if editor == "vscode":
        click.echo(f"Opening VSCode for '{name}'...")
    elif editor == "shell":
        click.echo(f"Connecting to '{name}' via SSH...")
    else:
        click.echo(f"Opening {editor} for '{name}'...")
    open_editor(editor, name, project_dir)


def _format_bytes(n: int) -> str:
    """Format byte count as human-readable string."""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024
    return f"{n:.1f} PB"


def _format_age(dt: "datetime | None") -> str:  # noqa: F821
    """Format a datetime as a human-readable age string."""
    if dt is None:
        return "-"
    from datetime import datetime, timezone
    delta = datetime.now(timezone.utc) - dt
    seconds = int(delta.total_seconds())
    if seconds < 60:
        return "just now"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h ago"
    days = hours // 24
    if days < 30:
        return f"{days}d ago"
    months = days // 30
    return f"{months}mo ago"


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


@main.command("list")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
@click.option("-v", "--verbose", is_flag=True, help="Include disk usage and IPv4 (slower)")
@click.option("-c", "--clean", "show_clean", is_flag=True, help="Check cleanness status (slower)")
def list_bubbles(as_json, verbose, show_clean):
    """List all bubbles."""
    config = load_config()
    runtime = get_runtime(config, ensure_ready=False)

    containers = runtime.list_containers(fast=not verbose)

    # Check cleanness for running containers if requested
    clean_statuses = {}
    if show_clean:
        for c in containers:
            if c.state == "running":
                clean_statuses[c.name] = check_clean(runtime, c.name)
            else:
                clean_statuses[c.name] = CleanStatus(clean=False, error="not running")

    if as_json:
        data = []
        for c in containers:
            entry = {
                "name": c.name,
                "state": c.state,
                "created_at": c.created_at.isoformat() if c.created_at else None,
                "last_used_at": c.last_used_at.isoformat() if c.last_used_at else None,
            }
            if verbose:
                entry["ipv4"] = c.ipv4
                entry["disk_usage"] = c.disk_usage
            if show_clean:
                cs = clean_statuses.get(c.name)
                if cs and cs.error:
                    entry["clean"] = None
                elif cs:
                    entry["clean"] = {"status": cs.clean, "reasons": cs.reasons}
            data.append(entry)
        click.echo(json.dumps(data, indent=2))
        return

    if not containers:
        click.echo("No bubbles. Create one with: bubble owner/repo")
        return

    # Build header and rows based on flags
    header = f"{'NAME':<30} {'STATE':<10} {'CREATED':<12} {'LAST USED':<12}"
    if verbose:
        header += f" {'DISK':<10} {'IPv4':<16}"
    if show_clean:
        header += " STATUS"
    click.echo(header)
    click.echo("-" * len(header))
    for c in containers:
        created = _format_age(c.created_at)
        used = _format_age(c.last_used_at)
        line = f"{c.name:<30} {c.state:<10} {created:<12} {used:<12}"
        if verbose:
            disk = _format_bytes(c.disk_usage) if c.disk_usage else "-"
            ipv4 = c.ipv4 or "-"
            line += f" {disk:<10} {ipv4:<16}"
        if show_clean:
            cs = clean_statuses.get(c.name)
            line += f" {cs.summary}" if cs else ""
        click.echo(line)
    if not verbose and not show_clean:
        click.echo("\nUse -v for disk usage and network info, -c to check cleanness.")


# ---------------------------------------------------------------------------
# pause / destroy
# ---------------------------------------------------------------------------


@main.command()
@click.argument("name")
def pause(name):
    """Pause (freeze) a bubble."""
    # Auto-route to remote host if the bubble is registered there
    info = get_bubble_info(name)
    if info and info.get("remote_host"):
        from .remote import RemoteHost, remote_command
        host = RemoteHost.parse(info["remote_host"])
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
def destroy(name, force):
    """Destroy a bubble permanently."""
    # Auto-route to remote host if the bubble is registered there
    info = get_bubble_info(name)
    if info and info.get("remote_host"):
        from .remote import RemoteHost, remote_command
        host = RemoteHost.parse(info["remote_host"])
        if not force:
            click.confirm(
                f"Permanently destroy bubble '{name}' on {host.ssh_destination}?",
                abort=True,
            )
        result = remote_command(host, ["destroy", "-f", name])
        if result.returncode != 0:
            click.echo(f"Failed to destroy on {host.ssh_destination}: {result.stderr}", err=True)
            sys.exit(1)
        remove_ssh_config(name)
        unregister_bubble(name)
        click.echo(f"Bubble '{name}' destroyed on {host.ssh_destination}.")
        return

    config = load_config()
    runtime = get_runtime(config, ensure_ready=False)

    if not force:
        cs = check_clean(runtime, name)
        if cs.clean:
            click.echo(f"Bubble '{name}' is clean. ", nl=False)
        elif cs.error:
            click.confirm(
                f"Cannot verify cleanness ({cs.error}). Permanently destroy bubble '{name}'?",
                abort=True,
            )
        else:
            reasons = format_reasons(cs.reasons)
            click.echo("Warning: bubble has unsaved work:")
            for r in reasons:
                click.echo(f"  - {r}")
            click.confirm(f"Permanently destroy bubble '{name}'?", abort=True)

    import time

    for attempt in range(3):
        try:
            runtime.delete(name, force=True)
            break
        except subprocess.CalledProcessError as e:
            msg = ((e.stderr or "") + " " + (e.stdout or "")).strip()
            if "not found" in msg.lower() or "does not exist" in msg.lower():
                break  # Already gone, just clean up registry/ssh
            if "busy" in msg.lower() and attempt < 2:
                click.echo(f"Container busy, retrying ({attempt + 1}/3)...")
                time.sleep(3 * (attempt + 1))
                continue
            click.echo(f"Failed to delete container: {msg}", err=True)
            click.echo("Try 'bubble doctor' to diagnose and fix the issue.", err=True)
            sys.exit(1)

    remove_ssh_config(name)
    unregister_bubble(name)
    click.echo(f"Bubble '{name}' destroyed.")


@main.command()
@click.option("-n", "--dry-run", is_flag=True, help="Show what would be destroyed")
@click.option("-f", "--force", is_flag=True, help="Skip confirmation prompt")
@click.option(
    "-a", "--all", "check_all", is_flag=True,
    help="Start stopped/frozen bubbles to check them",
)
@click.option("--age", type=int, default=0, help="Only clean up bubbles unused for N+ days")
def cleanup(dry_run, force, check_all, age):
    """Destroy all clean bubbles (safe, no unsaved work)."""
    config = load_config()
    runtime = get_runtime(config, ensure_ready=False)

    containers = runtime.list_containers(fast=True)
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
        click.echo("No clean bubbles to destroy.")
        return

    if dry_run:
        n = len(clean_list)
        click.echo(f"\nWould destroy {n} clean bubble{'s' if n != 1 else ''}.")
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
            f"\nDestroy {len(clean_list)} clean bubble{'s' if len(clean_list) != 1 else ''}?",
            abort=True,
        )

    for name in clean_list:
        runtime.delete(name, force=True)
        remove_ssh_config(name)
        unregister_bubble(name)
        click.echo(f"  Destroyed {name}")

    if dirty_count:
        click.echo(f"Kept {dirty_count} dirty bubble{'s' if dirty_count != 1 else ''}.")


# ---------------------------------------------------------------------------
# images
# ---------------------------------------------------------------------------


@main.group("images")
def images_group():
    """Manage base images."""


@images_group.command("list")
def images_list():
    """List available base images."""
    config = load_config()
    runtime = get_runtime(config, ensure_ready=False)
    try:
        images = runtime.list_images()
        if not images:
            click.echo("No images. Run: bubble images build base")
            return
        click.echo(f"{'ALIAS':<25} {'SIZE':<12} {'CREATED':<20}")
        click.echo("-" * 57)
        for img in images:
            aliases = ", ".join(a["name"] for a in img.get("aliases", []))
            size_mb = img.get("size", 0) / (1024 * 1024)
            created = img.get("created_at", "")[:19]
            click.echo(f"{aliases:<25} {size_mb:>8.1f} MB  {created:<20}")
    except Exception as e:
        click.echo(f"Error listing images: {e}", err=True)


@images_group.command("build")
@click.argument("image_name", default="base")
def images_build(image_name):
    """Build an image (base, lean, or lean-v4.X.Y for a specific toolchain)."""
    config = load_config()
    runtime = get_runtime(config)

    if image_name.startswith("lean-v"):
        import re

        from .images.builder import build_lean_toolchain_image

        version = image_name[len("lean-"):]
        if not re.fullmatch(r"v\d+\.\d+\.\d+(-rc\d+)?", version):
            click.echo(
                f"Invalid toolchain version: {version}. Expected format: v4.X.Y or v4.X.Y-rcN",
                err=True,
            )
            sys.exit(1)
        try:
            build_lean_toolchain_image(runtime, version)
        except Exception as e:
            click.echo(str(e), err=True)
            sys.exit(1)
    else:
        from .images.builder import build_image

        try:
            build_image(runtime, image_name)
        except ValueError as e:
            click.echo(str(e), err=True)
            sys.exit(1)


@images_group.command("delete")
@click.argument("image_name", required=False)
@click.option("--all", "delete_all", is_flag=True, help="Delete all images.")
def images_delete(image_name, delete_all):
    """Delete an image by alias or fingerprint, or --all to delete all images."""
    config = load_config()
    runtime = get_runtime(config, ensure_ready=False)
    if delete_all:
        images = runtime.list_images()
        if not images:
            click.echo("No images to delete.")
            return
        runtime.image_delete_all()
        click.echo(f"Deleted {len(images)} image(s).")
        return
    if not image_name:
        click.echo("Specify an image name or use --all.", err=True)
        sys.exit(1)
    # Try alias first, then fingerprint prefix
    if not runtime.image_exists(image_name):
        # Check if it matches a fingerprint prefix
        images = runtime.list_images()
        matches = [
            img for img in images if img.get("fingerprint", "").startswith(image_name)
        ]
        if len(matches) == 1:
            fp = matches[0]["fingerprint"]
            runtime.image_delete(fp)
            click.echo(f"Deleted image '{image_name}'.")
            return
        elif len(matches) > 1:
            click.echo(
                f"Ambiguous fingerprint prefix '{image_name}' matches {len(matches)} images.",
                err=True,
            )
            sys.exit(1)
        else:
            click.echo(f"Image '{image_name}' not found.", err=True)
            sys.exit(1)
    runtime.image_delete(image_name)
    click.echo(f"Deleted image '{image_name}'.")


# ---------------------------------------------------------------------------
# git
# ---------------------------------------------------------------------------


@main.group("git")
def git_group():
    """Manage shared git object store."""


@git_group.command("update")
def git_update():
    """Update all shared bare repos."""
    update_all_repos()
    click.echo("Git store updated.")


# ---------------------------------------------------------------------------
# network
# ---------------------------------------------------------------------------


@main.group("network")
def network_group():
    """Manage network allowlisting."""


@network_group.command("apply")
@click.argument("name")
def network_apply(name):
    """Apply network allowlist to a bubble."""
    config = load_config()
    runtime = get_runtime(config, ensure_ready=False)
    _ensure_running(runtime, name)

    domains = config.get("network", {}).get("allowlist", [])
    if not domains:
        click.echo("No domains in allowlist. Edit ~/.bubble/config.toml", err=True)
        sys.exit(1)

    from .network import apply_allowlist

    apply_allowlist(runtime, name, domains)
    click.echo(f"Network allowlist applied to '{name}' ({len(domains)} domains).")


@network_group.command("remove")
@click.argument("name")
def network_remove(name):
    """Remove network restrictions from a bubble."""
    config = load_config()
    runtime = get_runtime(config, ensure_ready=False)
    _ensure_running(runtime, name)

    from .network import remove_allowlist

    remove_allowlist(runtime, name)
    click.echo(f"Network restrictions removed from '{name}'.")


# ---------------------------------------------------------------------------
# automation
# ---------------------------------------------------------------------------


@main.group("automation")
def automation_group():
    """Manage automated tasks (git update, image refresh)."""


@automation_group.command("install")
def automation_install():
    """Install automation jobs (launchd on macOS, systemd on Linux)."""
    from .automation import install_automation

    installed = install_automation()
    if installed:
        for item in installed:
            click.echo(f"  Installed: {item}")
        click.echo("Automation installed.")
    else:
        click.echo("No automation installed (unsupported platform?).", err=True)


@automation_group.command("remove")
def automation_remove():
    """Remove all automation jobs."""
    from .automation import remove_automation

    removed = remove_automation()
    if removed:
        for item in removed:
            click.echo(f"  Removed: {item}")
        click.echo("Automation removed.")
    else:
        click.echo("No automation jobs found to remove.")


@automation_group.command("status")
def automation_status():
    """Show automation status."""
    from .automation import is_automation_installed

    status = is_automation_installed()
    if not status:
        click.echo("Automation not supported on this platform.")
        return
    for job, installed in status.items():
        state = "installed" if installed else "not installed"
        click.echo(f"  {job}: {state}")


# ---------------------------------------------------------------------------
# relay
# ---------------------------------------------------------------------------


@main.group("relay")
def relay_group():
    """Manage the bubble-in-bubble relay."""


@relay_group.command("enable")
def relay_enable():
    """Enable bubble-in-bubble relay.

    This allows containers to request creation of new bubbles on the host.
    Only repos already cloned in ~/.bubble/git/ can be opened via relay.
    All relay requests are rate-limited and logged.
    """
    click.echo("Enabling bubble-in-bubble relay.")
    click.echo()
    click.echo("This opens a controlled channel from containers to the host.")
    click.echo("Mitigations: known repos only, rate limiting, request logging.")
    click.echo()

    config = load_config()
    config.setdefault("relay", {})["enabled"] = True
    save_config(config)

    # Install and start the relay daemon
    from .automation import install_relay_daemon

    try:
        result = install_relay_daemon()
        if result:
            click.echo(f"  Installed: {result}")
    except Exception as e:
        click.echo(f"  Warning: could not install daemon: {e}")
        click.echo("  You can start it manually with: bubble relay daemon")

    click.echo()
    click.echo("Relay enabled. New bubbles will include the relay socket.")
    click.echo("Existing bubbles need to be recreated to get relay access.")


@relay_group.command("disable")
def relay_disable():
    """Disable bubble-in-bubble relay."""
    config = load_config()
    config.setdefault("relay", {})["enabled"] = False
    save_config(config)

    from .automation import remove_relay_daemon

    try:
        result = remove_relay_daemon()
        if result:
            click.echo(f"  Removed: {result}")
    except Exception:
        pass

    # Remove socket/port file
    from .relay import RELAY_PORT_FILE, RELAY_SOCK

    RELAY_SOCK.unlink(missing_ok=True)
    RELAY_PORT_FILE.unlink(missing_ok=True)

    click.echo("Relay disabled.")


@relay_group.command("status")
def relay_status():
    """Show relay status."""
    config = load_config()
    enabled = config.get("relay", {}).get("enabled", False)
    click.echo(f"  Relay: {'enabled' if enabled else 'disabled'}")

    from .relay import RELAY_PORT_FILE, RELAY_SOCK

    if platform.system() == "Darwin":
        if RELAY_PORT_FILE.exists():
            port = RELAY_PORT_FILE.read_text().strip()
            click.echo(f"  Listening: TCP 127.0.0.1:{port}")
        else:
            click.echo("  Listening: not running")
    else:
        click.echo(f"  Socket: {'exists' if RELAY_SOCK.exists() else 'not found'}")

    from .relay import RELAY_LOG

    if RELAY_LOG.exists():
        # Show last 5 log entries
        lines = RELAY_LOG.read_text().strip().splitlines()
        if lines:
            click.echo(f"  Log ({len(lines)} entries, last 5):")
            for line in lines[-5:]:
                click.echo(f"    {line}")
        else:
            click.echo("  Log: empty")
    else:
        click.echo("  Log: no requests yet")


@relay_group.command("daemon")
def relay_daemon_cmd():
    """Run the relay daemon (used by launchd/systemd)."""
    from .relay import run_daemon

    run_daemon()


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# remote
# ---------------------------------------------------------------------------


@main.group("remote")
def remote_group():
    """Manage remote SSH host settings."""


@remote_group.command("set-default")
@click.argument("host")
def remote_set_default(host):
    """Set the default remote SSH host for new bubbles.

    HOST can be: hostname, user@hostname, or user@hostname:port
    """
    from .remote import RemoteHost

    # Validate the spec parses
    parsed = RemoteHost.parse(host)
    config = load_config()
    if "remote" not in config:
        config["remote"] = {}
    config["remote"]["default_host"] = parsed.spec_string()
    save_config(config)
    click.echo(f"Default remote host set to: {parsed.spec_string()}")


@remote_group.command("clear-default")
def remote_clear_default():
    """Clear the default remote SSH host."""
    config = load_config()
    if "remote" in config:
        config["remote"]["default_host"] = ""
        save_config(config)
    click.echo("Default remote host cleared.")


@remote_group.command("status")
def remote_status():
    """Show current remote host configuration."""
    config = load_config()
    default = config.get("remote", {}).get("default_host", "")
    if default:
        click.echo(f"Default remote host: {default}")
    else:
        click.echo("No default remote host configured.")

    # Show remote bubbles from registry
    registry = load_registry()
    remote_bubbles = [
        (name, info) for name, info in registry.get("bubbles", {}).items()
        if info.get("remote_host")
    ]
    if remote_bubbles:
        click.echo(f"\nRemote bubbles ({len(remote_bubbles)}):")
        for name, info in remote_bubbles:
            click.echo(f"  {name:<30} {info['remote_host']}")


# ---------------------------------------------------------------------------
# cloud
# ---------------------------------------------------------------------------


@main.group("cloud")
def cloud_group():
    """Manage Hetzner Cloud server for remote bubbles."""


@cloud_group.command("provision")
@click.option("--type", "server_type", type=str, default=None,
              help="Server type (e.g. ccx43, cx53)")
@click.option("--location", type=str, default=None,
              help="Datacenter location (default: fsn1)")
def cloud_provision(server_type, location):
    """Provision a Hetzner Cloud server for bubble.

    Creates a server with Incus pre-installed. The server auto-shuts down
    after 15 minutes of idle (no SSH connections + low CPU), stopping
    hourly billing. It auto-starts again on next 'bubble open --cloud'.

    \b
    Common server types:
      ccx43   16 dedicated vCPU, 64GB RAM (~EUR 0.13/hr)
      cx53    16 shared vCPU, 32GB RAM (~EUR 0.024/hr)
      cx33     4 shared vCPU,  8GB RAM (~EUR 0.008/hr)
    """
    from .cloud import provision_server
    config = load_config()
    provision_server(config, server_type=server_type, location=location)


@cloud_group.command("destroy")
@click.option("-f", "--force", is_flag=True, help="Skip confirmation prompt")
def cloud_destroy(force):
    """Destroy the cloud server permanently."""
    from .cloud import destroy_server
    destroy_server(force=force)


@cloud_group.command("stop")
def cloud_stop():
    """Power off the cloud server (stops hourly billing).

    Containers are preserved on disk and will be available after restart.
    """
    from .cloud import stop_server
    stop_server()


@cloud_group.command("start")
def cloud_start():
    """Power on the cloud server and wait for SSH."""
    from .cloud import start_server
    start_server()


@cloud_group.command("status")
def cloud_status():
    """Show cloud server info and status."""
    from .cloud import get_server_status
    status = get_server_status()
    if not status:
        click.echo("No cloud server provisioned.")
        click.echo("Set one up with: bubble cloud provision --type ccx43")
        return

    click.echo(f"  Server:   {status.get('server_name', '?')}")
    click.echo(f"  ID:       {status.get('server_id', '?')}")
    click.echo(f"  IP:       {status.get('ipv4', '?')}")
    click.echo(f"  Type:     {status.get('server_type', '?')}")
    click.echo(f"  Location: {status.get('location', '?')}")
    click.echo(f"  Status:   {status.get('status', 'unknown')}")
    if status.get("server_type_description"):
        click.echo(f"  Specs:    {status['server_type_description']}")


@cloud_group.command("ssh")
@click.argument("args", nargs=-1)
def cloud_ssh_cmd(args):
    """SSH directly to the cloud server."""
    from .cloud import cloud_ssh
    cloud_ssh(list(args) if args else None)


@cloud_group.command("default")
@click.argument("setting", required=False, type=click.Choice(["on", "off"]))
def cloud_default(setting):
    """Set whether cloud is the default for all 'bubble open'.

    When on, all bubbles go to cloud unless --local is used.
    Shows current setting if no argument given.
    """
    config = load_config()
    if setting is None:
        current = config.get("cloud", {}).get("default", False)
        state = "on" if current else "off"
        click.echo(f"Cloud default: {state}")
        if current:
            click.echo("All 'bubble open' commands use cloud. Use --local to override.")
        else:
            click.echo("Use --cloud flag or: bubble cloud default on")
        return
    config.setdefault("cloud", {})["default"] = (setting == "on")
    save_config(config)
    if setting == "on":
        click.echo("Cloud set as default. All 'bubble open' will use cloud.")
        click.echo("Override with: bubble open --local <target>")
    else:
        click.echo("Cloud default disabled. Use --cloud flag for cloud bubbles.")


@main.command()
def doctor():
    """Diagnose and fix common bubble issues."""
    import platform
    import re

    config = load_config()
    issues = 0
    fixed = 0
    saved_tty = _save_terminal()

    # 1. Check Colima (macOS only)
    if platform.system() == "Darwin":
        from .runtime.colima import is_colima_running

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
                    from .runtime.colima import start_colima

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
            capture_output=True, text=True, check=True, stdin=subprocess.DEVNULL,
        )
        _restore_terminal(saved_tty)
        import json

        all_ops = json.loads(result.stdout) if result.stdout.strip() else []
        # websocket ops are active exec/console sessions (e.g. VS Code SSH), not stuck
        stuck = [op for op in all_ops if op.get("class") != "websocket"]
        if stuck:
            click.echo(f"  Found {len(stuck)} stuck operation(s):")
            for op in stuck:
                desc = op.get("description", "unknown")
                status = op.get("status", "unknown")
                click.echo(f"    {desc} ({status})")
            issues += len(stuck)
            if click.confirm("  Cancel stuck operations?"):
                cancelled = 0
                for op in stuck:
                    op_id = op.get("id", "")
                    if not op_id:
                        continue
                    try:
                        subprocess.run(
                            ["incus", "operation", "delete", op_id],
                            capture_output=True, check=True, timeout=10,
                            stdin=subprocess.DEVNULL,
                        )
                        cancelled += 1
                    except Exception:
                        pass
                if cancelled:
                    click.echo(f"  Cancelled {cancelled} operation(s).")
                    fixed += cancelled
                else:
                    click.echo("  Could not cancel operations.", err=True)
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


# ---------------------------------------------------------------------------
# editor
# ---------------------------------------------------------------------------


@main.command("editor")
@click.argument("choice", required=False,
                type=click.Choice(["vscode", "emacs", "neovim", "shell"]))
def editor_cmd(choice):
    """Get or set the default editor for new bubbles."""
    config = load_config()
    if choice is None:
        current = config.get("editor", "vscode")
        click.echo(f"Current editor: {current}")
        click.echo("Set with: bubble editor vscode|emacs|neovim|shell")
        return
    config["editor"] = choice
    save_config(config)
    click.echo(f"Default editor set to: {choice}")


if __name__ == "__main__":
    main()
