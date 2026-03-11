"""Tests for the configurable security posture system."""

from click.testing import CliRunner

from bubble.security import (
    SETTINGS,
    filter_github_domains,
    get_setting,
    is_enabled,
    is_locked_off,
    print_warnings,
)

# --- Core resolution tests ---


def test_all_settings_have_valid_auto_defaults():
    for name, defn in SETTINGS.items():
        assert defn.auto_default in ("on", "off"), f"{name} has invalid auto_default"


def test_get_setting_defaults_to_auto():
    config = {}
    for name in SETTINGS:
        assert get_setting(config, name) == "auto"


def test_get_setting_reads_security_section():
    config = {"security": {"shared_cache": "on", "relay": "off"}}
    assert get_setting(config, "shared_cache") == "on"
    assert get_setting(config, "relay") == "off"


def test_get_setting_relay_backwards_compat():
    """When relay is auto but [relay] enabled = true, treat as on."""
    config = {"relay": {"enabled": True}}
    assert get_setting(config, "relay") == "on"


def test_get_setting_relay_backwards_compat_disabled():
    """When relay is auto and [relay] enabled = false, stay auto."""
    config = {"relay": {"enabled": False}}
    assert get_setting(config, "relay") == "auto"


def test_get_setting_relay_explicit_overrides_backwards_compat():
    """Explicit security.relay takes precedence over [relay] enabled."""
    config = {"security": {"relay": "off"}, "relay": {"enabled": True}}
    assert get_setting(config, "relay") == "off"


def test_get_setting_unknown_raises():
    import pytest

    with pytest.raises(ValueError, match="Unknown security setting"):
        get_setting({}, "nonexistent")


def test_is_enabled_auto_on():
    config = {}
    assert is_enabled(config, "shared_cache") is True  # auto_default = on
    assert is_enabled(config, "network_github") is True
    assert is_enabled(config, "host_key_trust") is True
    assert is_enabled(config, "cloud_root") is True
    assert is_enabled(config, "git_manifest_trust") is True
    assert is_enabled(config, "user_mounts") is True


def test_is_enabled_auto_off():
    config = {}
    assert is_enabled(config, "relay") is False  # auto_default = off
    assert is_enabled(config, "claude_credentials") is False


def test_is_enabled_explicit_on():
    config = {"security": {"relay": "on"}}
    assert is_enabled(config, "relay") is True


def test_is_enabled_explicit_off():
    config = {"security": {"shared_cache": "off"}}
    assert is_enabled(config, "shared_cache") is False


def test_is_locked_off():
    config = {"security": {"user_mounts": "off"}}
    assert is_locked_off(config, "user_mounts") is True


def test_is_locked_off_auto_is_not_locked():
    config = {}
    # auto -> off settings are NOT locked off
    assert is_locked_off(config, "relay") is False
    assert is_locked_off(config, "claude_credentials") is False


def test_is_locked_off_on_is_not_locked():
    config = {"security": {"relay": "on"}}
    assert is_locked_off(config, "relay") is False


# --- Warning tests ---


def test_print_warnings_all_auto(capsys):
    config = {}
    print_warnings(config)
    captured = capsys.readouterr()
    # All settings are auto, so all should produce warnings
    for name in SETTINGS:
        assert f"{name}=auto" in captured.err


def test_print_warnings_none_when_all_explicit(capsys):
    config = {"security": {name: defn.auto_default for name, defn in SETTINGS.items()}}
    print_warnings(config)
    captured = capsys.readouterr()
    assert captured.err == ""


def test_print_warnings_suppressed_by_env(capsys, monkeypatch):
    monkeypatch.setenv("BUBBLE_QUIET_SECURITY", "1")
    config = {}
    print_warnings(config)
    captured = capsys.readouterr()
    assert captured.err == ""


def test_print_warnings_on_defaults_suggest_lock(capsys):
    config = {}
    print_warnings(config)
    captured = capsys.readouterr()
    # on-by-default settings should suggest "Lock: ..."
    assert "Lock: bubble config set security.shared_cache off" in captured.err


def test_print_warnings_off_defaults_suggest_enable(capsys):
    config = {}
    print_warnings(config)
    captured = capsys.readouterr()
    # off-by-default settings should suggest "Enable: ..."
    assert "Enable: bubble config set security.relay on" in captured.err


# --- GitHub domain filtering ---


def test_filter_github_domains():
    domains = [
        "github.com",
        "raw.githubusercontent.com",
        "example.com",
        "objects.githubusercontent.com",
    ]
    filtered = filter_github_domains(domains)
    assert "example.com" in filtered
    assert "github.com" not in filtered
    assert "raw.githubusercontent.com" not in filtered
    assert "objects.githubusercontent.com" not in filtered


def test_filter_github_domains_empty():
    assert filter_github_domains([]) == []


def test_filter_github_domains_no_github():
    domains = ["example.com", "api.anthropic.com"]
    assert filter_github_domains(domains) == domains


# --- CLI tests ---


def test_config_security_cli(tmp_data_dir):
    from bubble.cli import main

    runner = CliRunner()
    result = runner.invoke(main, ["config", "security"])
    assert result.exit_code == 0
    assert "SETTING" in result.output
    assert "shared_cache" in result.output
    assert "relay" in result.output


def test_config_set_cli(tmp_data_dir):
    from bubble.cli import main

    runner = CliRunner()
    result = runner.invoke(main, ["config", "set", "security.shared_cache", "off"])
    assert result.exit_code == 0
    assert "Set security.shared_cache = off" in result.output

    # Verify saved
    from bubble.config import load_config

    config = load_config()
    assert config["security"]["shared_cache"] == "off"


def test_config_set_bare_name(tmp_data_dir):
    """Accepts bare name without security. prefix."""
    from bubble.cli import main

    runner = CliRunner()
    result = runner.invoke(main, ["config", "set", "relay", "on"])
    assert result.exit_code == 0
    assert "Set security.relay = on" in result.output


def test_config_set_unknown(tmp_data_dir):
    from bubble.cli import main

    runner = CliRunner()
    result = runner.invoke(main, ["config", "set", "bogus", "on"])
    assert result.exit_code != 0
    assert "Unknown security setting" in result.output


def test_config_set_invalid_value(tmp_data_dir):
    from bubble.cli import main

    runner = CliRunner()
    result = runner.invoke(main, ["config", "set", "relay", "maybe"])
    assert result.exit_code != 0


def test_config_set_relay_syncs_old_config(tmp_data_dir):
    """Setting security.relay also updates [relay] enabled for backwards compat."""
    from bubble.cli import main

    runner = CliRunner()
    result = runner.invoke(main, ["config", "set", "relay", "on"])
    assert result.exit_code == 0

    from bubble.config import load_config

    config = load_config()
    assert config["security"]["relay"] == "on"
    assert config["relay"]["enabled"] is True


def test_config_lockdown(tmp_data_dir):
    from bubble.cli import main

    runner = CliRunner()
    result = runner.invoke(main, ["config", "lockdown"])
    assert result.exit_code == 0

    from bubble.config import load_config

    config = load_config()
    # relay and claude_credentials default to off, so lockdown should set them
    assert config["security"]["relay"] == "off"
    assert config["security"]["claude_credentials"] == "off"
    # on-by-default should NOT be changed
    assert config["security"].get("shared_cache") is None


def test_config_accept_risks(tmp_data_dir):
    from bubble.cli import main

    runner = CliRunner()
    result = runner.invoke(main, ["config", "accept-risks"])
    assert result.exit_code == 0

    from bubble.config import load_config

    config = load_config()
    # on-by-default should be set to on
    assert config["security"]["shared_cache"] == "on"
    assert config["security"]["network_github"] == "on"
    # off-by-default should NOT be changed
    assert config["security"].get("relay") is None


def test_config_accept_risks_idempotent(tmp_data_dir):
    """Running accept-risks twice doesn't fail."""
    from bubble.cli import main

    runner = CliRunner()
    runner.invoke(main, ["config", "accept-risks"])
    result = runner.invoke(main, ["config", "accept-risks"])
    assert result.exit_code == 0
    assert "No auto-defaulting-to-on" in result.output


# --- SSH config tests ---


def test_ssh_config_with_host_key_trust(tmp_ssh_dir):
    from bubble.vscode import SSH_CONFIG_FILE, add_ssh_config

    add_ssh_config("test-bubble", host_key_trust=True)
    content = SSH_CONFIG_FILE.read_text()
    assert "StrictHostKeyChecking no" in content
    assert "UserKnownHostsFile /dev/null" in content


def test_ssh_config_without_host_key_trust(tmp_ssh_dir):
    from bubble.vscode import SSH_CONFIG_FILE, add_ssh_config

    add_ssh_config("test-bubble", host_key_trust=False)
    content = SSH_CONFIG_FILE.read_text()
    assert "StrictHostKeyChecking" not in content
    assert "UserKnownHostsFile" not in content
    # But the rest should be there
    assert "Host bubble-test-bubble" in content
    assert "ProxyCommand" in content
    assert "LogLevel ERROR" in content
