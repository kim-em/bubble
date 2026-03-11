"""Tests for user-specified mount support."""

from pathlib import Path

import pytest

from bubble.config import MountSpec, claude_config_mounts, parse_mounts


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
        m = MountSpec.from_config(
            {
                "source": "~/.config/git",
                "target": "/home/user/.config/git",
                "mode": "ro",
            }
        )
        assert "~" not in m.source
        assert m.target == "/home/user/.config/git"
        assert m.readonly is True

    def test_readwrite(self):
        m = MountSpec.from_config(
            {
                "source": "/data",
                "target": "/mnt/data",
                "mode": "rw",
            }
        )
        assert m.readonly is False

    def test_default_mode_readonly(self):
        m = MountSpec.from_config(
            {
                "source": "/data",
                "target": "/mnt/data",
            }
        )
        assert m.readonly is True

    def test_exclude_list(self):
        m = MountSpec.from_config(
            {
                "source": "/data",
                "target": "/mnt/data",
                "exclude": [".cache", "tmp"],
            }
        )
        assert m.exclude == [".cache", "tmp"]

    def test_exclude_string_converted_to_list(self):
        m = MountSpec.from_config(
            {
                "source": "/data",
                "target": "/mnt/data",
                "exclude": ".cache",
            }
        )
        assert m.exclude == [".cache"]

    def test_missing_source_raises(self):
        with pytest.raises(ValueError, match="source"):
            MountSpec.from_config({"target": "/mnt/data"})

    def test_missing_target_raises(self):
        with pytest.raises(ValueError, match="target"):
            MountSpec.from_config({"source": "/data"})

    def test_invalid_mode_raises(self):
        with pytest.raises(ValueError, match="mode"):
            MountSpec.from_config(
                {
                    "source": "/data",
                    "target": "/mnt/data",
                    "mode": "wx",
                }
            )

    def test_exclude_absolute_path_raises(self):
        with pytest.raises(ValueError, match="relative"):
            MountSpec.from_config(
                {
                    "source": "/data",
                    "target": "/mnt/data",
                    "exclude": ["/etc"],
                }
            )

    def test_exclude_parent_traversal_raises(self):
        with pytest.raises(ValueError, match="\\.\\."):
            MountSpec.from_config(
                {
                    "source": "/data",
                    "target": "/mnt/data",
                    "exclude": ["../etc"],
                }
            )

    def test_exclude_empty_raises(self):
        with pytest.raises(ValueError, match="Empty"):
            MountSpec.from_config(
                {
                    "source": "/data",
                    "target": "/mnt/data",
                    "exclude": [""],
                }
            )


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
        result = parse_mounts(
            {},
            cli_mounts=(
                "/a:/x:ro",
                "/b:/y:rw",
            ),
        )
        assert len(result) == 2

    def test_duplicate_target_raises(self):
        with pytest.raises(ValueError, match="Duplicate mount target"):
            parse_mounts(
                {},
                cli_mounts=(
                    "/a:/mnt/data:ro",
                    "/b:/mnt/data:rw",
                ),
            )

    def test_duplicate_target_config_and_cli_raises(self):
        config = {
            "mounts": [
                {"source": "/a", "target": "/mnt/data"},
            ],
        }
        with pytest.raises(ValueError, match="Duplicate mount target"):
            parse_mounts(config, cli_mounts=("/b:/mnt/data:rw",))


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
            mock_runtime,
            "test-container",
            "base",
            ref_path,
            "repo.git",
            {},
            user_mounts=mounts,
        )

        # Find add_disk calls for user mounts
        disk_calls = [c for c in mock_runtime.calls if c[0] == "add_disk"]
        # Should have: shared-git + 2 user mounts = 3
        assert len(disk_calls) == 3

        # Check user mount calls
        user_disk_calls = [c for c in disk_calls if "user-mount" in c[2]]
        assert len(user_disk_calls) == 2
        assert user_disk_calls[0] == (
            "add_disk",
            "test-container",
            "user-mount-0",
            str(src1),
            "/mnt/src1",
            True,
        )
        assert user_disk_calls[1] == (
            "add_disk",
            "test-container",
            "user-mount-1",
            str(src2),
            "/mnt/src2",
            False,
        )

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
            mock_runtime,
            "test-container",
            "base",
            ref_path,
            "repo.git",
            {},
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
                source=str(src),
                target="/mnt/data",
                readonly=True,
                exclude=[".cache", "tmp"],
            ),
        ]

        _provision_container(
            mock_runtime,
            "test-container",
            "base",
            ref_path,
            "repo.git",
            {},
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
            mock_runtime,
            "test-container",
            "base",
            ref_path,
            "repo.git",
            {},
            user_mounts=[],
        )

        disk_calls = [c for c in mock_runtime.calls if c[0] == "add_disk"]
        user_disk_calls = [c for c in disk_calls if "user-mount" in c[2]]
        assert len(user_disk_calls) == 0


class TestClaudeConfigMounts:
    """Test automatic ~/.claude config mounting."""

    def test_returns_existing_files(self, tmp_path, monkeypatch):
        """Mounts returned for all config items that exist."""
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        (claude_dir / "CLAUDE.md").write_text("# test")
        (claude_dir / "settings.json").write_text("{}")
        (claude_dir / "skills").mkdir()
        (claude_dir / "keybindings.json").write_text("{}")
        (claude_dir / ".credentials.json").write_text("{}")
        (claude_dir / ".current-account").write_text("acct")

        monkeypatch.setattr("bubble.config.CLAUDE_CONFIG_DIR", claude_dir)

        mounts = claude_config_mounts()

        assert len(mounts) == 6
        targets = {m.target for m in mounts}
        assert "/home/user/.claude/CLAUDE.md" in targets
        assert "/home/user/.claude/settings.json" in targets
        assert "/home/user/.claude/skills" in targets
        assert "/home/user/.claude/keybindings.json" in targets
        assert "/home/user/.claude/.credentials.json" in targets
        assert "/home/user/.claude/.current-account" in targets
        assert all(m.readonly for m in mounts)

    def test_skips_missing_files(self, tmp_path, monkeypatch):
        """Only existing files are mounted."""
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        (claude_dir / "CLAUDE.md").write_text("# test")

        monkeypatch.setattr("bubble.config.CLAUDE_CONFIG_DIR", claude_dir)

        mounts = claude_config_mounts()

        assert len(mounts) == 1
        assert mounts[0].target == "/home/user/.claude/CLAUDE.md"

    def test_no_claude_dir(self, tmp_path, monkeypatch):
        """Returns empty when ~/.claude doesn't exist."""
        monkeypatch.setattr("bubble.config.CLAUDE_CONFIG_DIR", tmp_path / "nonexistent")

        mounts = claude_config_mounts()

        assert mounts == []

    def test_includes_credentials(self, tmp_path, monkeypatch):
        """Credential files are mounted for authentication."""
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        (claude_dir / ".credentials.json").write_text("{}")
        (claude_dir / ".current-account").write_text("acct")

        monkeypatch.setattr("bubble.config.CLAUDE_CONFIG_DIR", claude_dir)

        mounts = claude_config_mounts()

        targets = {m.target for m in mounts}
        assert "/home/user/.claude/.credentials.json" in targets
        assert "/home/user/.claude/.current-account" in targets
        assert all(m.readonly for m in mounts)

    def test_excludes_transient_state(self, tmp_path, monkeypatch):
        """Session history and transient state are NOT mounted."""
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        (claude_dir / "projects").mkdir()
        (claude_dir / "stats-cache.json").write_text("{}")
        (claude_dir / "history.jsonl").write_text("")
        (claude_dir / "todos").mkdir()

        monkeypatch.setattr("bubble.config.CLAUDE_CONFIG_DIR", claude_dir)

        mounts = claude_config_mounts()

        assert mounts == []

    def test_sources_are_absolute(self, tmp_path, monkeypatch):
        """Mount sources use absolute paths."""
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        (claude_dir / "CLAUDE.md").write_text("# test")

        monkeypatch.setattr("bubble.config.CLAUDE_CONFIG_DIR", claude_dir)

        mounts = claude_config_mounts()

        assert Path(mounts[0].source).is_absolute()


class TestClaudeConfigProvisioning:
    """Test that claude config mounts are applied during container provisioning."""

    def test_claude_mounts_applied(self, mock_runtime, tmp_path, tmp_data_dir):
        """Verify add_disk calls with claude-config device names."""
        from bubble.cli import _provision_container

        ref_path = tmp_path / "repo.git"
        ref_path.mkdir()

        claude_mounts = [
            MountSpec(
                source="/home/testuser/.claude/CLAUDE.md",
                target="/home/user/.claude/CLAUDE.md",
                readonly=True,
            ),
            MountSpec(
                source="/home/testuser/.claude/skills",
                target="/home/user/.claude/skills",
                readonly=True,
            ),
        ]

        _provision_container(
            mock_runtime,
            "test-container",
            "base",
            ref_path,
            "repo.git",
            {},
            claude_mounts=claude_mounts,
        )

        disk_calls = [c for c in mock_runtime.calls if c[0] == "add_disk"]
        claude_disk_calls = [c for c in disk_calls if "claude-config" in c[2]]
        assert len(claude_disk_calls) == 2
        assert claude_disk_calls[0] == (
            "add_disk",
            "test-container",
            "claude-config-0",
            "/home/testuser/.claude/CLAUDE.md",
            "/home/user/.claude/CLAUDE.md",
            True,
        )
        assert claude_disk_calls[1] == (
            "add_disk",
            "test-container",
            "claude-config-1",
            "/home/testuser/.claude/skills",
            "/home/user/.claude/skills",
            True,
        )

    def test_creates_claude_dir_in_container(self, mock_runtime, tmp_path, tmp_data_dir):
        """Verify .claude directory is created before mounting."""
        from bubble.cli import _provision_container

        ref_path = tmp_path / "repo.git"
        ref_path.mkdir()

        claude_mounts = [
            MountSpec(
                source="/home/testuser/.claude/CLAUDE.md",
                target="/home/user/.claude/CLAUDE.md",
                readonly=True,
            ),
        ]

        _provision_container(
            mock_runtime,
            "test-container",
            "base",
            ref_path,
            "repo.git",
            {},
            claude_mounts=claude_mounts,
        )

        exec_calls = [c for c in mock_runtime.calls if c[0] == "exec"]
        mkdir_calls = [c for c in exec_calls if ".claude" in " ".join(c[2])]
        assert len(mkdir_calls) == 1
        assert "mkdir -p /home/user/.claude" in " ".join(mkdir_calls[0][2])
        assert "chown user:user" in " ".join(mkdir_calls[0][2])

    def test_projects_dir_mounted_writable(self, mock_runtime, tmp_path, tmp_data_dir):
        """Verify projects directory is mounted read-write and created on host."""
        from bubble.cli import _provision_container

        ref_path = tmp_path / "repo.git"
        ref_path.mkdir()

        claude_mounts = [
            MountSpec(
                source="/home/testuser/.claude/CLAUDE.md",
                target="/home/user/.claude/CLAUDE.md",
                readonly=True,
            ),
        ]

        _provision_container(
            mock_runtime,
            "test-container",
            "base",
            ref_path,
            "repo.git",
            {},
            claude_mounts=claude_mounts,
        )

        disk_calls = [c for c in mock_runtime.calls if c[0] == "add_disk"]
        projects_calls = [c for c in disk_calls if c[2] == "claude-projects"]
        assert len(projects_calls) == 1
        assert projects_calls[0][4] == "/home/user/.claude/projects"
        assert projects_calls[0][5] is False  # read-write

        # Host directory created
        projects_dir = tmp_data_dir / "claude-projects"
        assert projects_dir.is_dir()
        assert projects_dir.stat().st_mode & 0o770 == 0o770

    def test_no_claude_mounts(self, mock_runtime, tmp_path, tmp_data_dir):
        """No claude mount calls when claude_mounts is empty."""
        from bubble.cli import _provision_container

        ref_path = tmp_path / "repo.git"
        ref_path.mkdir()

        _provision_container(
            mock_runtime,
            "test-container",
            "base",
            ref_path,
            "repo.git",
            {},
            claude_mounts=[],
        )

        disk_calls = [c for c in mock_runtime.calls if c[0] == "add_disk"]
        claude_disk_calls = [c for c in disk_calls if "claude" in c[2]]
        assert len(claude_disk_calls) == 0
