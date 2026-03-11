"""Tests for user-specified mount support."""

import pytest

from bubble.config import MountSpec, parse_mounts


class TestMountSpecFromCli:
    """Test parsing --mount flag values."""

    def test_basic_readonly(self):
        m = MountSpec.from_cli("/host/path:/container/path:ro")
        assert m.source == "/host/path"
        assert m.target == "/container/path"
        assert m.readonly is True

    def test_basic_readwrite(self):
        m = MountSpec.from_cli("/host/path:/container/path:rw")
        assert m.source == "/host/path"
        assert m.target == "/container/path"
        assert m.readonly is False

    def test_default_readonly(self):
        m = MountSpec.from_cli("/host/path:/container/path")
        assert m.readonly is True

    def test_tilde_expansion(self):
        m = MountSpec.from_cli("~/.config/git:/home/user/.config/git")
        assert "~" not in m.source
        assert m.target == "/home/user/.config/git"

    def test_nested_paths(self):
        m = MountSpec.from_cli("/home/user/data/project:/workspace/data:rw")
        assert m.source == "/home/user/data/project"
        assert m.target == "/workspace/data"
        assert m.readonly is False

    def test_missing_container_path_raises(self):
        with pytest.raises(ValueError, match="expected"):
            MountSpec.from_cli("/only/one/path")

    def test_relative_container_path_raises(self):
        with pytest.raises(ValueError, match="absolute"):
            MountSpec.from_cli("/host:relative/path")

    def test_invalid_mode_suffix_raises(self):
        with pytest.raises(ValueError, match="mode"):
            MountSpec.from_cli("/host:/container:bogus")


class TestMountSpecFromConfig:
    """Test parsing [[mounts]] config entries."""

    def test_basic(self):
        m = MountSpec.from_config({
            "source": "~/.config/git",
            "target": "/home/user/.config/git",
            "mode": "ro",
        })
        assert "~" not in m.source
        assert m.target == "/home/user/.config/git"
        assert m.readonly is True

    def test_readwrite(self):
        m = MountSpec.from_config({
            "source": "/data",
            "target": "/mnt/data",
            "mode": "rw",
        })
        assert m.readonly is False

    def test_default_mode_readonly(self):
        m = MountSpec.from_config({
            "source": "/data",
            "target": "/mnt/data",
        })
        assert m.readonly is True

    def test_exclude_list(self):
        m = MountSpec.from_config({
            "source": "/data",
            "target": "/mnt/data",
            "exclude": [".cache", "tmp"],
        })
        assert m.exclude == [".cache", "tmp"]

    def test_exclude_string_converted_to_list(self):
        m = MountSpec.from_config({
            "source": "/data",
            "target": "/mnt/data",
            "exclude": ".cache",
        })
        assert m.exclude == [".cache"]

    def test_missing_source_raises(self):
        with pytest.raises(ValueError, match="source"):
            MountSpec.from_config({"target": "/mnt/data"})

    def test_missing_target_raises(self):
        with pytest.raises(ValueError, match="target"):
            MountSpec.from_config({"source": "/data"})

    def test_invalid_mode_raises(self):
        with pytest.raises(ValueError, match="mode"):
            MountSpec.from_config({
                "source": "/data",
                "target": "/mnt/data",
                "mode": "wx",
            })

    def test_exclude_absolute_path_raises(self):
        with pytest.raises(ValueError, match="relative"):
            MountSpec.from_config({
                "source": "/data",
                "target": "/mnt/data",
                "exclude": ["/etc"],
            })

    def test_exclude_parent_traversal_raises(self):
        with pytest.raises(ValueError, match="\\.\\."):
            MountSpec.from_config({
                "source": "/data",
                "target": "/mnt/data",
                "exclude": ["../etc"],
            })

    def test_exclude_empty_raises(self):
        with pytest.raises(ValueError, match="Empty"):
            MountSpec.from_config({
                "source": "/data",
                "target": "/mnt/data",
                "exclude": [""],
            })


class TestParseMounts:
    """Test merging config and CLI mounts."""

    def test_empty(self):
        assert parse_mounts({}) == []

    def test_config_only(self):
        config = {
            "mounts": [
                {"source": "/a", "target": "/b", "mode": "ro"},
            ],
        }
        result = parse_mounts(config)
        assert len(result) == 1
        assert result[0].source == "/a"

    def test_cli_only(self):
        result = parse_mounts({}, cli_mounts=("/host:/container:rw",))
        assert len(result) == 1
        assert result[0].source == "/host"
        assert result[0].readonly is False

    def test_merged(self):
        config = {
            "mounts": [
                {"source": "/config-src", "target": "/config-tgt"},
            ],
        }
        result = parse_mounts(config, cli_mounts=("/cli-src:/cli-tgt:rw",))
        assert len(result) == 2
        assert result[0].source == "/config-src"
        assert result[1].source == "/cli-src"

    def test_multiple_cli(self):
        result = parse_mounts({}, cli_mounts=(
            "/a:/x:ro",
            "/b:/y:rw",
        ))
        assert len(result) == 2


class TestMountProvisioning:
    """Test that mounts are correctly applied during container provisioning."""

    def test_user_mounts_applied(self, mock_runtime, tmp_path):
        """Verify add_disk is called for each user mount."""
        from bubble.cli import _provision_container

        # Create a fake ref path and source dirs
        ref_path = tmp_path / "repo.git"
        ref_path.mkdir()
        src1 = tmp_path / "src1"
        src1.mkdir()
        src2 = tmp_path / "src2"
        src2.mkdir()

        mounts = [
            MountSpec(source=str(src1), target="/mnt/src1", readonly=True),
            MountSpec(source=str(src2), target="/mnt/src2", readonly=False),
        ]

        _provision_container(
            mock_runtime, "test-container", "base",
            ref_path, "repo.git", {},
            user_mounts=mounts,
        )

        # Find add_disk calls for user mounts
        disk_calls = [c for c in mock_runtime.calls if c[0] == "add_disk"]
        # Should have: shared-git + 2 user mounts = 3
        assert len(disk_calls) == 3

        # Check user mount calls
        user_disk_calls = [c for c in disk_calls if "user-mount" in c[2]]
        assert len(user_disk_calls) == 2
        assert user_disk_calls[0] == ("add_disk", "test-container", "user-mount-0", str(src1), "/mnt/src1", True)
        assert user_disk_calls[1] == ("add_disk", "test-container", "user-mount-1", str(src2), "/mnt/src2", False)

    def test_rw_mount_does_not_mutate_host_permissions(self, mock_runtime, tmp_path):
        """Verify rw mounts do NOT chmod the host source directory."""
        from bubble.cli import _provision_container

        ref_path = tmp_path / "repo.git"
        ref_path.mkdir()
        rw_dir = tmp_path / "rw_data"
        rw_dir.mkdir(mode=0o755)

        mounts = [
            MountSpec(source=str(rw_dir), target="/mnt/data", readonly=False),
        ]

        _provision_container(
            mock_runtime, "test-container", "base",
            ref_path, "repo.git", {},
            user_mounts=mounts,
        )

        # Host permissions must not be changed
        assert rw_dir.stat().st_mode & 0o777 == 0o755

    def test_exclusions_overmount_tmpfs(self, mock_runtime, tmp_path):
        """Verify exclusions create exec calls to mount tmpfs (no add_device)."""
        from bubble.cli import _provision_container

        ref_path = tmp_path / "repo.git"
        ref_path.mkdir()
        src = tmp_path / "src"
        src.mkdir()

        mounts = [
            MountSpec(
                source=str(src), target="/mnt/data", readonly=True,
                exclude=[".cache", "tmp"],
            ),
        ]

        _provision_container(
            mock_runtime, "test-container", "base",
            ref_path, "repo.git", {},
            user_mounts=mounts,
        )

        # Check for exec calls that mount tmpfs
        exec_calls = [c for c in mock_runtime.calls if c[0] == "exec"]
        tmpfs_execs = [c for c in exec_calls if "tmpfs" in " ".join(c[2])]
        assert len(tmpfs_execs) == 2

        # No add_device calls for exclusions (just tmpfs via exec)
        device_calls = [c for c in mock_runtime.calls if c[0] == "add_device"]
        excl_devices = [c for c in device_calls if "user-excl" in str(c)]
        assert len(excl_devices) == 0

    def test_no_user_mounts(self, mock_runtime, tmp_path):
        """No user mount calls when user_mounts is empty."""
        from bubble.cli import _provision_container

        ref_path = tmp_path / "repo.git"
        ref_path.mkdir()

        _provision_container(
            mock_runtime, "test-container", "base",
            ref_path, "repo.git", {},
            user_mounts=[],
        )

        disk_calls = [c for c in mock_runtime.calls if c[0] == "add_disk"]
        user_disk_calls = [c for c in disk_calls if "user-mount" in c[2]]
        assert len(user_disk_calls) == 0
