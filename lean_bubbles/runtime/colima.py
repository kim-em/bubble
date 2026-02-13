"""macOS Colima management for running Incus."""

import subprocess
import json


def is_colima_installed() -> bool:
    try:
        subprocess.run(["colima", "version"], capture_output=True, check=True)
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


def is_colima_running() -> bool:
    try:
        result = subprocess.run(
            ["colima", "status"], capture_output=True, text=True, check=False
        )
        return result.returncode == 0
    except FileNotFoundError:
        return False


def get_colima_config() -> dict:
    """Get current Colima configuration."""
    try:
        result = subprocess.run(
            ["colima", "status", "--json"], capture_output=True, text=True, check=True
        )
        return json.loads(result.stdout)
    except (subprocess.CalledProcessError, FileNotFoundError, json.JSONDecodeError):
        return {}


def start_colima(cpu: int, memory: int, disk: int = 60, vm_type: str = "vz"):
    """Start Colima with incus runtime and specified resources."""
    args = [
        "colima",
        "start",
        "--runtime=incus",
        f"--cpu={cpu}",
        f"--memory={memory}",
        f"--disk={disk}",
        f"--vm-type={vm_type}",
    ]
    subprocess.run(args, check=True)


def stop_colima():
    subprocess.run(["colima", "stop"], check=True)


def ensure_colima(cpu: int, memory: int, disk: int = 60, vm_type: str = "vz"):
    """Ensure Colima is running with correct settings. Restart if needed."""
    if not is_colima_installed():
        raise RuntimeError(
            "Colima is not installed. Run: brew install colima incus"
        )

    if is_colima_running():
        # Check if config matches
        # For now, just ensure it's running. Future: check CPU/memory match.
        return

    start_colima(cpu, memory, disk, vm_type)
