"""Automation setup for periodic tasks (git update, image refresh).

Supports:
- macOS: launchd plists in ~/Library/LaunchAgents/
- Linux: systemd user timers in ~/.config/systemd/user/
- Fallback: cron jobs
"""

import platform
import shutil
import subprocess
import textwrap
from pathlib import Path

PLIST_DIR = Path(__file__).parent.parent / "config"
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
# macOS: launchd
# ---------------------------------------------------------------------------


def _install_launchd() -> list[str]:
    launch_agents = Path.home() / "Library" / "LaunchAgents"
    launch_agents.mkdir(parents=True, exist_ok=True)
    installed = []

    for job_name, label in LAUNCHD_LABELS.items():
        plist_name = f"{label}.plist"
        src = PLIST_DIR / plist_name
        dst = launch_agents / plist_name

        if dst.exists():
            # Unload first to update
            subprocess.run(["launchctl", "unload", str(dst)], capture_output=True)

        if src.exists():
            shutil.copy2(src, dst)
            subprocess.run(["launchctl", "load", str(dst)], capture_output=True)
            installed.append(f"launchd: {label}")

    return installed


def _remove_launchd() -> list[str]:
    launch_agents = Path.home() / "Library" / "LaunchAgents"
    removed = []

    for job_name, label in LAUNCHD_LABELS.items():
        plist_name = f"{label}.plist"
        dst = launch_agents / plist_name

        if dst.exists():
            subprocess.run(["launchctl", "unload", str(dst)], capture_output=True)
            dst.unlink()
            removed.append(f"launchd: {label}")

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


def _bubble_path() -> str:
    """Find the bubble executable path."""
    result = subprocess.run(["which", "bubble"], capture_output=True, text=True)
    if result.returncode == 0:
        return result.stdout.strip()
    return "bubble"


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
    launch_agents = Path.home() / "Library" / "LaunchAgents"
    launch_agents.mkdir(parents=True, exist_ok=True)

    plist_name = f"{RELAY_LABEL}.plist"
    src = PLIST_DIR / plist_name
    dst = launch_agents / plist_name

    if dst.exists():
        subprocess.run(["launchctl", "unload", str(dst)], capture_output=True)

    if src.exists():
        shutil.copy2(src, dst)
        subprocess.run(["launchctl", "load", str(dst)], capture_output=True)
        return f"launchd: {RELAY_LABEL}"

    return ""


def _remove_relay_launchd() -> str:
    launch_agents = Path.home() / "Library" / "LaunchAgents"
    plist_name = f"{RELAY_LABEL}.plist"
    dst = launch_agents / plist_name

    if dst.exists():
        subprocess.run(["launchctl", "unload", str(dst)], capture_output=True)
        dst.unlink()
        return f"launchd: {RELAY_LABEL}"

    return ""


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
