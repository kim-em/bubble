"""Abstract container runtime interface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime


@dataclass
class ContainerInfo:
    name: str
    state: str  # "running", "stopped", "frozen"
    ipv4: str | None = None
    image: str | None = None
    disk_usage: int | None = None  # bytes
    created_at: datetime | None = None
    last_used_at: datetime | None = None


class ContainerRuntime(ABC):
    """Abstract interface for container operations.

    Exception contract: methods that interact with the container backend
    raise ``RuntimeError`` (or a subclass such as ``IncusError``) on
    failure.  Callers should catch ``RuntimeError`` to handle all
    backend errors uniformly.
    """

    def qualify(self, name: str) -> str:
        """Qualify a resource name with the runtime's remote prefix, if any.

        For the default Incus backend on Linux this is the identity function.
        On macOS-via-Colima the runtime targets a non-default Incus remote
        and this returns ``"<remote>:<name>"`` so callers don't have to depend
        on the user's default remote being set to ours.
        """
        return name

    @abstractmethod
    def is_available(self) -> bool:
        """Check if the runtime is available."""

    @abstractmethod
    def launch(self, name: str, image: str, **kwargs) -> ContainerInfo:
        """Launch a new container from an image."""

    @abstractmethod
    def list_containers(self, fast: bool = True) -> list[ContainerInfo]:
        """List all containers. With fast=True, skips expensive state queries (disk, network)."""

    @abstractmethod
    def start(self, name: str):
        """Start a stopped container."""

    @abstractmethod
    def stop(self, name: str):
        """Stop a running container."""

    @abstractmethod
    def freeze(self, name: str):
        """Freeze (pause) a running container."""

    @abstractmethod
    def unfreeze(self, name: str):
        """Unfreeze (resume) a frozen container."""

    @abstractmethod
    def delete(self, name: str, force: bool = False):
        """Delete a container."""

    @abstractmethod
    def exec(self, name: str, command: list[str], *, input: str | None = None, **kwargs) -> str:
        """Execute a command inside a container, return stdout.

        Raises ``RuntimeError`` (or a subclass) if the command fails.

        ``input`` (when non-None) is piped to the command's stdin.  This is
        the right channel for sensitive data — anything passed in argv shows
        up in the container's process list.
        """

    @abstractmethod
    def add_device(self, name: str, device_name: str, device_type: str, **props):
        """Add a device (disk, proxy, etc.) to a container."""

    def override_device(self, name: str, device_name: str, **props):
        """Override a profile-inherited device's properties on the instance.

        Default implementation raises NotImplementedError; runtimes that
        support per-instance device overrides (e.g. IncusRuntime) should
        implement it. Used to apply per-NIC security filtering to the
        default ``eth0`` without modifying the profile.
        """
        raise NotImplementedError("override_device not supported by this runtime")

    @abstractmethod
    def remove_device(self, name: str, device_name: str):
        """Remove a device from a container.

        Idempotent: succeeds (returns normally) if the device does not
        exist. Used by bubble's stop/pop paths to ensure incus reaps
        the device's helper processes before the container goes away.
        """

    @abstractmethod
    def device_exists(self, name: str, device_name: str) -> bool:
        """Check whether a named device is attached to a container."""

    def device_property(self, name: str, device_name: str, key: str) -> str | None:
        """Return a device property value, or None if unset/unavailable.

        Default implementation returns None; runtimes that can introspect
        device config (e.g. IncusRuntime) override it.
        """
        return None

    def container_ipv4(self, name: str) -> str | None:
        """Return the container's primary IPv4 address, or None.

        Default implementation reads from :meth:`list_containers`. Subclasses
        may override for a more efficient single-container lookup.
        """
        for info in self.list_containers(fast=False):
            if info.name == name:
                return info.ipv4
        return None

    @abstractmethod
    def add_disk(self, name: str, device_name: str, source: str, path: str, readonly: bool = False):
        """Mount a host path into the container."""

    @abstractmethod
    def publish(self, name: str, alias: str):
        """Publish a container as a reusable image."""

    @abstractmethod
    def image_exists(self, alias: str) -> bool:
        """Check if an image with the given alias exists."""

    @abstractmethod
    def image_delete(self, alias_or_fingerprint: str):
        """Delete an image by alias or fingerprint."""

    @abstractmethod
    def image_delete_all(self):
        """Delete all images."""

    @abstractmethod
    def list_images(self) -> list[dict]:
        """List all images. Returns list of dicts with aliases, size, created_at."""

    def exec_streaming(
        self,
        name: str,
        command: list[str],
        *,
        on_line: Callable[[str], None] | None = None,
    ) -> str:
        """Execute a command, calling *on_line* for each stdout line.

        The default implementation delegates to :meth:`exec` and replays
        lines after completion.  Subclasses may override for true
        line-by-line streaming.
        """
        output = self.exec(name, command)
        if on_line:
            for line in output.splitlines():
                on_line(line)
        return output

    @abstractmethod
    def push_file(self, name: str, local_path: str, remote_path: str):
        """Push a local file into a container."""
