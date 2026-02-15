"""Automation setup for periodic tasks (git update, image refresh).

Supports:
- macOS: launchd plists in ~/Library/LaunchAgents/
- Linux: systemd user timers in ~/.config/systemd/user/
- Fallback: cron jobs
"""

import platform
import plistlib
import subprocess
import textwrap
from pathlib import Path

LAUNCHD_LABELS = {
    "git-update": "com.bubble.git-update",
    "image-refresh": "com.bubble.image-refresh",
}
RELAY_LABEL = "com.bubble.relay-daemon"


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
    """Find the bubble executable path."""
    import shutil as _shutil

    path = _shutil.which("bubble")
    return path if path else "bubble"


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


def _write_launchd_plist(label: str, job: dict) -> str:
    """Generate and install a launchd plist. Returns the installed path."""
    launch_agents = Path.home() / "Library" / "LaunchAgents"
    launch_agents.mkdir(parents=True, exist_ok=True)
    dst = launch_agents / f"{label}.plist"

    if dst.exists():
        subprocess.run(["launchctl", "unload", str(dst)], capture_output=True)

    bubble = _bubble_path()
    plist = {
        "Label": label,
        "ProgramArguments": [bubble] + job["args"],
        "StandardOutPath": job["log"],
        "StandardErrorPath": job["log"],
    }
    plist.update(job["extra"])

    with open(dst, "wb") as f:
        plistlib.dump(plist, f)

    subprocess.run(["launchctl", "load", str(dst)], capture_output=True)
    return str(dst)


def _remove_launchd_job(label: str) -> str | None:
    """Remove a single launchd job. Returns description or None."""
    launch_agents = Path.home() / "Library" / "LaunchAgents"
    dst = launch_agents / f"{label}.plist"

    if dst.exists():
        subprocess.run(["launchctl", "unload", str(dst)], capture_output=True)
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

        [Install]
        WantedBy=default.target
    """)
    )

    subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
    subprocess.run(
        ["systemctl", "--user", "enable", "--now", "bubble-relay.service"],
        capture_output=True,
    )

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
