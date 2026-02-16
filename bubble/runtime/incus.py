"""Incus container runtime implementation."""

import json
import subprocess
from datetime import datetime, timezone

from .base import ContainerInfo, ContainerRuntime


class IncusRuntime(ContainerRuntime):
    """Container runtime using Incus."""

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
            from ..cli import _ensure_dependencies

            _ensure_dependencies()
        return result.stdout.strip() if capture else ""

    def _run_json(self, args: list[str]) -> dict | list:
        """Run an incus command and parse JSON output."""
        output = self._run(args + ["--format=json"])
        return json.loads(output) if output else {}

    def is_available(self) -> bool:
        try:
            self._run(["version"])
            return True
        except (subprocess.CalledProcessError, FileNotFoundError):
            return False

    def launch(self, name: str, image: str, **kwargs) -> ContainerInfo:
        args = ["launch", image, name]
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
        data = self._run_json(["list", name])
        if not data:
            raise RuntimeError(f"Container '{name}' not found")
        return self._parse_container(data[0])

    def list_containers(self, fast: bool = True) -> list[ContainerInfo]:
        args = ["list"]
        if fast:
            args.append("--fast")
        data = self._run_json(args)
        if not isinstance(data, list):
            return []
        return [self._parse_container(c) for c in data]

    def start(self, name: str):
        self._run(["start", name])

    def stop(self, name: str):
        self._run(["stop", name])

    def freeze(self, name: str):
        self._run(["pause", name])

    def unfreeze(self, name: str):
        self._run(["start", name])  # unpauses a frozen container

    def delete(self, name: str, force: bool = False):
        args = ["delete", name]
        if force:
            args.append("--force")
        self._run(args)

    def exec(self, name: str, command: list[str], **kwargs) -> str:
        args = ["exec", name, "--"]
        args.extend(command)
        cmd = ["incus"] + args
        result = subprocess.run(cmd, capture_output=True, text=True, stdin=subprocess.DEVNULL)
        if result.returncode != 0:
            error_detail = result.stderr.strip() or result.stdout.strip()
            raise RuntimeError(
                f"Command failed (exit {result.returncode}): {' '.join(command)}\n{error_detail}"
            )
        return result.stdout.strip()

    def add_device(self, name: str, device_name: str, device_type: str, **props):
        args = ["config", "device", "add", name, device_name, device_type]
        for k, v in props.items():
            args.append(f"{k}={v}")
        self._run(args)

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
        self._run(["publish", name, "--alias", alias])

    def image_exists(self, alias: str) -> bool:
        try:
            self._run(["image", "show", alias])
            return True
        except subprocess.CalledProcessError:
            return False

    def image_delete(self, alias: str):
        self._run(["image", "delete", alias])

    def list_images(self) -> list[dict]:
        data = self._run_json(["image", "list"])
        if not isinstance(data, list):
            return []
        return data

    def push_file(self, name: str, local_path: str, remote_path: str):
        self._run(["file", "push", local_path, f"{name}{remote_path}"])
