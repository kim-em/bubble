"""Tests for user-specified mount support."""

from pathlib import Path

import pytest

from bubble.config import (
    MountSpec,
    claude_config_mounts,
    codex_config_mounts,
    editor_config_mounts,
    has_claude_credentials,
    has_codex_credentials,
    parse_mounts,
)


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
        from bubble.provisioning import provision_container as _provision_container

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
        from bubble.provisioning import provision_container as _provision_container

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
        from bubble.provisioning import provision_container as _provision_container

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
        from bubble.provisioning import provision_container as _provision_container

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
        """Mounts returned for config items that exist (credentials included by default)."""
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        (claude_dir / "CLAUDE.md").write_text("# test")
        (claude_dir / "settings.json").write_text("{}")
        (claude_dir / "skills").mkdir()
        (claude_dir / "keybindings.json").write_text("{}")
        (claude_dir / "commands").mkdir()
        (claude_dir / ".credentials.json").write_text("{}")

        monkeypatch.setattr("bubble.config.CLAUDE_CONFIG_DIR", claude_dir)

        mounts = claude_config_mounts()

        assert len(mounts) == 6
        targets = {m.target for m in mounts}
        assert "/home/user/.claude/CLAUDE.md" in targets
        assert "/home/user/.claude/settings.json" in targets
        assert "/home/user/.claude/skills" in targets
        assert "/home/user/.claude/keybindings.json" in targets
        assert "/home/user/.claude/commands" in targets
        # Credentials included by default
        assert "/home/user/.claude/.credentials.json" in targets
        assert all(m.readonly for m in mounts)

    def test_returns_all_with_credentials(self, tmp_path, monkeypatch):
        """All items returned when include_credentials=True."""
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        (claude_dir / "CLAUDE.md").write_text("# test")
        (claude_dir / "settings.json").write_text("{}")
        (claude_dir / "skills").mkdir()
        (claude_dir / "keybindings.json").write_text("{}")
        (claude_dir / "commands").mkdir()
        (claude_dir / ".credentials.json").write_text("{}")

        monkeypatch.setattr("bubble.config.CLAUDE_CONFIG_DIR", claude_dir)

        mounts = claude_config_mounts(include_credentials=True)

        assert len(mounts) == 6
        targets = {m.target for m in mounts}
        assert "/home/user/.claude/.credentials.json" in targets

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

    def test_credentials_included_by_default(self, tmp_path, monkeypatch):
        """Credential files are mounted by default."""
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        (claude_dir / ".credentials.json").write_text("{}")

        monkeypatch.setattr("bubble.config.CLAUDE_CONFIG_DIR", claude_dir)

        mounts = claude_config_mounts()

        targets = {m.target for m in mounts}
        assert "/home/user/.claude/.credentials.json" in targets
        assert all(m.readonly for m in mounts)

    def test_credentials_excluded_when_requested(self, tmp_path, monkeypatch):
        """Credential files are NOT mounted when include_credentials=False."""
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        (claude_dir / ".credentials.json").write_text("{}")

        monkeypatch.setattr("bubble.config.CLAUDE_CONFIG_DIR", claude_dir)

        mounts = claude_config_mounts(include_credentials=False)

        assert mounts == []

    def test_has_claude_credentials(self, tmp_path, monkeypatch):
        """has_claude_credentials() detects credential files."""
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        monkeypatch.setattr("bubble.config.CLAUDE_CONFIG_DIR", claude_dir)

        assert not has_claude_credentials()

        (claude_dir / ".credentials.json").write_text("{}")
        assert has_claude_credentials()

    def test_rejects_symlinks_escaping_claude_dir(self, tmp_path, monkeypatch):
        """Symlinks that escape ~/.claude are rejected."""
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        # Create a file outside ~/.claude
        secret = tmp_path / "secret.txt"
        secret.write_text("sensitive data")
        # Symlink from inside ~/.claude to outside
        (claude_dir / "CLAUDE.md").symlink_to(secret)

        monkeypatch.setattr("bubble.config.CLAUDE_CONFIG_DIR", claude_dir)

        mounts = claude_config_mounts()

        assert mounts == []

    def test_allows_symlinks_within_claude_dir(self, tmp_path, monkeypatch):
        """Symlinks within ~/.claude are allowed."""
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        real = claude_dir / "real-claude.md"
        real.write_text("# config")
        (claude_dir / "CLAUDE.md").symlink_to(real)

        monkeypatch.setattr("bubble.config.CLAUDE_CONFIG_DIR", claude_dir)

        mounts = claude_config_mounts()

        assert len(mounts) == 1
        assert mounts[0].target == "/home/user/.claude/CLAUDE.md"

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
        from bubble.provisioning import provision_container as _provision_container

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
        from bubble.provisioning import provision_container as _provision_container

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
        from bubble.provisioning import provision_container as _provision_container

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

        # Host directory created with per-bubble subdirectory
        projects_dir = tmp_data_dir / "claude-projects" / "test-container"
        assert projects_dir.is_dir()
        assert projects_dir.stat().st_mode & 0o770 == 0o770

    def test_no_claude_mounts(self, mock_runtime, tmp_path, tmp_data_dir):
        """No claude mount calls when claude_mounts is empty."""
        from bubble.provisioning import provision_container as _provision_container

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

    def test_projects_dir_skipped_when_user_mount_overlaps(
        self, mock_runtime, tmp_path, tmp_data_dir
    ):
        """Projects dir not mounted if user mount targets /home/user/.claude/projects."""
        from bubble.provisioning import provision_container as _provision_container

        ref_path = tmp_path / "repo.git"
        ref_path.mkdir()

        claude_mounts = [
            MountSpec(
                source="/home/testuser/.claude/CLAUDE.md",
                target="/home/user/.claude/CLAUDE.md",
                readonly=True,
            ),
        ]
        user_mounts = [
            MountSpec(
                source="/tmp/my-projects",
                target="/home/user/.claude/projects",
                readonly=False,
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
            user_mounts=user_mounts,
        )

        disk_calls = [c for c in mock_runtime.calls if c[0] == "add_disk"]
        projects_calls = [c for c in disk_calls if c[2] == "claude-projects"]
        assert len(projects_calls) == 0


class TestCodexConfigMounts:
    """Test automatic ~/.codex config mounting."""

    def test_returns_existing_config(self, tmp_path, monkeypatch):
        """Mounts returned for config items that exist (credentials included by default)."""
        codex_dir = tmp_path / ".codex"
        codex_dir.mkdir()
        (codex_dir / "config.toml").write_text("[settings]")
        (codex_dir / "auth.json").write_text("{}")

        monkeypatch.setattr("bubble.config.CODEX_CONFIG_DIR", codex_dir)

        mounts = codex_config_mounts()

        assert len(mounts) == 2
        targets = {m.target for m in mounts}
        assert "/home/user/.codex/config.toml" in targets
        # Credentials included by default
        assert "/home/user/.codex/auth.json" in targets
        assert all(m.readonly for m in mounts)

    def test_returns_all_with_credentials(self, tmp_path, monkeypatch):
        """All items returned when include_credentials=True."""
        codex_dir = tmp_path / ".codex"
        codex_dir.mkdir()
        (codex_dir / "config.toml").write_text("[settings]")
        (codex_dir / "auth.json").write_text("{}")

        monkeypatch.setattr("bubble.config.CODEX_CONFIG_DIR", codex_dir)

        mounts = codex_config_mounts(include_credentials=True)

        assert len(mounts) == 2
        targets = {m.target for m in mounts}
        assert "/home/user/.codex/config.toml" in targets
        assert "/home/user/.codex/auth.json" in targets

    def test_skips_missing_files(self, tmp_path, monkeypatch):
        """Only existing files are mounted."""
        codex_dir = tmp_path / ".codex"
        codex_dir.mkdir()
        (codex_dir / "config.toml").write_text("[settings]")

        monkeypatch.setattr("bubble.config.CODEX_CONFIG_DIR", codex_dir)

        mounts = codex_config_mounts(include_credentials=True)

        assert len(mounts) == 1
        assert mounts[0].target == "/home/user/.codex/config.toml"

    def test_no_codex_dir(self, tmp_path, monkeypatch):
        """Returns empty when ~/.codex doesn't exist."""
        monkeypatch.setattr("bubble.config.CODEX_CONFIG_DIR", tmp_path / "nonexistent")

        mounts = codex_config_mounts()

        assert mounts == []

    def test_credentials_included_by_default(self, tmp_path, monkeypatch):
        """Credential files are mounted by default."""
        codex_dir = tmp_path / ".codex"
        codex_dir.mkdir()
        (codex_dir / "auth.json").write_text("{}")

        monkeypatch.setattr("bubble.config.CODEX_CONFIG_DIR", codex_dir)

        mounts = codex_config_mounts()

        targets = {m.target for m in mounts}
        assert "/home/user/.codex/auth.json" in targets
        assert all(m.readonly for m in mounts)

    def test_credentials_excluded_when_requested(self, tmp_path, monkeypatch):
        """Credential files are NOT mounted when include_credentials=False."""
        codex_dir = tmp_path / ".codex"
        codex_dir.mkdir()
        (codex_dir / "auth.json").write_text("{}")

        monkeypatch.setattr("bubble.config.CODEX_CONFIG_DIR", codex_dir)

        mounts = codex_config_mounts(include_credentials=False)

        assert mounts == []

    def test_has_codex_credentials(self, tmp_path, monkeypatch):
        """has_codex_credentials() detects credential files."""
        codex_dir = tmp_path / ".codex"
        codex_dir.mkdir()
        monkeypatch.setattr("bubble.config.CODEX_CONFIG_DIR", codex_dir)

        assert not has_codex_credentials()

        (codex_dir / "auth.json").write_text("{}")
        assert has_codex_credentials()

    def test_rejects_symlinks_escaping_codex_dir(self, tmp_path, monkeypatch):
        """Symlinks that escape ~/.codex are rejected."""
        codex_dir = tmp_path / ".codex"
        codex_dir.mkdir()
        # Create a file outside ~/.codex
        secret = tmp_path / "secret.txt"
        secret.write_text("sensitive data")
        # Symlink from inside ~/.codex to outside
        (codex_dir / "config.toml").symlink_to(secret)

        monkeypatch.setattr("bubble.config.CODEX_CONFIG_DIR", codex_dir)

        mounts = codex_config_mounts()

        assert mounts == []

    def test_allows_symlinks_within_codex_dir(self, tmp_path, monkeypatch):
        """Symlinks within ~/.codex are allowed."""
        codex_dir = tmp_path / ".codex"
        codex_dir.mkdir()
        real = codex_dir / "real-config.toml"
        real.write_text("[settings]")
        (codex_dir / "config.toml").symlink_to(real)

        monkeypatch.setattr("bubble.config.CODEX_CONFIG_DIR", codex_dir)

        mounts = codex_config_mounts()

        assert len(mounts) == 1
        assert mounts[0].target == "/home/user/.codex/config.toml"

    def test_sources_are_absolute(self, tmp_path, monkeypatch):
        """Mount sources use absolute paths."""
        codex_dir = tmp_path / ".codex"
        codex_dir.mkdir()
        (codex_dir / "config.toml").write_text("[settings]")

        monkeypatch.setattr("bubble.config.CODEX_CONFIG_DIR", codex_dir)

        mounts = codex_config_mounts()

        assert Path(mounts[0].source).is_absolute()


class TestCodexConfigProvisioning:
    """Test that codex config mounts are applied during container provisioning."""

    def test_codex_mounts_applied(self, mock_runtime, tmp_path, tmp_data_dir):
        """Verify add_disk calls with codex-config device names."""
        from bubble.provisioning import provision_container as _provision_container

        ref_path = tmp_path / "repo.git"
        ref_path.mkdir()

        codex_mounts = [
            MountSpec(
                source="/home/testuser/.codex/config.toml",
                target="/home/user/.codex/config.toml",
                readonly=True,
            ),
            MountSpec(
                source="/home/testuser/.codex/auth.json",
                target="/home/user/.codex/auth.json",
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
            codex_mounts=codex_mounts,
        )

        disk_calls = [c for c in mock_runtime.calls if c[0] == "add_disk"]
        codex_disk_calls = [c for c in disk_calls if "codex-config" in c[2]]
        assert len(codex_disk_calls) == 2
        assert codex_disk_calls[0] == (
            "add_disk",
            "test-container",
            "codex-config-0",
            "/home/testuser/.codex/config.toml",
            "/home/user/.codex/config.toml",
            True,
        )
        assert codex_disk_calls[1] == (
            "add_disk",
            "test-container",
            "codex-config-1",
            "/home/testuser/.codex/auth.json",
            "/home/user/.codex/auth.json",
            True,
        )

    def test_creates_codex_dir_in_container(self, mock_runtime, tmp_path, tmp_data_dir):
        """Verify .codex directory is created before mounting."""
        from bubble.provisioning import provision_container as _provision_container

        ref_path = tmp_path / "repo.git"
        ref_path.mkdir()

        codex_mounts = [
            MountSpec(
                source="/home/testuser/.codex/auth.json",
                target="/home/user/.codex/auth.json",
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
            codex_mounts=codex_mounts,
        )

        exec_calls = [c for c in mock_runtime.calls if c[0] == "exec"]
        mkdir_calls = [c for c in exec_calls if ".codex" in " ".join(c[2])]
        assert len(mkdir_calls) == 1
        assert "mkdir -p /home/user/.codex" in " ".join(mkdir_calls[0][2])
        assert "chown user:user" in " ".join(mkdir_calls[0][2])

    def test_no_codex_mounts(self, mock_runtime, tmp_path, tmp_data_dir):
        """No codex mount calls when codex_mounts is empty."""
        from bubble.provisioning import provision_container as _provision_container

        ref_path = tmp_path / "repo.git"
        ref_path.mkdir()

        _provision_container(
            mock_runtime,
            "test-container",
            "base",
            ref_path,
            "repo.git",
            {},
            codex_mounts=[],
        )

        disk_calls = [c for c in mock_runtime.calls if c[0] == "add_disk"]
        codex_disk_calls = [c for c in disk_calls if "codex" in c[2]]
        assert len(codex_disk_calls) == 0


class TestMountOverlaps:
    """Test path ancestry overlap detection."""

    def test_exact_match(self):
        from bubble.provisioning import mount_overlaps as _mount_overlaps

        assert _mount_overlaps(
            Path("/home/user/.claude/CLAUDE.md"),
            {Path("/home/user/.claude/CLAUDE.md")},
        )

    def test_target_inside_user_mount(self):
        from bubble.provisioning import mount_overlaps as _mount_overlaps

        assert _mount_overlaps(
            Path("/home/user/.claude/CLAUDE.md"),
            {Path("/home/user/.claude")},
        )

    def test_user_mount_inside_target(self):
        from bubble.provisioning import mount_overlaps as _mount_overlaps

        assert _mount_overlaps(
            Path("/home/user/.claude"),
            {Path("/home/user/.claude/CLAUDE.md")},
        )

    def test_no_overlap(self):
        from bubble.provisioning import mount_overlaps as _mount_overlaps

        assert not _mount_overlaps(
            Path("/home/user/.claude/CLAUDE.md"),
            {Path("/home/user/projects")},
        )

    def test_empty_user_targets(self):
        from bubble.provisioning import mount_overlaps as _mount_overlaps

        assert not _mount_overlaps(
            Path("/home/user/.claude/CLAUDE.md"),
            set(),
        )


class TestRemoteClaudeConfig:
    """Test that --no-claude-config is forwarded to remote opens."""

    def test_no_claude_config_forwarded(self):
        """remote_open appends --no-claude-config when disabled."""
        from unittest.mock import MagicMock, patch

        from bubble.remote import RemoteHost, remote_open

        host = RemoteHost(hostname="test.example.com", user="root")

        with (
            patch("bubble.remote.ensure_remote_bubble"),
            patch("bubble.remote._find_remote_python", return_value="python3"),
            patch("subprocess.Popen") as mock_popen,
        ):
            proc = MagicMock()
            proc.stdout.__iter__ = MagicMock(
                return_value=iter(['{"name": "test", "status": "ok"}\n'])
            )
            proc.stderr = MagicMock()
            proc.stderr.read = MagicMock(return_value="")
            proc.wait = MagicMock(return_value=0)
            proc.returncode = 0
            mock_popen.return_value = proc

            try:
                remote_open(host, "target", claude_config=False)
            except Exception:
                pass  # May fail on JSON parsing, that's fine

            # Check that --no-claude-config was in the command
            call_args = mock_popen.call_args
            cmd = call_args[0][0] if call_args[0] else call_args[1].get("args", [])
            cmd_str = " ".join(cmd) if isinstance(cmd, list) else cmd
            assert "--no-claude-config" in cmd_str


class TestEditorConfigMounts:
    """Test automatic editor config mounting for emacs/neovim."""

    def _patch_editor(self, monkeypatch, tmp_path, editor_config):
        """Helper: patch both _EDITOR_CONFIG and _EDITOR_SAFE_ROOTS for tmp_path."""
        monkeypatch.setattr("bubble.config._EDITOR_CONFIG", editor_config)
        monkeypatch.setattr(
            "bubble.config._EDITOR_SAFE_ROOTS",
            [tmp_path / ".config", tmp_path / ".emacs.d", tmp_path / ".local", tmp_path / ".cache"],
        )

    def test_vscode_returns_empty(self):
        """VSCode doesn't need host config mounts."""
        assert editor_config_mounts("vscode") == []

    def test_shell_returns_empty(self):
        """Shell editor doesn't need config mounts."""
        assert editor_config_mounts("shell") == []

    def test_unknown_editor_returns_empty(self):
        assert editor_config_mounts("vim") == []

    def test_emacs_xdg_config(self, tmp_path, monkeypatch):
        """Emacs XDG config (~/.config/emacs/) is mounted read-only."""
        config_dir = tmp_path / ".config" / "emacs"
        config_dir.mkdir(parents=True)
        (config_dir / "init.el").write_text(";; init")

        self._patch_editor(
            monkeypatch,
            tmp_path,
            {
                "emacs": {
                    "config": [
                        (config_dir, "/home/user/.config/emacs"),
                        (tmp_path / ".emacs.d", "/home/user/.emacs.d"),
                    ],
                    "data": [],
                    "config_writable_subdirs": [],
                },
            },
        )

        mounts = editor_config_mounts("emacs")
        assert len(mounts) == 1
        assert mounts[0].target == "/home/user/.config/emacs"
        assert mounts[0].readonly is True

    def test_emacs_fallback_to_emacs_d(self, tmp_path, monkeypatch):
        """Falls back to ~/.emacs.d/ when XDG location doesn't exist."""
        emacs_d = tmp_path / ".emacs.d"
        emacs_d.mkdir()
        (emacs_d / "init.el").write_text(";; init")

        self._patch_editor(
            monkeypatch,
            tmp_path,
            {
                "emacs": {
                    "config": [
                        (tmp_path / ".config" / "emacs", "/home/user/.config/emacs"),
                        (emacs_d, "/home/user/.emacs.d"),
                    ],
                    "data": [],
                    "config_writable_subdirs": [],
                },
            },
        )

        mounts = editor_config_mounts("emacs")
        assert len(mounts) == 1
        assert mounts[0].target == "/home/user/.emacs.d"
        assert mounts[0].readonly is True

    def test_emacs_xdg_preferred_over_emacs_d(self, tmp_path, monkeypatch):
        """When both exist, XDG location wins."""
        config_dir = tmp_path / ".config" / "emacs"
        config_dir.mkdir(parents=True)
        emacs_d = tmp_path / ".emacs.d"
        emacs_d.mkdir()

        self._patch_editor(
            monkeypatch,
            tmp_path,
            {
                "emacs": {
                    "config": [
                        (config_dir, "/home/user/.config/emacs"),
                        (emacs_d, "/home/user/.emacs.d"),
                    ],
                    "data": [],
                    "config_writable_subdirs": [],
                },
            },
        )

        mounts = editor_config_mounts("emacs")
        assert len(mounts) == 1
        assert mounts[0].target == "/home/user/.config/emacs"

    def test_emacs_data_dirs_writable(self, tmp_path, monkeypatch):
        """Emacs data directories are mounted read-write."""
        config_dir = tmp_path / ".config" / "emacs"
        config_dir.mkdir(parents=True)
        share_dir = tmp_path / ".local" / "share" / "emacs"
        share_dir.mkdir(parents=True)
        cache_dir = tmp_path / ".cache" / "emacs"
        cache_dir.mkdir(parents=True)

        self._patch_editor(
            monkeypatch,
            tmp_path,
            {
                "emacs": {
                    "config": [
                        (config_dir, "/home/user/.config/emacs"),
                    ],
                    "data": [
                        (share_dir, "/home/user/.local/share/emacs"),
                        (cache_dir, "/home/user/.cache/emacs"),
                    ],
                    "config_writable_subdirs": [],
                },
            },
        )

        mounts = editor_config_mounts("emacs")
        assert len(mounts) == 3
        # Config is read-only
        assert mounts[0].readonly is True
        # Data dirs are read-write
        assert mounts[1].target == "/home/user/.local/share/emacs"
        assert mounts[1].readonly is False
        assert mounts[2].target == "/home/user/.cache/emacs"
        assert mounts[2].readonly is False

    def test_neovim_config(self, tmp_path, monkeypatch):
        """Neovim config is mounted read-only."""
        nvim_dir = tmp_path / ".config" / "nvim"
        nvim_dir.mkdir(parents=True)
        (nvim_dir / "init.lua").write_text("-- init")

        self._patch_editor(
            monkeypatch,
            tmp_path,
            {
                "neovim": {
                    "config": [
                        (nvim_dir, "/home/user/.config/nvim"),
                    ],
                    "data": [],
                    "config_writable_subdirs": [],
                },
            },
        )

        mounts = editor_config_mounts("neovim")
        assert len(mounts) == 1
        assert mounts[0].target == "/home/user/.config/nvim"
        assert mounts[0].readonly is True

    def test_neovim_data_dirs_writable(self, tmp_path, monkeypatch):
        """Neovim data/state/cache directories are mounted read-write."""
        nvim_dir = tmp_path / ".config" / "nvim"
        nvim_dir.mkdir(parents=True)
        share_dir = tmp_path / ".local" / "share" / "nvim"
        share_dir.mkdir(parents=True)
        state_dir = tmp_path / ".local" / "state" / "nvim"
        state_dir.mkdir(parents=True)
        cache_dir = tmp_path / ".cache" / "nvim"
        cache_dir.mkdir(parents=True)

        self._patch_editor(
            monkeypatch,
            tmp_path,
            {
                "neovim": {
                    "config": [
                        (nvim_dir, "/home/user/.config/nvim"),
                    ],
                    "data": [
                        (share_dir, "/home/user/.local/share/nvim"),
                        (state_dir, "/home/user/.local/state/nvim"),
                        (cache_dir, "/home/user/.cache/nvim"),
                    ],
                    "config_writable_subdirs": [],
                },
            },
        )

        mounts = editor_config_mounts("neovim")
        assert len(mounts) == 4
        assert mounts[0].readonly is True  # config
        assert all(not m.readonly for m in mounts[1:])  # data dirs writable

    def test_skips_missing_data_dirs(self, tmp_path, monkeypatch):
        """Only existing data directories are mounted."""
        nvim_dir = tmp_path / ".config" / "nvim"
        nvim_dir.mkdir(parents=True)
        # Only create share, not state or cache
        share_dir = tmp_path / ".local" / "share" / "nvim"
        share_dir.mkdir(parents=True)

        self._patch_editor(
            monkeypatch,
            tmp_path,
            {
                "neovim": {
                    "config": [
                        (nvim_dir, "/home/user/.config/nvim"),
                    ],
                    "data": [
                        (share_dir, "/home/user/.local/share/nvim"),
                        (tmp_path / ".local" / "state" / "nvim", "/home/user/.local/state/nvim"),
                        (tmp_path / ".cache" / "nvim", "/home/user/.cache/nvim"),
                    ],
                    "config_writable_subdirs": [],
                },
            },
        )

        mounts = editor_config_mounts("neovim")
        assert len(mounts) == 2  # config + share only

    def test_no_config_dir_returns_empty(self, tmp_path, monkeypatch):
        """Returns empty when no config directory exists."""
        self._patch_editor(
            monkeypatch,
            tmp_path,
            {
                "emacs": {
                    "config": [
                        (tmp_path / "nonexistent1", "/home/user/.config/emacs"),
                        (tmp_path / "nonexistent2", "/home/user/.emacs.d"),
                    ],
                    "data": [
                        (tmp_path / "nonexistent3", "/home/user/.local/share/emacs"),
                    ],
                    "config_writable_subdirs": [],
                },
            },
        )

        mounts = editor_config_mounts("emacs")
        assert mounts == []

    def test_data_dirs_skipped_when_no_config(self, tmp_path, monkeypatch):
        """Data dirs are NOT mounted when no config directory exists."""
        # Only data dirs exist, no config dir
        share_dir = tmp_path / ".local" / "share" / "nvim"
        share_dir.mkdir(parents=True)

        self._patch_editor(
            monkeypatch,
            tmp_path,
            {
                "neovim": {
                    "config": [
                        (tmp_path / ".config" / "nvim", "/home/user/.config/nvim"),
                    ],
                    "data": [
                        (share_dir, "/home/user/.local/share/nvim"),
                    ],
                    "config_writable_subdirs": [],
                },
            },
        )

        mounts = editor_config_mounts("neovim")
        assert mounts == []

    def test_sources_are_absolute(self, tmp_path, monkeypatch):
        """Mount sources use absolute paths."""
        nvim_dir = tmp_path / ".config" / "nvim"
        nvim_dir.mkdir(parents=True)

        self._patch_editor(
            monkeypatch,
            tmp_path,
            {
                "neovim": {
                    "config": [(nvim_dir, "/home/user/.config/nvim")],
                    "data": [],
                    "config_writable_subdirs": [],
                },
            },
        )

        mounts = editor_config_mounts("neovim")
        assert Path(mounts[0].source).is_absolute()

    def test_rejects_symlinks_escaping_safe_roots(self, tmp_path, monkeypatch):
        """Symlinks that escape safe root directories are rejected."""
        # Create a secret dir outside safe roots
        secret = tmp_path / "secret"
        secret.mkdir()
        # Create a config dir that's a symlink to the secret dir
        config_link = tmp_path / ".config" / "nvim"
        config_link.parent.mkdir(parents=True)
        config_link.symlink_to(secret)

        self._patch_editor(
            monkeypatch,
            tmp_path,
            {
                "neovim": {
                    "config": [(config_link, "/home/user/.config/nvim")],
                    "data": [],
                    "config_writable_subdirs": [],
                },
            },
        )

        mounts = editor_config_mounts("neovim")
        assert mounts == []

    def test_allows_symlinks_within_safe_roots(self, tmp_path, monkeypatch):
        """Symlinks within safe root directories are allowed."""
        real_dir = tmp_path / ".config" / "nvim-real"
        real_dir.mkdir(parents=True)
        (real_dir / "init.lua").write_text("-- init")
        link_dir = tmp_path / ".config" / "nvim"
        link_dir.symlink_to(real_dir)

        self._patch_editor(
            monkeypatch,
            tmp_path,
            {
                "neovim": {
                    "config": [(link_dir, "/home/user/.config/nvim")],
                    "data": [],
                    "config_writable_subdirs": [],
                },
            },
        )

        mounts = editor_config_mounts("neovim")
        assert len(mounts) == 1

    def test_emacs_d_writable_subdirs(self, tmp_path, monkeypatch):
        """Legacy ~/.emacs.d/ gets exclusions for known writable subdirs."""
        emacs_d = tmp_path / ".emacs.d"
        emacs_d.mkdir()
        (emacs_d / "init.el").write_text(";; init")
        # Create some writable subdirs that should be excluded
        (emacs_d / "elpa").mkdir()
        (emacs_d / "eln-cache").mkdir()

        self._patch_editor(
            monkeypatch,
            tmp_path,
            {
                "emacs": {
                    "config": [
                        (tmp_path / ".config" / "emacs", "/home/user/.config/emacs"),
                        (emacs_d, "/home/user/.emacs.d"),
                    ],
                    "data": [],
                    "config_writable_subdirs": ["elpa", "eln-cache", "straight"],
                },
            },
        )

        mounts = editor_config_mounts("emacs")
        assert len(mounts) == 1
        assert mounts[0].readonly is True
        # Only existing subdirs should appear in exclude list
        assert "elpa" in mounts[0].exclude
        assert "eln-cache" in mounts[0].exclude
        assert "straight" not in mounts[0].exclude  # doesn't exist on host


class TestEditorConfigProvisioning:
    """Test that editor config mounts are applied during container provisioning."""

    def test_editor_mounts_applied(self, mock_runtime, tmp_path):
        """Verify add_disk calls with editor-config device names."""
        from bubble.provisioning import provision_container as _provision_container

        ref_path = tmp_path / "repo.git"
        ref_path.mkdir()

        editor_mounts = [
            MountSpec(
                source="/home/testuser/.config/nvim",
                target="/home/user/.config/nvim",
                readonly=True,
            ),
            MountSpec(
                source="/home/testuser/.local/share/nvim",
                target="/home/user/.local/share/nvim",
                readonly=False,
            ),
        ]

        _provision_container(
            mock_runtime,
            "test-container",
            "base",
            ref_path,
            "repo.git",
            {},
            editor_mounts=editor_mounts,
        )

        disk_calls = [c for c in mock_runtime.calls if c[0] == "add_disk"]
        editor_disk_calls = [c for c in disk_calls if "editor-config" in c[2]]
        assert len(editor_disk_calls) == 2
        assert editor_disk_calls[0] == (
            "add_disk",
            "test-container",
            "editor-config-0",
            "/home/testuser/.config/nvim",
            "/home/user/.config/nvim",
            True,
        )
        assert editor_disk_calls[1] == (
            "add_disk",
            "test-container",
            "editor-config-1",
            "/home/testuser/.local/share/nvim",
            "/home/user/.local/share/nvim",
            False,
        )

    def test_creates_parent_dirs_in_container(self, mock_runtime, tmp_path):
        """Verify parent directories are created before mounting."""
        from bubble.provisioning import provision_container as _provision_container

        ref_path = tmp_path / "repo.git"
        ref_path.mkdir()

        editor_mounts = [
            MountSpec(
                source="/home/testuser/.config/nvim",
                target="/home/user/.config/nvim",
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
            editor_mounts=editor_mounts,
        )

        exec_calls = [c for c in mock_runtime.calls if c[0] == "exec"]
        mkdir_calls = [
            c for c in exec_calls if ".config" in " ".join(c[2]) and "mkdir" in " ".join(c[2])
        ]
        assert len(mkdir_calls) >= 1
        assert "chown" in " ".join(mkdir_calls[0][2])

    def test_no_editor_mounts(self, mock_runtime, tmp_path):
        """No editor mount calls when editor_mounts is empty."""
        from bubble.provisioning import provision_container as _provision_container

        ref_path = tmp_path / "repo.git"
        ref_path.mkdir()

        _provision_container(
            mock_runtime,
            "test-container",
            "base",
            ref_path,
            "repo.git",
            {},
            editor_mounts=[],
        )

        disk_calls = [c for c in mock_runtime.calls if c[0] == "add_disk"]
        editor_disk_calls = [c for c in disk_calls if "editor-config" in c[2]]
        assert len(editor_disk_calls) == 0

    def test_exclusions_create_tmpfs_overlays(self, mock_runtime, tmp_path):
        """Verify exclusions create tmpfs exec calls in container."""
        from bubble.provisioning import provision_container as _provision_container

        ref_path = tmp_path / "repo.git"
        ref_path.mkdir()

        editor_mounts = [
            MountSpec(
                source="/home/testuser/.emacs.d",
                target="/home/user/.emacs.d",
                readonly=True,
                exclude=["elpa", "eln-cache"],
            ),
        ]

        _provision_container(
            mock_runtime,
            "test-container",
            "base",
            ref_path,
            "repo.git",
            {},
            editor_mounts=editor_mounts,
        )

        exec_calls = [c for c in mock_runtime.calls if c[0] == "exec"]
        tmpfs_execs = [c for c in exec_calls if "tmpfs" in " ".join(c[2])]
        assert len(tmpfs_execs) == 2
        # Check paths include the exclusion subdirs
        all_cmds = " ".join(" ".join(c[2]) for c in tmpfs_execs)
        assert "/home/user/.emacs.d/elpa" in all_cmds
        assert "/home/user/.emacs.d/eln-cache" in all_cmds
