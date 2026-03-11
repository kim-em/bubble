"""macOS Colima management for running Incus."""

import os
import select
import shutil
import subprocess
import sys
from pathlib import Path


def is_colima_running() -> bool:
    try:
        result = subprocess.run(
            ["colima", "status"],
            capture_output=True,
            text=True,
            check=False,
            stdin=subprocess.DEVNULL,
        )
        return result.returncode == 0
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


def _run_colima_start(args: list[str]) -> subprocess.CompletedProcess:
    """Run colima start, streaming output to the terminal while capturing it.

    Uses a PTY so that colima sees a terminal and doesn't block-buffer its
    output.  Stdout and stderr from the child are merged (as colima does
    internally) and echoed to our stderr so that normal stdout piping is
    unaffected.
    """
    parent_fd, child_fd = os.openpty()
    try:
        proc = subprocess.Popen(
            args,
            stdout=child_fd,
            stderr=child_fd,
            stdin=subprocess.DEVNULL,
        )
    finally:
        os.close(child_fd)

    output_chunks: list[str] = []
    try:
        while True:
            ready, _, _ = select.select([parent_fd], [], [], 0.1)
            if ready:
                try:
                    data = os.read(parent_fd, 4096)
                except OSError:
                    break
                if not data:
                    break
                decoded = data.decode("utf-8", errors="replace")
                output_chunks.append(decoded)
                sys.stderr.write(decoded)
                sys.stderr.flush()
            elif proc.poll() is not None:
                # Process exited; drain any remaining output
                try:
                    while True:
                        data = os.read(parent_fd, 4096)
                        if not data:
                            break
                        decoded = data.decode("utf-8", errors="replace")
                        output_chunks.append(decoded)
                        sys.stderr.write(decoded)
                        sys.stderr.flush()
                except OSError:
                    pass
                break
    finally:
        os.close(parent_fd)

    proc.wait()
    stdout = "".join(output_chunks)
    return subprocess.CompletedProcess(args, proc.returncode, stdout=stdout, stderr="")


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
    result = _run_colima_start(args)
    if result.returncode != 0:
        if "already exists" in result.stdout:
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
            result = _run_colima_start(args)
            if result.returncode != 0:
                raise subprocess.CalledProcessError(
                    result.returncode, args, output=result.stdout
                )
        else:
            raise subprocess.CalledProcessError(
                result.returncode, args, output=result.stdout
            )


def ensure_colima(cpu: int, memory: int, disk: int = 60, vm_type: str = "vz"):
    """Ensure Colima is running with correct settings. Restart if needed."""
    if is_colima_running():
        return

    print("Starting Colima VM (one-time setup)...", file=sys.stderr)
    start_colima(cpu, memory, disk, vm_type)
