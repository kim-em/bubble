"""Tests for configuration management."""

from bubble.config import _deep_merge, repo_short_name


def test_repo_short_name():
    assert repo_short_name("leanprover-community/mathlib4") == "mathlib4"


def test_repo_short_name_lowercases():
    assert repo_short_name("org/MyRepo") == "myrepo"


def test_deep_merge_basic():
    base = {"a": 1, "b": 2}
    override = {"b": 3, "c": 4}
    result = _deep_merge(base, override)
    assert result == {"a": 1, "b": 3, "c": 4}


def test_deep_merge_nested():
    base = {"x": {"a": 1, "b": 2}}
    override = {"x": {"b": 3, "c": 4}}
    result = _deep_merge(base, override)
    assert result == {"x": {"a": 1, "b": 3, "c": 4}}


def test_deep_merge_override_replaces_non_dict():
    base = {"x": {"a": 1}}
    override = {"x": "replaced"}
    result = _deep_merge(base, override)
    assert result == {"x": "replaced"}


def test_load_config_creates_default(tmp_data_dir):
    from bubble.config import load_config

    config = load_config()
    assert config["runtime"]["backend"] == "incus"
    assert "github.com" in config["network"]["allowlist"]


def test_save_load_roundtrip(tmp_data_dir):
    from bubble.config import load_config, save_config

    config = load_config()
    config["runtime"]["colima_cpu"] = 42
    save_config(config)

    reloaded = load_config()
    assert reloaded["runtime"]["colima_cpu"] == 42


def test_default_config_has_claude_credentials_false(tmp_data_dir):
    from bubble.config import load_config

    config = load_config()
    assert config["claude"]["credentials"] is False


def test_claude_credentials_roundtrip(tmp_data_dir):
    from bubble.config import load_config, save_config

    config = load_config()
    config["claude"]["credentials"] = True
    save_config(config)

    reloaded = load_config()
    assert reloaded["claude"]["credentials"] is True


def test_claude_credentials_on_cli(tmp_data_dir):
    from click.testing import CliRunner

    from bubble.cli import main

    runner = CliRunner()
    result = runner.invoke(main, ["claude", "credentials", "on"])
    assert result.exit_code == 0
    assert "enabled" in result.output

    from bubble.config import load_config

    config = load_config()
    assert config["claude"]["credentials"] is True


def test_claude_credentials_off_cli(tmp_data_dir):
    from click.testing import CliRunner

    from bubble.cli import main

    runner = CliRunner()
    # First enable
    runner.invoke(main, ["claude", "credentials", "on"])
    # Then disable
    result = runner.invoke(main, ["claude", "credentials", "off"])
    assert result.exit_code == 0
    assert "disabled" in result.output

    from bubble.config import load_config

    config = load_config()
    assert config["claude"]["credentials"] is False


def test_claude_credentials_show_current(tmp_data_dir):
    from click.testing import CliRunner

    from bubble.cli import main

    runner = CliRunner()
    result = runner.invoke(main, ["claude", "credentials"])
    assert result.exit_code == 0
    assert "off" in result.output


def test_claude_status_cli(tmp_data_dir):
    from click.testing import CliRunner

    from bubble.cli import main

    runner = CliRunner()
    result = runner.invoke(main, ["claude", "status"])
    assert result.exit_code == 0
    assert "credentials: off" in result.output

    # Enable and check again
    runner.invoke(main, ["claude", "credentials", "on"])
    result = runner.invoke(main, ["claude", "status"])
    assert result.exit_code == 0
    assert "credentials: on" in result.output
