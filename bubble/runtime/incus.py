"""Incus container runtime implementation."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from datetime import datetime, timezone

from .base import ContainerInfo, ContainerRuntime


class IncusError(subprocess.CalledProcessError, RuntimeError):
    """Error from an incus command.

    Inherits from both CalledProcessError (for returncode/cmd/stderr fields)
    and RuntimeError (so ``except RuntimeError`` catches it, matching the
    ContainerRuntime exception contract).
    """

    def __str__(self):
        detail = (self.stderr or self.stdout or "").strip()
        base = f"incus {' '.join(self.cmd[1:])} failed (exit {self.returncode})"
        if detail:
            return f"{base}: {detail}"
        return base


class IncusRuntime(ContainerRuntime):
    """Container runtime using Incus.

    The optional ``remote`` constructor argument names a non-default
    Incus remote that all resource references will be prefixed with
    (e.g. ``"bubble-colima"`` on macOS).  When empty, container/image
    names are passed through unchanged and the user's current default
    remote applies — bubble does not switch it.
    """

    def __init__(self, remote: str = ""):
        self._remote = remote

    def qualify(self, name: str) -> str:
        """Prefix *name* with our remote if one is configured.

        Names that already contain ``:`` are assumed to be explicitly
        qualified by the caller and pass through unchanged.
        """
        if self._remote and ":" not in name:
            return f"{self._remote}:{name}"
        return name

    def _q(self, name: str) -> str:
        # Internal alias matching the public method, kept short so call sites
        # stay readable.
        return self.qualify(name)

    def _run(self, args: list[str], check: bool = True, capture: bool = True) -> str:
        """Run an incus command."""
        cmd = ["incus"] + args
        try:
            result = subprocess.run(
                cmd,
                capture_output=capture,
                text=True,
                check=check,
                stdin=subprocess.DEVNULL,
            )
        except FileNotFoundError:
            from ..setup import _ensure_dependencies

            _ensure_dependencies()
            # _ensure_dependencies exits if incus not found; if we get here, retry
            result = subprocess.run(
                cmd,
                capture_output=capture,
                text=True,
                check=check,
                stdin=subprocess.DEVNULL,
            )
        except subprocess.CalledProcessError as e:
            raise IncusError(e.returncode, e.cmd, e.output, e.stderr) from None
        return result.stdout.strip() if capture else ""

    def _run_json(self, args: list[str]) -> dict | list:
        """Run an incus command and parse JSON output."""
        output = self._run(args + ["--format=json"])
        if not output:
            return {}
        try:
            return json.loads(output)
        except json.JSONDecodeError as e:
            raise RuntimeError(f"Invalid JSON from incus {' '.join(args)}: {e}") from None

    def is_available(self) -> bool:
        try:
            self._run(["version"])
            return True
        except (subprocess.CalledProcessError, FileNotFoundError):
            return False

    def launch(self, name: str, image: str, **kwargs) -> ContainerInfo:
        args = ["launch", self._q(image), self._q(name)]
        self._run(args)
        return self._get_info(name)

    @staticmethod
    def _parse_container(c: dict) -> ContainerInfo:
        """Parse Incus container JSON into ContainerInfo."""
        ipv4 = None
        state = c.get("state") or {}
        network = state.get("network") or {}
        eth0 = network.get("eth0") or {}
        for addr in eth0.get("addresses", []):
            if addr["family"] == "inet":
                ipv4 = addr["address"]
                break
        disk_usage = None
        disk = state.get("disk") or {}
        root = disk.get("root") or {}
        if root.get("usage"):
            disk_usage = root["usage"]

        def _parse_ts(key: str) -> datetime | None:
            raw = c.get(key)
            if not raw or raw.startswith("0001-"):
                return None
            # Incus uses RFC 3339 with nanoseconds; truncate to microseconds
            raw = raw.rstrip("Z")
            if "." in raw:
                base, frac = raw.split(".", 1)
                raw = f"{base}.{frac[:6]}"
            return datetime.fromisoformat(raw).replace(tzinfo=timezone.utc)

        state_map = {"Running": "running", "Stopped": "stopped", "Frozen": "frozen"}
        return ContainerInfo(
            name=c["name"],
            state=state_map.get(c["status"], c["status"].lower()),
            ipv4=ipv4,
            disk_usage=disk_usage,
            created_at=_parse_ts("created_at"),
            last_used_at=_parse_ts("last_used_at"),
        )

    def _get_info(self, name: str) -> ContainerInfo:
        """Get info for a single container."""
        data = self._run_json(["list", self._q(name)])
        if not data:
            raise RuntimeError(f"Container '{name}' not found")
        return self._parse_container(data[0])

    def list_containers(self, fast: bool = True) -> list[ContainerInfo]:
        # When a remote is set, pass "remote:" with no name so list scopes
        # to that remote rather than the user's default.
        args = ["list", self._q("")] if self._remote else ["list"]
        if fast:
            args.append("--fast")
        data = self._run_json(args)
        if not isinstance(data, list):
            return []
        return [self._parse_container(c) for c in data]

    def start(self, name: str):
        self._run(["start", self._q(name)])

    def stop(self, name: str):
        self._run(["stop", self._q(name)])

    def freeze(self, name: str):
        self._run(["pause", self._q(name)])

    def unfreeze(self, name: str):
        self._run(["start", self._q(name)])  # unpauses a frozen container

    def delete(self, name: str, force: bool = False):
        args = ["delete", self._q(name)]
        if force:
            args.append("--force")
        self._run(args)

    def exec(self, name: str, command: list[str], *, input: str | None = None, **kwargs) -> str:
        args = ["exec", self._q(name), "--"]
        args.extend(command)
        cmd = ["incus"] + args
        # When *input* is provided we pipe it through stdin so secrets stay
        # out of the container's argv (process list).  Otherwise we close
        # stdin so subprocesses don't inherit our terminal.
        run_kwargs: dict = {"capture_output": True, "text": True}
        if input is None:
            run_kwargs["stdin"] = subprocess.DEVNULL
        else:
            run_kwargs["input"] = input
        result = subprocess.run(cmd, **run_kwargs)
        if result.returncode != 0:
            raise IncusError(result.returncode, cmd, result.stdout, result.stderr)
        return result.stdout.strip()

    def exec_streaming(
        self,
        name: str,
        command: list[str],
        *,
        on_line: Callable[[str], None] | None = None,
    ) -> str:
        """Execute a command with true line-by-line streaming.

        When *on_line* is provided, stdout is streamed line by line and
        each line is passed to the callback as it arrives.  When *on_line*
        is ``None``, falls back to the normal captured :meth:`exec`.
        """
        if on_line is None:
            return self.exec(name, command)
        args = ["exec", self._q(name), "--"] + command
        cmd = ["incus"] + args
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            stdin=subprocess.DEVNULL,
        )
        lines: list[str] = []
        stderr_output = ""
        try:
            assert proc.stdout is not None
            for raw in proc.stdout:
                line = raw.rstrip("\n")
                lines.append(line)
                on_line(line)
        finally:
            if proc.stdout:
                proc.stdout.close()
            if proc.stderr:
                stderr_output = proc.stderr.read()
                proc.stderr.close()
            proc.wait()
        output = "\n".join(lines)
        if proc.returncode != 0:
            raise IncusError(proc.returncode, cmd, output, stderr_output)
        return output

    def add_device(self, name: str, device_name: str, device_type: str, **props):
        args = ["config", "device", "add", self._q(name), device_name, device_type]
        for k, v in props.items():
            args.append(f"{k}={v}")
        self._run(args)

    def override_device(self, name: str, device_name: str, **props):
        """Copy a profile-inherited device onto the instance and set props.

        Used to apply ``security.mac_filtering=true`` (etc.) to the
        default ``eth0`` NIC without modifying the underlying profile.
        Idempotent: if the device is already overridden, falls back to
        ``set`` for each property.
        """
        args = [
            "incus",
            "config",
            "device",
            "override",
            self._q(name),
            device_name,
        ]
        for k, v in props.items():
            args.append(f"{k}={v}")
        result = subprocess.run(args, capture_output=True, text=True, check=False)
        if result.returncode == 0:
            return
        stderr = (result.stderr or "").strip()
        # "already exists" => fall back to per-key set
        if "already exists" in stderr.lower() or "is already" in stderr.lower():
            for k, v in props.items():
                set_cmd = [
                    "incus",
                    "config",
                    "device",
                    "set",
                    self._q(name),
                    device_name,
                    f"{k}={v}",
                ]
                set_result = subprocess.run(set_cmd, capture_output=True, text=True, check=False)
                if set_result.returncode != 0:
                    raise IncusError(
                        set_result.returncode,
                        set_cmd,
                        set_result.stdout,
                        set_result.stderr,
                    )
            return
        raise IncusError(result.returncode, args, result.stdout, stderr)

    def remove_device(self, name: str, device_name: str):
        """Remove a device. Tolerates 'device doesn't exist' (idempotent)."""
        cmd = ["incus", "config", "device", "remove", self._q(name), device_name]
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if result.returncode == 0:
            return
        stderr = (result.stderr or "").strip()
        # incus phrases the missing-device error a couple of ways; tolerate both.
        if "doesn't exist" in stderr.lower() or "not found" in stderr.lower():
            return
        raise IncusError(result.returncode, cmd, result.stdout, stderr)

    def device_exists(self, name: str, device_name: str) -> bool:
        cmd = ["incus", "config", "device", "show", self._q(name), device_name]
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
        return result.returncode == 0

    def container_ipv4(self, name: str) -> str | None:
        try:
            data = self._run_json(["list", self._q(name)])
        except (subprocess.CalledProcessError, RuntimeError):
            return None
        if not data:
            return None
        return self._parse_container(data[0]).ipv4

    def add_disk(self, name: str, device_name: str, source: str, path: str, readonly: bool = False):
        props = {"source": source, "path": path}
        if readonly:
            props["readonly"] = "true"
        self.add_device(name, device_name, "disk", **props)

    def publish(self, name: str, alias: str):
        # Stop first if running
        try:
            info = self._get_info(name)
            if info.state == "running":
                self.stop(name)
        except RuntimeError:
            pass
        # Delete existing image with same alias
        if self.image_exists(alias):
            self.image_delete(alias)
        self._run(["publish", self._q(name), "--alias", alias])

    def image_exists(self, alias: str) -> bool:
        try:
            self._run(["image", "show", self._q(alias)])
            return True
        except subprocess.CalledProcessError:
            return False

    def image_delete(self, alias_or_fingerprint: str):
        self._run(["image", "delete", self._q(alias_or_fingerprint)])

    def image_delete_all(self):
        images = self.list_images()
        for img in images:
            fingerprint = img.get("fingerprint", "")
            if fingerprint:
                self._run(["image", "delete", self._q(fingerprint)])

    def list_images(self) -> list[dict]:
        args = ["image", "list", self._q("")] if self._remote else ["image", "list"]
        data = self._run_json(args)
        if not isinstance(data, list):
            return []
        return data

    def push_file(self, name: str, local_path: str, remote_path: str):
        self._run(["file", "push", local_path, f"{self._q(name)}{remote_path}"])

    # --- Operation introspection (used by `bubble doctor`) -------------

    def list_operations(self) -> list[dict]:
        """List currently running incus operations on our remote."""
        args = ["operation", "list", self._q("")] if self._remote else ["operation", "list"]
        data = self._run_json(args)
        if not isinstance(data, list):
            return []
        return data

    def delete_operation(self, op_id: str):
        """Cancel a running operation by id."""
        self._run(["operation", "delete", self._q(op_id)])

    # --- Network introspection (used by image build IPv4/DNS fixups) --

    def network_get(self, network: str, key: str) -> str:
        """Get a single config value from an incus-managed network."""
        return self._run(["network", "get", self._q(network), key])
