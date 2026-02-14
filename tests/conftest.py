"""Shared test fixtures for lean-bubbles."""

import subprocess

import pytest

from lean_bubbles.runtime.base import ContainerInfo, ContainerRuntime


class MockRuntime(ContainerRuntime):
    """Mock container runtime that records calls and returns configurable responses."""

    def __init__(self):
        self.calls = []
        self.exec_responses: dict[str, str] = {}
        self._containers: dict[str, ContainerInfo] = {}
        self._images: set[str] = {"lean-base"}

    def is_available(self) -> bool:
        self.calls.append(("is_available",))
        return True

    def launch(self, name: str, image: str, **kwargs) -> ContainerInfo:
        self.calls.append(("launch", name, image))
        info = ContainerInfo(name=name, state="running", image=image)
        self._containers[name] = info
        return info

    def list_containers(self) -> list[ContainerInfo]:
        self.calls.append(("list_containers",))
        return list(self._containers.values())

    def start(self, name: str):
        self.calls.append(("start", name))

    def stop(self, name: str):
        self.calls.append(("stop", name))

    def freeze(self, name: str):
        self.calls.append(("freeze", name))

    def unfreeze(self, name: str):
        self.calls.append(("unfreeze", name))

    def delete(self, name: str, force: bool = False):
        self.calls.append(("delete", name, force))
        self._containers.pop(name, None)

    def exec(self, name: str, command: list[str], **kwargs) -> str:
        self.calls.append(("exec", name, command))
        key = " ".join(command)
        for pattern, response in self.exec_responses.items():
            if pattern in key:
                return response
        return ""

    def add_device(self, name: str, device_name: str, device_type: str, **props):
        self.calls.append(("add_device", name, device_name, device_type, props))

    def add_disk(self, name: str, device_name: str, source: str, path: str, readonly: bool = False):
        self.calls.append(("add_disk", name, device_name, source, path, readonly))

    def publish(self, name: str, alias: str):
        self.calls.append(("publish", name, alias))
        self._images.add(alias)

    def image_exists(self, alias: str) -> bool:
        self.calls.append(("image_exists", alias))
        return alias in self._images

    def image_delete(self, alias: str):
        self.calls.append(("image_delete", alias))
        self._images.discard(alias)


@pytest.fixture
def mock_runtime():
    """Provide a fresh MockRuntime."""
    return MockRuntime()


@pytest.fixture
def tmp_data_dir(tmp_path, monkeypatch):
    """Redirect all lean-bubbles data dirs to a temp directory."""
    import lean_bubbles.config as config
    import lean_bubbles.git_store as git_store
    import lean_bubbles.lake_cache as lake_cache
    import lean_bubbles.lifecycle as lifecycle

    data_dir = tmp_path / "lean-bubbles"
    data_dir.mkdir()
    git_dir = data_dir / "git"
    git_dir.mkdir()
    lake_cache_dir = data_dir / "lake-cache"
    lake_cache_dir.mkdir()
    registry_file = data_dir / "registry.json"
    config_file = data_dir / "config.toml"

    # Patch the canonical config module
    monkeypatch.setattr(config, "DATA_DIR", data_dir)
    monkeypatch.setattr(config, "CONFIG_FILE", config_file)
    monkeypatch.setattr(config, "REGISTRY_FILE", registry_file)
    monkeypatch.setattr(config, "GIT_DIR", git_dir)
    monkeypatch.setattr(config, "LAKE_CACHE_DIR", lake_cache_dir)

    # Also patch modules that do `from .config import X` (separate bindings)
    monkeypatch.setattr(lifecycle, "REGISTRY_FILE", registry_file)
    monkeypatch.setattr(git_store, "GIT_DIR", git_dir)
    monkeypatch.setattr(lake_cache, "LAKE_CACHE_DIR", lake_cache_dir)

    return data_dir


@pytest.fixture
def tmp_ssh_dir(tmp_path, monkeypatch):
    """Redirect SSH config paths to a temp directory."""
    import lean_bubbles.vscode as vscode

    ssh_dir = tmp_path / ".ssh" / "config.d"
    ssh_dir.mkdir(parents=True)
    ssh_file = ssh_dir / "lean-bubbles"

    monkeypatch.setattr(vscode, "SSH_CONFIG_DIR", ssh_dir)
    monkeypatch.setattr(vscode, "SSH_CONFIG_FILE", ssh_file)
    monkeypatch.setattr(vscode, "SSH_MAIN_CONFIG", tmp_path / ".ssh" / "config")

    return ssh_dir


def _incus_is_available() -> bool:
    """Check if Incus is available on this system."""
    try:
        result = subprocess.run(
            ["incus", "version"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _lean_base_image_exists() -> bool:
    """Check if the lean-base image exists."""
    try:
        result = subprocess.run(
            ["incus", "image", "show", "lean-base"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


# Skip integration tests if Incus is not available
requires_incus = pytest.mark.skipif(
    not _incus_is_available(),
    reason="Incus not available",
)
requires_lean_base = pytest.mark.skipif(
    not _incus_is_available() or not _lean_base_image_exists(),
    reason="Incus not available or lean-base image not built",
)
