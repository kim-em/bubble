"""Shared test fixtures for bubble."""

import subprocess

import pytest

from bubble.runtime.base import ContainerInfo, ContainerRuntime


class MockRuntime(ContainerRuntime):
    """Mock container runtime that records calls and returns configurable responses."""

    def __init__(self):
        self.calls = []
        self.exec_responses: dict[str, str] = {}
        self._containers: dict[str, ContainerInfo] = {}
        self._images: set[str] = {"base"}

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

    def image_delete_all(self):
        self.calls.append(("image_delete_all",))
        self._images.clear()

    def list_images(self) -> list[dict]:
        self.calls.append(("list_images",))
        return []

    def push_file(self, name: str, local_path: str, remote_path: str):
        self.calls.append(("push_file", name, local_path, remote_path))


@pytest.fixture
def mock_runtime():
    """Provide a fresh MockRuntime."""
    return MockRuntime()


@pytest.fixture
def tmp_data_dir(tmp_path, monkeypatch):
    """Redirect all bubble data dirs to a temp directory."""
    import bubble.config as config
    import bubble.git_store as git_store
    import bubble.lifecycle as lifecycle

    data_dir = tmp_path / "bubble"
    data_dir.mkdir()
    git_dir = data_dir / "git"
    git_dir.mkdir()
    registry_file = data_dir / "registry.json"
    config_file = data_dir / "config.toml"
    repos_file = data_dir / "repos.json"

    cloud_state_file = data_dir / "cloud.json"
    cloud_key_file = data_dir / "cloud_key"
    cloud_known_hosts = data_dir / "known_hosts"

    # Patch the canonical config module
    monkeypatch.setattr(config, "DATA_DIR", data_dir)
    monkeypatch.setattr(config, "CONFIG_FILE", config_file)
    monkeypatch.setattr(config, "REGISTRY_FILE", registry_file)
    monkeypatch.setattr(config, "GIT_DIR", git_dir)
    monkeypatch.setattr(config, "REPOS_FILE", repos_file)
    monkeypatch.setattr(config, "CLOUD_STATE_FILE", cloud_state_file)
    monkeypatch.setattr(config, "CLOUD_KEY_FILE", cloud_key_file)
    monkeypatch.setattr(config, "CLOUD_KNOWN_HOSTS", cloud_known_hosts)

    # Also patch modules that do `from .config import X` (separate bindings)
    monkeypatch.setattr(lifecycle, "REGISTRY_FILE", registry_file)
    monkeypatch.setattr(git_store, "GIT_DIR", git_dir)

    # Patch cloud module if imported
    try:
        import bubble.cloud as cloud_mod
        monkeypatch.setattr(cloud_mod, "CLOUD_STATE_FILE", cloud_state_file)
        monkeypatch.setattr(cloud_mod, "CLOUD_KEY_FILE", cloud_key_file)
        monkeypatch.setattr(cloud_mod, "CLOUD_KNOWN_HOSTS", cloud_known_hosts)
        monkeypatch.setattr(cloud_mod, "DATA_DIR", data_dir)
    except ImportError:
        pass

    return data_dir


@pytest.fixture
def tmp_ssh_dir(tmp_path, monkeypatch):
    """Redirect SSH config paths to a temp directory."""
    import bubble.vscode as vscode

    ssh_dir = tmp_path / ".ssh" / "config.d"
    ssh_dir.mkdir(parents=True)
    ssh_file = ssh_dir / "bubble"

    monkeypatch.setattr(vscode, "SSH_CONFIG_DIR", ssh_dir)
    monkeypatch.setattr(vscode, "SSH_CONFIG_FILE", ssh_file)
    monkeypatch.setattr(vscode, "SSH_MAIN_CONFIG", tmp_path / ".ssh" / "config")

    return ssh_dir


@pytest.fixture
def relay_env(tmp_path, monkeypatch):
    """Set BUBBLE_HOME to tmp_path and reload relay-related modules."""
    import importlib

    import bubble.config
    import bubble.git_store
    import bubble.relay

    monkeypatch.setenv("BUBBLE_HOME", str(tmp_path))
    importlib.reload(bubble.config)
    importlib.reload(bubble.git_store)
    importlib.reload(bubble.relay)
    return tmp_path


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


def _base_image_exists() -> bool:
    """Check if the base image exists."""
    try:
        result = subprocess.run(
            ["incus", "image", "show", "base"],
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
requires_base_image = pytest.mark.skipif(
    not _incus_is_available() or not _base_image_exists(),
    reason="Incus not available or base image not built",
)
