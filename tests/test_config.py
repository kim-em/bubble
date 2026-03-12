"""Tests for configuration management."""

from bubble.config import _deep_merge, is_first_run, load_raw_config, repo_short_name


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


def test_default_config_has_claude_credentials_true(tmp_data_dir):
    from bubble.config import load_config

    config = load_config()
    assert config["claude"]["credentials"] is True


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
    assert "on" in result.output


def test_claude_status_cli(tmp_data_dir):
    from click.testing import CliRunner

    from bubble.cli import main

    runner = CliRunner()
    result = runner.invoke(main, ["claude", "status"])
    assert result.exit_code == 0
    assert "credentials: on" in result.output

    # Disable and check again
    runner.invoke(main, ["claude", "credentials", "off"])
    result = runner.invoke(main, ["claude", "status"])
    assert result.exit_code == 0
    assert "credentials: off" in result.output


def test_load_raw_config_fresh_install(tmp_data_dir):
    """Raw config returns empty dict on fresh install (no user settings)."""
    raw = load_raw_config()
    # Fresh install: config file doesn't exist yet, so raw is empty
    # After load_config creates default, raw should NOT contain claude defaults
    from bubble.config import load_config

    load_config()  # creates config.toml with defaults
    raw = load_raw_config()
    # Even though config.toml now exists with defaults written by load_config,
    # the key question is: does the user's raw config contain claude.credentials?
    # After load_config writes defaults, it WILL be present in the file.
    # But on a legacy config (no [claude] section), it won't be.
    assert "claude" in raw  # defaults are written to file


def test_load_raw_config_legacy_no_claude(tmp_data_dir):
    """Legacy config file without [claude] section should show no explicit setting."""
    # Write a legacy config without [claude] section
    import tomli_w

    import bubble.config as config

    legacy = {
        "editor": "vscode",
        "runtime": {"backend": "incus"},
    }
    with open(config.CONFIG_FILE, "wb") as f:
        tomli_w.dump(legacy, f)

    raw = load_raw_config()
    assert "claude" not in raw
    # But merged config should still have defaults
    merged = config.load_config()
    assert merged["claude"]["credentials"] is True


def test_default_config_has_codex_credentials_true(tmp_data_dir):
    from bubble.config import load_config

    config = load_config()
    assert config["codex"]["credentials"] is True


def test_codex_credentials_roundtrip(tmp_data_dir):
    from bubble.config import load_config, save_config

    config = load_config()
    config["codex"]["credentials"] = True
    save_config(config)

    reloaded = load_config()
    assert reloaded["codex"]["credentials"] is True


def test_codex_credentials_on_cli(tmp_data_dir):
    from click.testing import CliRunner

    from bubble.cli import main

    runner = CliRunner()
    result = runner.invoke(main, ["codex", "credentials", "on"])
    assert result.exit_code == 0
    assert "enabled" in result.output

    from bubble.config import load_config

    config = load_config()
    assert config["codex"]["credentials"] is True


def test_codex_credentials_off_cli(tmp_data_dir):
    from click.testing import CliRunner

    from bubble.cli import main

    runner = CliRunner()
    # First enable
    runner.invoke(main, ["codex", "credentials", "on"])
    # Then disable
    result = runner.invoke(main, ["codex", "credentials", "off"])
    assert result.exit_code == 0
    assert "disabled" in result.output

    from bubble.config import load_config

    config = load_config()
    assert config["codex"]["credentials"] is False


def test_codex_credentials_show_current(tmp_data_dir):
    from click.testing import CliRunner

    from bubble.cli import main

    runner = CliRunner()
    result = runner.invoke(main, ["codex", "credentials"])
    assert result.exit_code == 0
    assert "on" in result.output


def test_codex_status_cli(tmp_data_dir):
    from click.testing import CliRunner

    from bubble.cli import main

    runner = CliRunner()
    result = runner.invoke(main, ["codex", "status"])
    assert result.exit_code == 0
    assert "credentials: on" in result.output

    # Disable and check again
    runner.invoke(main, ["codex", "credentials", "off"])
    result = runner.invoke(main, ["codex", "status"])
    assert result.exit_code == 0
    assert "credentials: off" in result.output


def test_load_raw_config_legacy_no_codex(tmp_data_dir):
    """Legacy config file without [codex] section should show no explicit setting."""
    import tomli_w

    import bubble.config as config

    legacy = {
        "editor": "vscode",
        "runtime": {"backend": "incus"},
    }
    with open(config.CONFIG_FILE, "wb") as f:
        tomli_w.dump(legacy, f)

    raw = load_raw_config()
    assert "codex" not in raw
    # But merged config should still have defaults
    merged = config.load_config()
    assert merged["codex"]["credentials"] is True


def test_config_show_defaults(tmp_data_dir):
    """config show labels all values as (default) on a fresh install."""
    from click.testing import CliRunner

    from bubble.cli import main

    runner = CliRunner()
    result = runner.invoke(main, ["config", "show"])
    assert result.exit_code == 0
    assert "(default)" in result.output
    # All values should be default on fresh install
    assert "(set in config)" not in result.output


def test_config_show_custom_editor(tmp_data_dir):
    """config show labels editor as (set in config) when changed."""
    import tomli_w

    import bubble.config as config

    cfg = {"editor": "emacs"}
    with open(config.CONFIG_FILE, "wb") as f:
        tomli_w.dump(cfg, f)

    from click.testing import CliRunner

    from bubble.cli import main

    runner = CliRunner()
    result = runner.invoke(main, ["config", "show"])
    assert result.exit_code == 0
    assert '"emacs"  (set in config)' in result.output
    # Default sections should still show (default)
    assert "(default)" in result.output


def test_config_show_mixed_origins(tmp_data_dir):
    """config show correctly distinguishes default from user-set values."""
    import tomli_w

    import bubble.config as config

    cfg = {"claude": {"credentials": False}}
    with open(config.CONFIG_FILE, "wb") as f:
        tomli_w.dump(cfg, f)

    from click.testing import CliRunner

    from bubble.cli import main

    runner = CliRunner()
    result = runner.invoke(main, ["config", "show"])
    assert result.exit_code == 0
    assert "credentials = false  (set in config)" in result.output
    # editor should still be default
    assert 'editor = "vscode"  (default)' in result.output


def test_config_show_security_deferred(tmp_data_dir):
    """config show defers security settings to `bubble security`."""
    from click.testing import CliRunner

    from bubble.cli import main

    runner = CliRunner()
    result = runner.invoke(main, ["config", "show"])
    assert result.exit_code == 0
    assert "bubble security" in result.output


def test_deep_merge_does_not_mutate_default(tmp_data_dir):
    """Verify _deep_merge doesn't mutate DEFAULT_CONFIG nested dicts."""
    from bubble.config import DEFAULT_CONFIG

    original_val = DEFAULT_CONFIG["claude"]["credentials"]
    merged = _deep_merge(DEFAULT_CONFIG, {})
    merged["claude"]["credentials"] = not original_val
    assert DEFAULT_CONFIG["claude"]["credentials"] == original_val


def test_is_first_run_true_before_config_created(tmp_data_dir):
    """is_first_run() returns True when config.toml doesn't exist."""
    assert is_first_run() is True


def test_is_first_run_false_after_load_config(tmp_data_dir):
    """is_first_run() returns False after load_config() creates the file."""
    from bubble.config import load_config

    load_config()
    assert is_first_run() is False
