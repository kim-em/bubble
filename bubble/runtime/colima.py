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


def _colima_supports_vm_type() -> bool:
    """Check if colima supports the --vm-type flag."""
    try:
        result = subprocess.run(
            ["colima", "start", "--help"],
            capture_output=True, text=True, timeout=5, stdin=subprocess.DEVNULL,
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
    subprocess.run(args, check=True, stdin=subprocess.DEVNULL)


def ensure_colima(cpu: int, memory: int, disk: int = 60, vm_type: str = "vz"):
    """Ensure Colima is running with correct settings. Restart if needed."""
    if is_colima_running():
        return

    print("Starting Colima VM (one-time setup)...")
    start_colima(cpu, memory, disk, vm_type)
