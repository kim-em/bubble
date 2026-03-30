"""macOS Colima management for running Incus."""

import json
import os
import select
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
            timeout=10,
            stdin=subprocess.DEVNULL,
        )
        if result.returncode == 0:
            return True
        result = subprocess.run(
            ["colima", "list", "--json"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
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
    except (FileNotFoundError, subprocess.TimeoutExpired):
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
                raise subprocess.CalledProcessError(result.returncode, args, output=result.stdout)
        else:
            raise subprocess.CalledProcessError(result.returncode, args, output=result.stdout)


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
            timeout=10,
            stdin=subprocess.DEVNULL,
        )
        current = result.stdout.strip()
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        current = ""

    if current == "colima":
        return

    # Add the colima remote if it doesn't exist
    result = subprocess.run(
        ["incus", "remote", "list", "--format=json"],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
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
                timeout=10,
                stdin=subprocess.DEVNULL,
            )

    subprocess.run(
        ["incus", "remote", "switch", "colima"],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
        stdin=subprocess.DEVNULL,
    )


def _check_colima_dns() -> bool:
    """Check if DNS resolution works inside the Colima VM."""
    try:
        result = subprocess.run(
            ["colima", "ssh", "--", "cat", "/etc/resolv.conf"],
            capture_output=True,
            text=True,
            timeout=10,
            stdin=subprocess.DEVNULL,
        )
        if result.returncode != 0:
            return False
        # Check that the file has actual content with a nameserver
        return "nameserver" in result.stdout
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _remove_stale_ssh_socket():
    """Remove a stale SSH control socket that can cause colima commands to hang."""
    sock = Path.home() / ".colima" / "_lima" / "colima" / "ssh.sock"
    if sock.exists():
        try:
            sock.unlink()
        except OSError:
            pass


def ensure_colima(cpu: int, memory: int, disk: int = 60, vm_type: str = "vz"):
    """Ensure Colima is running with correct settings. Restart if needed."""
    if not is_colima_running():
        _remove_stale_ssh_socket()
        print("Starting Colima VM (one-time setup)...", file=sys.stderr)
        start_colima(cpu, memory, disk, vm_type)
    elif not _check_colima_dns():
        print("Colima VM DNS is broken, restarting...", file=sys.stderr)
        try:
            subprocess.run(
                ["colima", "stop"],
                capture_output=True,
                check=False,
                timeout=30,
                stdin=subprocess.DEVNULL,
            )
        except subprocess.TimeoutExpired:
            print("Colima stop timed out, forcing...", file=sys.stderr)
            try:
                subprocess.run(
                    ["colima", "stop", "--force"],
                    capture_output=True,
                    check=False,
                    timeout=15,
                    stdin=subprocess.DEVNULL,
                )
            except subprocess.TimeoutExpired:
                print("Colima force-stop also timed out, proceeding anyway...", file=sys.stderr)
        _remove_stale_ssh_socket()
        start_colima(cpu, memory, disk, vm_type)

    _ensure_incus_remote()


def colima_host_ip() -> str:
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


def colima_bind_ip() -> str:
    """Get the macOS-side IP to bind daemons that need VM reachability.

    With ``--vm-type=vz`` (vzNAT), macOS creates a ``bridge*`` interface
    backed by ``vmenet*`` that connects the VM.  Binding to that bridge's
    IPv4 address is tighter than ``0.0.0.0`` — only the VM and the host
    can reach it, not the wider LAN.

    Discovery: find any ``bridge*`` interface whose member is ``vmenet*``
    and return its IPv4 address.  Falls back to ``0.0.0.0`` if no VMNet
    bridge is found (e.g. qemu backend or unusual network config).
    """
    import re as _re

    try:
        result = subprocess.run(
            ["ifconfig"],
            capture_output=True,
            text=True,
            timeout=5,
            stdin=subprocess.DEVNULL,
        )
        if result.returncode != 0:
            return "0.0.0.0"
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return "0.0.0.0"

    # Split output into per-interface blocks
    blocks = _re.split(r"(?=^\S+:)", result.stdout, flags=_re.MULTILINE)
    for block in blocks:
        if not block.startswith("bridge"):
            continue
        if "vmenet" not in block:
            continue
        # Found the VMNet bridge — extract its IPv4 address
        m = _re.search(r"inet (\d+\.\d+\.\d+\.\d+)", block)
        if m:
            return m.group(1)

    return "0.0.0.0"
