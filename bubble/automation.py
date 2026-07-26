"""Automation setup for periodic tasks (git update, image refresh).

Supports:
- macOS: launchd plists in ~/Library/LaunchAgents/
- Linux: systemd user timers in ~/.config/systemd/user/
- Fallback: cron jobs
"""

import os
import platform
import plistlib
import subprocess
import sys
import textwrap
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable

from .config import HOST_DATA_DIR

LAUNCHD_LABELS = {
    "git-update": "com.bubble.git-update",
    "image-refresh": "com.bubble.image-refresh",
}
RELAY_LABEL = "com.bubble.relay-daemon"
AUTH_PROXY_LABEL = "com.bubble.auth-proxy"


def install_automation() -> list[str]:
    """Install automation jobs. Returns list of what was installed."""
    system = platform.system()
    if system == "Darwin":
        return _install_launchd()
    elif system == "Linux":
        return _install_systemd()
    else:
        return []


def remove_automation() -> list[str]:
    """Remove automation jobs. Returns list of what was removed."""
    system = platform.system()
    if system == "Darwin":
        return _remove_launchd()
    elif system == "Linux":
        return _remove_systemd()
    else:
        return []


def is_automation_installed() -> dict[str, bool]:
    """Check which automation jobs are installed."""
    system = platform.system()
    if system == "Darwin":
        return _check_launchd()
    elif system == "Linux":
        return _check_systemd()
    return {}


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _bubble_path() -> str:
    """Find the Bubble executable, preferring the CLI currently being run."""
    import shutil as _shutil

    invoked = Path(sys.argv[0])
    if invoked.name == "bubble" and invoked.exists():
        return str(invoked.resolve())
    path = _shutil.which("bubble")
    return path if path else "bubble"


def _systemd_path_env() -> str:
    """Return an Environment=PATH=... line for systemd service files.

    Captures the user's current PATH so that tools like gh, incus, etc.
    are discoverable when the service runs under systemd (which provides
    only a minimal default PATH).

    Escapes '%' as '%%' since systemd treats '%' as a specifier prefix
    in unit files (e.g., %h = home directory).
    """
    path = os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")
    # Escape % for systemd specifier syntax
    path = path.replace("%", "%%")
    return f"Environment=PATH={path}"


# ---------------------------------------------------------------------------
# macOS: launchd
# ---------------------------------------------------------------------------

# Job definitions: label -> (args_suffix, extra_plist_keys)
_LAUNCHD_JOBS = {
    "com.bubble.git-update": {
        "args": ["git", "update"],
        "extra": {
            "StartInterval": 3600,
            "RunAtLoad": False,
        },
        "log": "/tmp/bubble-git-update.log",
    },
    "com.bubble.image-refresh": {
        "args": ["images", "build", "base"],
        "extra": {
            "StartCalendarInterval": {"Hour": 3, "Weekday": 0},
            "RunAtLoad": False,
        },
        "log": "/tmp/bubble-image-refresh.log",
    },
}

_RELAY_JOB = {
    "args": ["relay", "daemon"],
    "extra": {
        "KeepAlive": True,
        "RunAtLoad": True,
    },
    "log": "/tmp/bubble-relay-daemon.log",
}

_AUTH_PROXY_JOB = {
    "args": ["gh", "proxy", "daemon"],
    "extra": {
        "KeepAlive": True,
        "RunAtLoad": True,
    },
    "log": "/tmp/bubble-auth-proxy.log",
}


def _write_launchd_plist(label: str, job: dict, *, host_global: bool = False) -> str:
    """Generate and install a launchd plist. Returns the installed path."""
    home = HOST_DATA_DIR.parent if host_global else Path.home()
    launch_agents = home / "Library" / "LaunchAgents"
    launch_agents.mkdir(parents=True, exist_ok=True)
    dst = launch_agents / f"{label}.plist"
    domain = f"gui/{os.getuid()}"
    service = f"{domain}/{label}"

    bubble = _bubble_path()
    plist = {
        "Label": label,
        "ProgramArguments": [bubble] + job["args"],
        "StandardOutPath": job["log"],
        "StandardErrorPath": job["log"],
        # Inherit the current PATH so that tools like gh are discoverable.
        "EnvironmentVariables": {"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
    }
    plist.update(job["extra"])

    tmp = dst.with_name(f".{dst.name}.{os.getpid()}.tmp")
    with open(tmp, "wb") as f:
        plistlib.dump(plist, f)
    os.replace(tmp, dst)

    # Address the registered service by label, not by its old plist path. This
    # also removes jobs installed while HOME pointed at an isolated directory.
    subprocess.run(["launchctl", "bootout", service], capture_output=True)

    # launchd can briefly return EIO immediately after a successful bootout;
    # retrying bootstrap lets its service registry settle without an unbounded
    # `bootout --wait` (which launchctl itself warns may block indefinitely).
    for attempt in range(5):
        result = subprocess.run(
            ["launchctl", "bootstrap", domain, str(dst)],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            break
        if attempt < 4:
            time.sleep(0.2)
    else:
        detail = (result.stderr or result.stdout or "unknown launchctl error").strip()
        raise RuntimeError(f"could not bootstrap {label}: {detail}")
    return str(dst)


def _remove_launchd_job(label: str, *, host_global: bool = False) -> str | None:
    """Remove a single launchd job. Returns description or None."""
    home = HOST_DATA_DIR.parent if host_global else Path.home()
    launch_agents = home / "Library" / "LaunchAgents"
    dst = launch_agents / f"{label}.plist"

    if dst.exists():
        domain = f"gui/{os.getuid()}"
        subprocess.run(["launchctl", "bootout", f"{domain}/{label}"], capture_output=True)
        dst.unlink()
        return f"launchd: {label}"

    return None


def _install_launchd() -> list[str]:
    installed = []
    for label, job in _LAUNCHD_JOBS.items():
        _write_launchd_plist(label, job)
        installed.append(f"launchd: {label}")
    return installed


def _remove_launchd() -> list[str]:
    removed = []
    for label in LAUNCHD_LABELS.values():
        result = _remove_launchd_job(label)
        if result:
            removed.append(result)
    return removed


def _check_launchd() -> dict[str, bool]:
    launch_agents = Path.home() / "Library" / "LaunchAgents"
    result = {}
    for job_name, label in LAUNCHD_LABELS.items():
        dst = launch_agents / f"{label}.plist"
        result[job_name] = dst.exists()
    return result


# ---------------------------------------------------------------------------
# Linux: systemd user timers
# ---------------------------------------------------------------------------

SYSTEMD_DIR = Path.home() / ".config" / "systemd" / "user"
HOST_SYSTEMD_DIR = HOST_DATA_DIR.parent / ".config" / "systemd" / "user"


def _systemctl_checked(args: list[str]) -> None:
    """Run a user systemd operation and surface failures to the caller."""
    result = subprocess.run(
        ["systemctl", "--user", *args],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "unknown systemctl error").strip()
        raise RuntimeError(f"systemctl --user {' '.join(args)} failed: {detail}")


def _install_systemd() -> list[str]:
    SYSTEMD_DIR.mkdir(parents=True, exist_ok=True)
    installed = []
    bubble = _bubble_path()

    # Git update: hourly
    git_service = SYSTEMD_DIR / "bubble-git-update.service"
    git_timer = SYSTEMD_DIR / "bubble-git-update.timer"

    git_service.write_text(
        textwrap.dedent(f"""\
        [Unit]
        Description=bubble git store update

        [Service]
        Type=oneshot
        ExecStart={bubble} git update
        {_systemd_path_env()}
    """)
    )

    git_timer.write_text(
        textwrap.dedent("""\
        [Unit]
        Description=Hourly bubble git store update

        [Timer]
        OnCalendar=hourly
        Persistent=true

        [Install]
        WantedBy=timers.target
    """)
    )
    installed.append("systemd: bubble-git-update.timer")

    # Image refresh: weekly (Sunday 3am)
    img_service = SYSTEMD_DIR / "bubble-image-refresh.service"
    img_timer = SYSTEMD_DIR / "bubble-image-refresh.timer"

    img_service.write_text(
        textwrap.dedent(f"""\
        [Unit]
        Description=bubble base image refresh

        [Service]
        Type=oneshot
        ExecStart={bubble} images build base
        {_systemd_path_env()}
    """)
    )

    img_timer.write_text(
        textwrap.dedent("""\
        [Unit]
        Description=Weekly bubble base image refresh

        [Timer]
        OnCalendar=Sun *-*-* 03:00:00
        Persistent=true

        [Install]
        WantedBy=timers.target
    """)
    )
    installed.append("systemd: bubble-image-refresh.timer")

    # Reload and enable
    subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
    subprocess.run(
        ["systemctl", "--user", "enable", "--now", "bubble-git-update.timer"],
        capture_output=True,
    )
    subprocess.run(
        ["systemctl", "--user", "enable", "--now", "bubble-image-refresh.timer"],
        capture_output=True,
    )

    return installed


def _remove_systemd() -> list[str]:
    removed = []

    for name in ["bubble-git-update", "bubble-image-refresh"]:
        timer = SYSTEMD_DIR / f"{name}.timer"
        service = SYSTEMD_DIR / f"{name}.service"

        if timer.exists():
            subprocess.run(
                ["systemctl", "--user", "disable", "--now", f"{name}.timer"], capture_output=True
            )
            timer.unlink()
            removed.append(f"systemd: {name}.timer")

        if service.exists():
            service.unlink()

    subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
    return removed


def _check_systemd() -> dict[str, bool]:
    result = {}
    for job_name, timer_name in [
        ("git-update", "bubble-git-update.timer"),
        ("image-refresh", "bubble-image-refresh.timer"),
    ]:
        timer_path = SYSTEMD_DIR / timer_name
        result[job_name] = timer_path.exists()
    return result


# ---------------------------------------------------------------------------
# Relay daemon (separate lifecycle from main automation)
# ---------------------------------------------------------------------------


def install_relay_daemon() -> str:
    """Install and start the relay daemon. Returns description of what was installed."""
    system = platform.system()
    if system == "Darwin":
        return _install_relay_launchd()
    elif system == "Linux":
        return _install_relay_systemd()
    return ""


def remove_relay_daemon() -> str:
    """Stop and remove the relay daemon. Returns description of what was removed."""
    system = platform.system()
    if system == "Darwin":
        return _remove_relay_launchd()
    elif system == "Linux":
        return _remove_relay_systemd()
    return ""


def _install_relay_launchd() -> str:
    _write_launchd_plist(RELAY_LABEL, _RELAY_JOB)
    return f"launchd: {RELAY_LABEL}"


def _remove_relay_launchd() -> str:
    return _remove_launchd_job(RELAY_LABEL) or ""


def _install_relay_systemd() -> str:
    SYSTEMD_DIR.mkdir(parents=True, exist_ok=True)
    bubble = _bubble_path()

    service = SYSTEMD_DIR / "bubble-relay.service"
    service.write_text(
        textwrap.dedent(f"""\
        [Unit]
        Description=bubble relay daemon

        [Service]
        Type=simple
        ExecStart={bubble} relay daemon
        Restart=always
        RestartSec=5
        {_systemd_path_env()}

        [Install]
        WantedBy=default.target
    """)
    )

    _systemctl_checked(["daemon-reload"])
    _systemctl_checked(["enable", "bubble-relay.service"])
    _systemctl_checked(["restart", "bubble-relay.service"])

    return "systemd: bubble-relay.service"


def _remove_relay_systemd() -> str:
    service = SYSTEMD_DIR / "bubble-relay.service"

    if service.exists():
        subprocess.run(
            ["systemctl", "--user", "disable", "--now", "bubble-relay.service"],
            capture_output=True,
        )
        service.unlink()
        subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
        return "systemd: bubble-relay.service"

    return ""


# ---------------------------------------------------------------------------
# Auth proxy daemon (separate lifecycle, started on demand by --gh-token)
# ---------------------------------------------------------------------------


@contextmanager
def _auth_proxy_install_lock():
    """Serialize changes to the one auth-proxy service owned by this OS user."""
    import fcntl

    HOST_DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(HOST_DATA_DIR / "auth-proxy.install.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def install_auth_proxy_daemon(*, skip_if: Callable[[], bool] | None = None) -> str:
    """Install and start the auth proxy daemon. Returns description of what was installed."""
    with _auth_proxy_install_lock():
        if skip_if is not None and skip_if():
            return "already running"
        system = platform.system()
        if system == "Darwin":
            return _install_auth_proxy_launchd()
        elif system == "Linux":
            return _install_auth_proxy_systemd()
        return ""


def remove_auth_proxy_daemon() -> str:
    """Stop and remove the auth proxy daemon. Returns description of what was removed."""
    system = platform.system()
    if system == "Darwin":
        return _remove_auth_proxy_launchd()
    elif system == "Linux":
        return _remove_auth_proxy_systemd()
    return ""


def is_auth_proxy_installed() -> bool:
    """Check if the auth proxy daemon is installed."""
    system = platform.system()
    if system == "Darwin":
        launch_agents = HOST_DATA_DIR.parent / "Library" / "LaunchAgents"
        return (launch_agents / f"{AUTH_PROXY_LABEL}.plist").exists()
    elif system == "Linux":
        return (HOST_SYSTEMD_DIR / "bubble-auth-proxy.service").exists()
    return False


def _install_auth_proxy_launchd() -> str:
    _write_launchd_plist(AUTH_PROXY_LABEL, _AUTH_PROXY_JOB, host_global=True)
    return f"launchd: {AUTH_PROXY_LABEL}"


def _remove_auth_proxy_launchd() -> str:
    return _remove_launchd_job(AUTH_PROXY_LABEL, host_global=True) or ""


def _install_auth_proxy_systemd() -> str:
    HOST_SYSTEMD_DIR.mkdir(parents=True, exist_ok=True)
    bubble = _bubble_path()

    service = HOST_SYSTEMD_DIR / "bubble-auth-proxy.service"
    service.write_text(
        textwrap.dedent(f"""\
        [Unit]
        Description=bubble git auth proxy daemon

        [Service]
        Type=simple
        ExecStart={bubble} gh proxy daemon
        Restart=always
        RestartSec=5
        {_systemd_path_env()}

        [Install]
        WantedBy=default.target
    """)
    )

    _systemctl_checked(["daemon-reload"])
    _systemctl_checked(["enable", "bubble-auth-proxy.service"])
    _systemctl_checked(["restart", "bubble-auth-proxy.service"])

    return "systemd: bubble-auth-proxy.service"


def _remove_auth_proxy_systemd() -> str:
    service = HOST_SYSTEMD_DIR / "bubble-auth-proxy.service"

    if service.exists():
        subprocess.run(
            ["systemctl", "--user", "disable", "--now", "bubble-auth-proxy.service"],
            capture_output=True,
        )
        service.unlink()
        subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
        return "systemd: bubble-auth-proxy.service"

    return ""
