"""Tests for the configurable security posture system."""

from click.testing import CliRunner

from bubble.security import (
    CATEGORIES,
    SETTINGS,
    apply_preset_default,
    apply_preset_lockdown,
    apply_preset_permissive,
    display_setting_name,
    filter_github_domains,
    get_setting,
    has_auto_settings,
    is_enabled,
    is_locked_off,
    normalize_setting_name,
    print_warnings,
)

# --- Name normalization tests ---


def test_normalize_setting_name_hyphens():
    assert normalize_setting_name("github-auth") == "github_auth"
    assert normalize_setting_name("claude-credentials") == "claude_credentials"
    assert normalize_setting_name("host-key-trust") == "host_key_trust"


def test_normalize_setting_name_underscores_unchanged():
    assert normalize_setting_name("github_auth") == "github_auth"
    assert normalize_setting_name("relay") == "relay"


def test_display_setting_name():
    assert display_setting_name("github_auth") == "github-auth"
    assert display_setting_name("claude_credentials") == "claude-credentials"
    assert display_setting_name("relay") == "relay"


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


def test_get_setting_github_auth_backwards_compat_on():
    """When github_auth is auto but [github] token = true, treat as on."""
    config = {"github": {"token": True}}
    assert get_setting(config, "github_auth") == "on"


def test_get_setting_github_auth_backwards_compat_off():
    """When github_auth is auto but [github] token = false, treat as off."""
    config = {"github": {"token": False}}
    assert get_setting(config, "github_auth") == "off"


def test_get_setting_github_auth_explicit_overrides_backwards_compat():
    """Explicit security.github_auth takes precedence over [github] token."""
    config = {"security": {"github_auth": "on"}, "github": {"token": False}}
    assert get_setting(config, "github_auth") == "on"


def test_get_setting_unknown_raises():
    import pytest

    with pytest.raises(ValueError, match="Unknown security setting"):
        get_setting({}, "nonexistent")


def test_is_enabled_auto_on():
    config = {}
    assert is_enabled(config, "shared_cache") is True  # auto_default = on
    assert is_enabled(config, "network_github") is True
    assert is_enabled(config, "host_key_trust") is True
    assert is_enabled(config, "git_manifest_trust") is True
    assert is_enabled(config, "user_mounts") is True
    assert is_enabled(config, "github_auth") is True
    assert is_enabled(config, "relay") is True


def test_is_enabled_auto_on_credentials():
    config = {}
    assert is_enabled(config, "claude_credentials") is True


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
    # auto-default settings are NOT locked off (regardless of on/off default)
    assert is_locked_off(config, "relay") is False
    assert is_locked_off(config, "claude_credentials") is False


def test_is_locked_off_on_is_not_locked():
    config = {"security": {"relay": "on"}}
    assert is_locked_off(config, "relay") is False


# --- Warning tests ---


def test_print_warnings_all_auto_shows_single_line(capsys):
    config = {}
    print_warnings(config)
    captured = capsys.readouterr()
    # Should show a single summary line, not per-setting warnings
    assert "bubble security" in captured.err
    # Should NOT show per-setting detail
    for name in SETTINGS:
        assert f"{name}=auto" not in captured.err


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


def test_print_warnings_partial_auto(capsys):
    """Even one auto setting should show the summary."""
    config = {"security": {name: defn.auto_default for name, defn in SETTINGS.items()}}
    # Set one back to auto
    del config["security"]["relay"]
    print_warnings(config)
    captured = capsys.readouterr()
    assert "bubble security" in captured.err


def test_has_auto_settings_all_auto():
    assert has_auto_settings({}) is True


def test_has_auto_settings_none_auto():
    config = {"security": {name: defn.auto_default for name, defn in SETTINGS.items()}}
    assert has_auto_settings(config) is False


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


def test_security_cli_shows_posture(tmp_data_dir):
    from bubble.cli import main

    runner = CliRunner()
    result = runner.invoke(main, ["security"])
    assert result.exit_code == 0
    assert "Quick presets" in result.output
    assert "bubble security permissive" in result.output
    assert "bubble security lockdown" in result.output
    # Display should use hyphenated forms
    assert "shared-cache" in result.output
    assert "relay" in result.output
    # No underscore setting names should appear in display output
    for name in SETTINGS:
        if "_" in name:
            assert name not in result.output, f"Underscore name '{name}' leaked into display"


def test_security_cli_shows_categories(tmp_data_dir):
    from bubble.cli import main

    runner = CliRunner()
    result = runner.invoke(main, ["security"])
    assert result.exit_code == 0
    for cat_name, _ in CATEGORIES:
        assert cat_name in result.output


def test_security_permissive_cli(tmp_data_dir):
    from bubble.cli import main

    runner = CliRunner()
    result = runner.invoke(main, ["security", "permissive"])
    assert result.exit_code == 0

    from bubble.config import load_config

    config = load_config()
    for name in SETTINGS:
        assert config["security"][name] == "on"


def test_security_lockdown_cli(tmp_data_dir):
    from bubble.cli import main

    runner = CliRunner()
    result = runner.invoke(main, ["security", "lockdown"])
    assert result.exit_code == 0

    from bubble.config import load_config

    config = load_config()
    for name in SETTINGS:
        assert config["security"][name] == "off"


def test_security_default_cli(tmp_data_dir):
    from bubble.cli import main

    runner = CliRunner()
    # First set everything to on
    runner.invoke(main, ["security", "permissive"])
    # Then reset
    result = runner.invoke(main, ["security", "default"])
    assert result.exit_code == 0

    from bubble.config import load_config

    config = load_config()
    # All should be cleared (auto)
    for name in SETTINGS:
        assert config.get("security", {}).get(name) is None


def test_security_set_cli(tmp_data_dir):
    from bubble.cli import main

    runner = CliRunner()
    result = runner.invoke(main, ["security", "set", "shared_cache", "off"])
    assert result.exit_code == 0
    assert "Set security.shared-cache = off" in result.output


def test_security_set_cli_hyphenated(tmp_data_dir):
    """Hyphenated input is accepted and normalized to underscores internally."""
    from bubble.cli import main

    runner = CliRunner()
    result = runner.invoke(main, ["security", "set", "shared-cache", "on"])
    assert result.exit_code == 0
    assert "Set security.shared-cache = on" in result.output

    from bubble.config import load_config

    config = load_config()
    # Stored internally with underscores
    assert config["security"]["shared_cache"] == "on"


def test_security_set_cli_hyphenated_compound(tmp_data_dir):
    """Multi-word hyphenated names like github-auth work."""
    from bubble.cli import main

    runner = CliRunner()
    result = runner.invoke(main, ["security", "set", "github-auth", "off"])
    assert result.exit_code == 0
    assert "Set security.github-auth = off" in result.output

    from bubble.config import load_config

    config = load_config()
    assert config["security"]["github_auth"] == "off"


def test_security_set_cli_prefixed_hyphenated(tmp_data_dir):
    """security.github-auth (prefix + hyphen) works in security set."""
    from bubble.cli import main

    runner = CliRunner()
    result = runner.invoke(main, ["security", "set", "security.github-auth", "on"])
    assert result.exit_code == 0
    assert "Set security.github-auth = on" in result.output

    from bubble.config import load_config

    config = load_config()
    assert config["security"]["github_auth"] == "on"


def test_security_set_unknown(tmp_data_dir):
    from bubble.cli import main

    runner = CliRunner()
    result = runner.invoke(main, ["security", "set", "bogus", "on"])
    assert result.exit_code != 0
    assert "Unknown security setting" in result.output


def test_security_set_relay_syncs_old_config(tmp_data_dir):
    """Setting security.relay also updates [relay] enabled for backwards compat."""
    from bubble.cli import main

    runner = CliRunner()
    result = runner.invoke(main, ["security", "set", "relay", "on"])
    assert result.exit_code == 0

    from bubble.config import load_config

    config = load_config()
    assert config["security"]["relay"] == "on"
    assert config["relay"]["enabled"] is True


# --- Legacy config commands still work ---


def test_config_help_hides_deprecated_commands(tmp_data_dir):
    """Deprecated commands should not appear in `bubble config --help`."""
    from bubble.cli import main

    runner = CliRunner()
    result = runner.invoke(main, ["config", "--help"])
    assert result.exit_code == 0
    # 'set' and 'symlink-claude-projects' should still be visible
    assert "set" in result.output
    # Deprecated commands should be hidden
    assert "lockdown" not in result.output
    assert "accept-risks" not in result.output
    # 'security' as a subcommand of config should also be hidden.
    # Extract just the command names (first word of each indented line in command sections).
    lines = result.output.splitlines()
    command_names = []
    in_commands = False
    for line in lines:
        stripped = line.strip()
        if stripped.endswith(":") and line.startswith(" ") is False:
            in_commands = True
            continue
        if in_commands and stripped:
            command_names.append(stripped.split()[0])
        elif in_commands and not stripped:
            in_commands = False
    assert "security" not in command_names


def test_config_security_cli_deprecated(tmp_data_dir):
    from bubble.cli import main

    runner = CliRunner()
    result = runner.invoke(main, ["config", "security"])
    assert result.exit_code == 0
    assert "deprecated" in result.output
    assert "bubble security" in result.output
    assert "shared-cache" in result.output


def test_config_set_cli(tmp_data_dir):
    from bubble.cli import main

    runner = CliRunner()
    result = runner.invoke(main, ["config", "set", "security.shared_cache", "off"])
    assert result.exit_code == 0
    assert "Set security.shared-cache = off" in result.output

    from bubble.config import load_config

    config = load_config()
    assert config["security"]["shared_cache"] == "off"


def test_config_set_cli_hyphenated(tmp_data_dir):
    """Accepts hyphenated names in config set."""
    from bubble.cli import main

    runner = CliRunner()
    result = runner.invoke(main, ["config", "set", "security.shared-cache", "off"])
    assert result.exit_code == 0
    assert "Set security.shared-cache = off" in result.output

    from bubble.config import load_config

    config = load_config()
    assert config["security"]["shared_cache"] == "off"


def test_config_set_cli_prefixed_hyphenated_compound(tmp_data_dir):
    """security.github-auth (prefix + compound hyphen) works in config set."""
    from bubble.cli import main

    runner = CliRunner()
    result = runner.invoke(main, ["config", "set", "security.github-auth", "on"])
    assert result.exit_code == 0
    assert "Set security.github-auth = on" in result.output

    from bubble.config import load_config

    config = load_config()
    assert config["security"]["github_auth"] == "on"


def test_config_set_bare_name(tmp_data_dir):
    """Accepts bare name without security. prefix."""
    from bubble.cli import main

    runner = CliRunner()
    result = runner.invoke(main, ["config", "set", "relay", "on"])
    assert result.exit_code == 0
    assert "Set security.relay = on" in result.output


def test_config_set_bare_name_hyphenated(tmp_data_dir):
    """Accepts bare hyphenated name without security. prefix."""
    from bubble.cli import main

    runner = CliRunner()
    result = runner.invoke(main, ["config", "set", "github-auth", "on"])
    assert result.exit_code == 0
    assert "Set security.github-auth = on" in result.output

    from bubble.config import load_config

    config = load_config()
    assert config["security"]["github_auth"] == "on"


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


def test_config_lockdown(tmp_data_dir):
    from bubble.cli import main

    runner = CliRunner()
    result = runner.invoke(main, ["config", "lockdown"])
    assert result.exit_code == 0
    assert "deprecated" in result.output
    assert "bubble security lockdown" in result.output

    from bubble.config import load_config

    config = load_config()
    # on-by-default should NOT be changed by lockdown (lockdown only targets off-by-default)
    assert config["security"].get("claude_credentials") is None
    assert config["security"].get("shared_cache") is None
    assert config["security"].get("relay") is None


def test_config_accept_risks(tmp_data_dir):
    from bubble.cli import main

    runner = CliRunner()
    result = runner.invoke(main, ["config", "accept-risks"])
    assert result.exit_code == 0
    assert "deprecated" in result.output
    assert "bubble security permissive" in result.output

    from bubble.config import load_config

    config = load_config()
    # on-by-default should be set to on (includes credentials now)
    assert config["security"]["shared_cache"] == "on"
    assert config["security"]["network_github"] == "on"
    assert config["security"]["relay"] == "on"
    assert config["security"]["claude_credentials"] == "on"
    assert config["security"]["codex_credentials"] == "on"


def test_config_accept_risks_idempotent(tmp_data_dir):
    """Running accept-risks twice doesn't fail."""
    from bubble.cli import main

    runner = CliRunner()
    runner.invoke(main, ["config", "accept-risks"])
    result = runner.invoke(main, ["config", "accept-risks"])
    assert result.exit_code == 0
    assert "No auto-defaulting-to-on" in result.output


# --- Preset function tests ---


def test_apply_preset_permissive():
    config = {}
    changed = apply_preset_permissive(config)
    assert len(changed) == len(SETTINGS)
    for name in SETTINGS:
        assert config["security"][name] == "on"


def test_apply_preset_lockdown():
    config = {}
    changed = apply_preset_lockdown(config)
    assert len(changed) == len(SETTINGS)
    for name in SETTINGS:
        assert config["security"][name] == "off"


def test_apply_preset_default():
    config = {"security": {name: "on" for name in SETTINGS}}
    changed = apply_preset_default(config)
    assert len(changed) == len(SETTINGS)
    for name in SETTINGS:
        assert config["security"].get(name) is None


def test_apply_preset_default_idempotent():
    config = {}
    changed = apply_preset_default(config)
    assert len(changed) == 0


def test_apply_preset_default_clears_legacy_relay():
    """permissive then default should fully restore relay to auto."""
    config = {}
    apply_preset_permissive(config)
    # permissive sets both security.relay=on and relay.enabled=True
    assert config["security"]["relay"] == "on"
    assert config["relay"]["enabled"] is True
    apply_preset_default(config)
    # default should clear both, so relay is truly auto (on)
    assert get_setting(config, "relay") == "auto"
    assert is_enabled(config, "relay") is True


def test_all_settings_have_valid_category():
    valid_cats = {name for name, _ in CATEGORIES}
    for name, defn in SETTINGS.items():
        assert defn.category in valid_cats, f"{name} has unknown category '{defn.category}'"


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
