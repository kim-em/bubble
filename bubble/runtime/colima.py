"""macOS Colima management for running Incus."""

import subprocess


def is_colima_running() -> bool:
    try:
        result = subprocess.run(
            ["colima", "status"],
            capture_output=True, text=True, check=False, stdin=subprocess.DEVNULL,
        )
        return result.returncode == 0
    except FileNotFoundError:
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
        f"--vm-type={vm_type}",
    ]
    subprocess.run(args, check=True, stdin=subprocess.DEVNULL)


def ensure_colima(cpu: int, memory: int, disk: int = 60, vm_type: str = "vz"):
    """Ensure Colima is running with correct settings. Restart if needed."""
    if is_colima_running():
        return

    print("Starting Colima VM (one-time setup)...")
    start_colima(cpu, memory, disk, vm_type)
