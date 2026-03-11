"""macOS Colima management for running Incus."""

import json
import shutil
import subprocess
import sys
from pathlib import Path


def is_colima_running() -> bool:
    try:
        # colima status can fail even when the VM is running (e.g. empty
        # runtime field in colima 0.10.x), so fall back to colima list.
        result = subprocess.run(
            ["colima", "status"],
            capture_output=True,
            text=True,
            check=False,
            stdin=subprocess.DEVNULL,
        )
        if result.returncode == 0:
            return True
        result = subprocess.run(
            ["colima", "list", "--json"],
            capture_output=True,
            text=True,
            check=False,
            stdin=subprocess.DEVNULL,
        )
        if result.returncode == 0 and result.stdout.strip():
            for line in result.stdout.strip().splitlines():
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if entry.get("name") == "default" and entry.get("status") == "Running":
                    return True
        return False
    except FileNotFoundError:
        return False


def _colima_supports_vm_type() -> bool:
    """Check if colima supports the --vm-type flag."""
    try:
        result = subprocess.run(
            ["colima", "start", "--help"],
            capture_output=True,
            text=True,
            timeout=5,
            stdin=subprocess.DEVNULL,
        )
        return "--vm-type" in result.stdout
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def start_colima(cpu: int, memory: int, disk: int = 60, vm_type: str = "vz"):
    """Start Colima with incus runtime and specified resources."""
    args = [
        "colima",
        "start",
        "--runtime=incus",
        f"--cpu={cpu}",
        f"--memory={memory}",
        f"--disk={disk}",
    ]
    if _colima_supports_vm_type():
        args.append(f"--vm-type={vm_type}")
    result = subprocess.run(
        args, check=False, capture_output=True, text=True, stdin=subprocess.DEVNULL
    )
    if result.returncode != 0:
        if "already exists" in (result.stderr + result.stdout):
            # Stale instance exists but isn't running — delete and retry
            subprocess.run(
                ["colima", "delete", "--force"],
                check=False,
                stdin=subprocess.DEVNULL,
            )
            # colima delete can fail if lima.yaml is missing, leaving
            # the instance directory behind. Remove it manually.
            lima_dir = Path.home() / ".colima" / "_lima" / "colima"
            if lima_dir.exists():
                shutil.rmtree(lima_dir)
            subprocess.run(args, check=True, stdin=subprocess.DEVNULL)
        else:
            # Unknown error — print output and raise
            if result.stdout:
                print(result.stdout, end="")
            if result.stderr:
                print(result.stderr, end="")
            result.check_returncode()


def _ensure_incus_remote():
    """Ensure the incus client is configured to talk to Colima's incus socket."""
    sock = Path.home() / ".colima" / "default" / "incus.sock"
    if not sock.exists():
        return
    sock_uri = f"unix://{sock}"

    try:
        result = subprocess.run(
            ["incus", "remote", "get-default"],
            capture_output=True,
            text=True,
            check=True,
            stdin=subprocess.DEVNULL,
        )
        current = result.stdout.strip()
    except (FileNotFoundError, subprocess.CalledProcessError):
        current = ""

    if current == "colima":
        return

    # Add the colima remote if it doesn't exist
    result = subprocess.run(
        ["incus", "remote", "list", "--format=json"],
        capture_output=True,
        text=True,
        check=False,
        stdin=subprocess.DEVNULL,
    )
    if result.returncode == 0:
        try:
            remotes = json.loads(result.stdout)
        except json.JSONDecodeError:
            remotes = {}
        if "colima" not in remotes:
            subprocess.run(
                ["incus", "remote", "add", "colima", sock_uri],
                capture_output=True,
                text=True,
                check=False,
                stdin=subprocess.DEVNULL,
            )

    subprocess.run(
        ["incus", "remote", "switch", "colima"],
        capture_output=True,
        text=True,
        check=False,
        stdin=subprocess.DEVNULL,
    )


def ensure_colima(cpu: int, memory: int, disk: int = 60, vm_type: str = "vz"):
    """Ensure Colima is running with correct settings. Restart if needed."""
    if not is_colima_running():
        print("Starting Colima VM (one-time setup)...", file=sys.stderr)
        start_colima(cpu, memory, disk, vm_type)

    _ensure_incus_remote()
