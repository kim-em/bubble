"""Remote SSH host support for running bubbles on remote machines."""

import json
import os
import re
import shlex
import subprocess
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path

from . import __version__

_SAFE_NAME_RE = re.compile(r"^[a-zA-Z0-9_][a-zA-Z0-9._-]*$")


@dataclass
class RemoteHost:
    """SSH remote host specification."""

    hostname: str
    user: str | None = None
    port: int = 22

    @classmethod
    def parse(cls, spec: str) -> "RemoteHost":
        """Parse a remote host specification.

        Supported formats:
          host
          user@host
          host:port
          user@host:port
        """
        user = None
        port = 22

        # Extract user@ prefix
        if "@" in spec:
            user, spec = spec.rsplit("@", 1)
            if not user:
                raise ValueError(f"Empty user in SSH spec: {spec!r}")

        # Extract :port suffix
        if ":" in spec:
            host_part, port_str = spec.rsplit(":", 1)
            try:
                port = int(port_str)
            except ValueError:
                raise ValueError(f"Invalid port in SSH spec: {port_str!r}")
            if not 1 <= port <= 65535:
                raise ValueError(f"Port out of range: {port}")
            spec = host_part

        if not spec:
            raise ValueError("Empty hostname in SSH spec")

        # Validate hostname and user to prevent SSH option injection
        # (e.g., "-oProxyCommand=...") and shell metacharacter injection.
        if not _SAFE_NAME_RE.match(spec):
            raise ValueError(
                f"Invalid hostname: {spec!r} "
                f"(must be alphanumeric, dots, hyphens; cannot start with -)"
            )
        if user and not _SAFE_NAME_RE.match(user):
            raise ValueError(
                f"Invalid user: {user!r} "
                f"(must be alphanumeric, dots, hyphens; cannot start with -)"
            )

        return cls(hostname=spec, user=user, port=port)

    @property
    def ssh_destination(self) -> str:
        """Return 'user@host' or just 'host'."""
        if self.user:
            return f"{self.user}@{self.hostname}"
        return self.hostname

    def ssh_cmd(self, command: list[str]) -> list[str]:
        """Build SSH command: ['ssh', '-p', port, destination] + command."""
        cmd = ["ssh"]
        if self.port != 22:
            cmd += ["-p", str(self.port)]
        cmd.append(self.ssh_destination)
        cmd += command
        return cmd

    def scp_cmd(self, local_path: str, remote_path: str) -> list[str]:
        """Build SCP command to copy a file to the remote."""
        cmd = ["scp", "-q"]
        if self.port != 22:
            cmd += ["-P", str(self.port)]
        cmd += [local_path, f"{self.ssh_destination}:{remote_path}"]
        return cmd

    def spec_string(self) -> str:
        """Return the canonical spec string for this host."""
        s = self.ssh_destination
        if self.port != 22:
            s += f":{self.port}"
        return s


REMOTE_DIR = "/tmp/bubble-remote"


def _find_package_dirs() -> dict[str, Path]:
    """Find installed package directories for bubble and its pure-Python deps."""
    import click
    import tomli_w

    import bubble

    packages = {"bubble": bubble, "click": click, "tomli_w": tomli_w}

    # tomli is only needed for Python < 3.11 (otherwise tomllib is in stdlib)
    try:
        import tomli

        packages["tomli"] = tomli
    except ImportError:
        pass

    dirs = {}
    for name, mod in packages.items():
        mod_file = getattr(mod, "__file__", None)
        if mod_file:
            dirs[name] = Path(mod_file).parent
    return dirs


def _create_bundle() -> Path:
    """Create a tarball of bubble and its pure-Python dependencies.

    Returns the path to the temporary tarball.
    """
    packages = _find_package_dirs()
    fd, bundle_path_str = tempfile.mkstemp(suffix=".tar.gz", prefix="bubble-bundle-")
    os.close(fd)
    bundle_path = Path(bundle_path_str)

    with tarfile.open(bundle_path, "w:gz") as tar:
        for name, pkg_dir in packages.items():
            # Use the directory name as the arcname (e.g., bubble/, click/)
            for root, dirs, files in os.walk(pkg_dir):
                # Skip __pycache__ directories
                dirs[:] = [d for d in dirs if d != "__pycache__"]
                for f in files:
                    if f.endswith((".pyc", ".pyo")):
                        continue
                    filepath = Path(root) / f
                    arcname = str(Path(name) / filepath.relative_to(pkg_dir))
                    tar.add(filepath, arcname=arcname)

    return bundle_path


def _ssh_run(
    host: RemoteHost,
    command: list[str],
    check: bool = True,
    capture: bool = True,
    timeout: int = 30,
) -> subprocess.CompletedProcess:
    """Run a command on the remote host via SSH."""
    # SSH concatenates args after the destination and passes them to the
    # remote shell.  Shell-quote each part so spaces and metacharacters
    # in any argument are preserved correctly on the remote side.
    quoted_cmd = " ".join(shlex.quote(a) for a in command)
    ssh_cmd = host.ssh_cmd([quoted_cmd])
    return subprocess.run(
        ssh_cmd,
        capture_output=capture,
        text=True,
        check=check,
        timeout=timeout,
    )


def _parse_python_version(version_str: str) -> tuple[int, int] | None:
    """Parse 'Python 3.X.Y' into (major, minor), or None."""
    try:
        parts = version_str.split()[1].split(".")
        return int(parts[0]), int(parts[1])
    except (IndexError, ValueError):
        return None


# Cache: host spec -> python binary path
_remote_python_cache: dict[str, str] = {}

_PYTHON_CANDIDATES = [
    "python3",
    "/usr/local/bin/python3",
    "/opt/homebrew/bin/python3",
    "python3.14",
    "python3.13",
    "python3.12",
    "python3.11",
    "python3.10",
]


def _find_remote_python(host: RemoteHost) -> str:
    """Find a Python >= 3.10 on the remote host.

    Probes multiple common paths since on macOS, `python3` may resolve to the
    Xcode-bundled 3.9 while a newer version exists at /usr/local/bin/python3.
    Caches the result per host.
    """
    cache_key = host.spec_string()
    if cache_key in _remote_python_cache:
        return _remote_python_cache[cache_key]

    best_bin = None
    best_ver = (0, 0)

    for candidate in _PYTHON_CANDIDATES:
        try:
            result = _ssh_run(host, [candidate, "--version"], check=False, timeout=5)
            if result.returncode != 0:
                continue
            ver = _parse_python_version(result.stdout.strip())
            if ver and (ver[0] > 3 or (ver[0] == 3 and ver[1] >= 10)):
                if ver > best_ver:
                    best_bin = candidate
                    best_ver = ver
                    # If we found one via the default name, good enough
                    if candidate == "python3":
                        break
        except (subprocess.TimeoutExpired, FileNotFoundError):
            continue

    if not best_bin:
        raise RuntimeError(
            f"No Python >= 3.10 found on {host.ssh_destination}.\n"
            f"Checked: {', '.join(_PYTHON_CANDIDATES)}\n"
            f"Install Python 3.10+ and ensure it's on PATH."
        )

    _remote_python_cache[cache_key] = best_bin
    return best_bin


def _check_remote_version(host: RemoteHost) -> bool:
    """Check if the deployed bubble version on the remote matches the local version.

    Returns True if versions match (no redeploy needed).
    """
    try:
        result = _ssh_run(
            host,
            ["cat", f"{REMOTE_DIR}/.version"],
            check=False,
            timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip() == __version__:
            return True
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return False


def ensure_remote_bubble(host: RemoteHost) -> None:
    """Deploy bubble to the remote host if needed.

    Bundles the local bubble package and its pure-Python dependencies,
    copies them to the remote via scp, and verifies the deployment.
    Skips redeployment if the remote version matches the local version.
    """
    import click as click_mod

    # Check if already deployed with matching version
    if _check_remote_version(host):
        return

    click_mod.echo(f"Deploying bubble {__version__} to {host.ssh_destination}...")

    # Find a suitable Python on the remote
    _find_remote_python(host)

    # Create bundle tarball
    bundle_path = _create_bundle()
    try:
        # Create remote directory and clean old deployment
        _ssh_run(host, ["rm", "-rf", REMOTE_DIR], check=False, timeout=15)
        _ssh_run(host, ["mkdir", "-p", REMOTE_DIR], timeout=10)
        _ssh_run(host, ["chmod", "700", REMOTE_DIR], timeout=10)

        # Copy bundle to remote
        remote_tarball = f"{REMOTE_DIR}/bundle.tar.gz"
        scp_cmd = host.scp_cmd(str(bundle_path), remote_tarball)
        subprocess.run(scp_cmd, check=True, capture_output=True, timeout=60)

        # Extract on remote
        _ssh_run(
            host,
            ["tar", "xzf", remote_tarball, "-C", REMOTE_DIR],
            timeout=30,
        )

        # Clean up tarball on remote
        _ssh_run(host, ["rm", "-f", remote_tarball], check=False, timeout=10)

        # Verify deployment before writing version marker
        result = remote_bubble(host, ["--version"], timeout=15)
        if result.returncode != 0:
            raise RuntimeError(
                f"Bubble deployment verification failed on {host.ssh_destination}.\n"
                f"stderr: {result.stderr}"
            )

        # Write version marker only after verification succeeds
        _ssh_run(
            host,
            ["sh", "-c", f"echo {shlex.quote(__version__)} > {REMOTE_DIR}/.version"],
            timeout=10,
        )

        click_mod.echo(f"Deployed bubble {__version__} to {host.ssh_destination}.")
    finally:
        bundle_path.unlink(missing_ok=True)


def remote_bubble(
    host: RemoteHost,
    args: list[str],
    timeout: int | None = None,
    capture: bool = True,
) -> subprocess.CompletedProcess:
    """Run a bubble command on the remote host via SSH.

    Invokes: ssh host PYTHONPATH=/tmp/bubble-remote <python> -m bubble <args>
    """
    python_bin = _find_remote_python(host)
    remote_parts = [
        f"PYTHONPATH={REMOTE_DIR}",
        python_bin,
        "-m",
        "bubble",
    ] + args
    # Shell-quote each part for safe transport through SSH.
    quoted_cmd = " ".join(shlex.quote(a) for a in remote_parts)

    ssh_cmd = host.ssh_cmd([quoted_cmd])
    return subprocess.run(
        ssh_cmd,
        capture_output=capture,
        text=True,
        check=False,
        timeout=timeout,
    )


_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]|\x1b[^[\[]")


def _sanitize_output(text: str) -> str:
    """Strip ANSI escape sequences from remote output."""
    return _ANSI_ESCAPE_RE.sub("", text)


def remote_open(
    host: RemoteHost,
    target: str,
    network: bool = True,
    custom_name: str | None = None,
    git_name: str = "",
    git_email: str = "",
) -> dict:
    """Open a bubble on a remote host.

    Deploys bubble to the remote if needed, runs `bubble open` remotely
    with --machine-readable, and returns the parsed JSON result.
    Streams progress output to the local terminal as it arrives.
    """
    import click as click_mod

    ensure_remote_bubble(host)

    args = ["open", "--no-interactive", "--machine-readable"]
    if not network:
        args.append("--no-network")
    if custom_name:
        args += ["--name", custom_name]
    if git_name:
        args += ["--git-name", git_name]
    if git_email:
        args += ["--git-email", git_email]
    args.append(target)

    click_mod.echo(f"Creating bubble on {host.ssh_destination}...")

    # Build the SSH command
    python_bin = _find_remote_python(host)
    remote_parts = [
        f"PYTHONPATH={REMOTE_DIR}",
        python_bin,
        "-m",
        "bubble",
    ] + args
    quoted_cmd = " ".join(shlex.quote(a) for a in remote_parts)
    ssh_cmd = host.ssh_cmd([quoted_cmd])

    # Stream stdout line-by-line so the user sees progress, while collecting
    # all lines for JSON parsing at the end. Read stderr in a separate thread
    # to avoid deadlock if the stderr buffer fills while we drain stdout.
    import threading

    proc = subprocess.Popen(
        ssh_cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    stderr_chunks = []

    def _drain_stderr():
        stderr_chunks.append(proc.stderr.read())

    stderr_thread = threading.Thread(target=_drain_stderr, daemon=True)
    stderr_thread.start()

    stdout_lines = []
    for line in proc.stdout:
        stripped = line.rstrip("\n")
        if not stripped:
            continue
        stdout_lines.append(stripped)
        # Don't echo the final JSON result line
        if not stripped.startswith("{"):
            click_mod.echo(f"  {_sanitize_output(stripped)}")

    proc.wait(timeout=600)
    stderr_thread.join(timeout=5)
    stderr_output = stderr_chunks[0] if stderr_chunks else ""

    if proc.returncode != 0:
        stderr = _sanitize_output(stderr_output.strip())
        stdout = _sanitize_output("\n".join(stdout_lines))
        msg = stderr or stdout or "Unknown error"
        raise RuntimeError(f"Remote bubble open failed: {msg}")

    if not stdout_lines:
        raise RuntimeError(
            f"Empty output from remote bubble.\n"
            f"stderr: {_sanitize_output(stderr_output)}"
        )
    try:
        data = json.loads(stdout_lines[-1])
    except json.JSONDecodeError:
        raise RuntimeError(
            f"Failed to parse remote bubble output.\n"
            f"stdout: {_sanitize_output(chr(10).join(stdout_lines))}\n"
            f"stderr: {_sanitize_output(stderr_output)}"
        )

    if data.get("status") == "error":
        raise RuntimeError(f"Remote bubble error: {data.get('message', 'Unknown error')}")

    return data


def remote_command(
    host: RemoteHost,
    args: list[str],
    timeout: int = 120,
) -> subprocess.CompletedProcess:
    """Run an arbitrary bubble command on the remote host.

    Used for pause, destroy, list, etc. Ensures bubble is deployed first.
    """
    ensure_remote_bubble(host)
    return remote_bubble(host, args, timeout=timeout)
