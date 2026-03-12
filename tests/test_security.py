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
    github_domains_for_allowlist,
    has_auto_settings,
    is_enabled,
    is_locked_off,
    normalize_setting_name,
    print_warnings,
    should_include_credentials,
    valid_values_for,
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


def test_get_setting_unknown_raises():
    import pytest

    with pytest.raises(ValueError, match="Unknown security setting"):
        get_setting({}, "nonexistent")


def test_is_enabled_auto_on():
    config = {}
    assert is_enabled(config, "shared_cache") is True  # auto_default = on
    assert is_enabled(config, "host_key_trust") is True
    assert is_enabled(config, "git_manifest_trust") is True
    assert is_enabled(config, "user_mounts") is True
    assert is_enabled(config, "github_auth") is True
    assert is_enabled(config, "relay") is True


def test_is_enabled_auto_off():
    """github_token_inject defaults to off (auto_default=off)."""
    config = {}
    assert is_enabled(config, "github_token_inject") is False


def test_is_enabled_github_token_inject_explicit_on():
    config = {"security": {"github_token_inject": "on"}}
    assert is_enabled(config, "github_token_inject") is True


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


def test_github_domains_for_allowlist():
    domains = [
        "github.com",
        "api.github.com",
        "raw.githubusercontent.com",
        "example.com",
        "objects.githubusercontent.com",
        "cli.github.com",
    ]
    gh = github_domains_for_allowlist(domains)
    assert "github.com" in gh
    assert "api.github.com" in gh
    assert "raw.githubusercontent.com" in gh
    assert "objects.githubusercontent.com" in gh
    assert "cli.github.com" in gh
    assert "example.com" not in gh


def test_github_domains_for_allowlist_case_insensitive():
    """Mixed-case domains should still be detected as GitHub domains."""
    domains = ["GitHub.com", "API.GITHUB.COM", "Raw.GitHubusercontent.com", "example.com"]
    gh = github_domains_for_allowlist(domains)
    assert "GitHub.com" in gh
    assert "API.GITHUB.COM" in gh
    assert "Raw.GitHubusercontent.com" in gh
    assert "example.com" not in gh


def test_github_domains_for_allowlist_empty():
    assert github_domains_for_allowlist([]) == []


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
    # Presets should appear after the settings, not before (#151)
    assert result.output.index("Filesystem") < result.output.index("Quick presets")
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
    # Only categories with settings should appear (Network is empty now)
    for cat_name, _ in CATEGORIES:
        cat_settings = [n for n, d in SETTINGS.items() if d.category == cat_name]
        if cat_settings:
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


def test_apply_preset_default_restores_relay():
    """permissive then default should fully restore relay to auto."""
    config = {}
    apply_preset_permissive(config)
    assert config["security"]["relay"] == "on"
    apply_preset_default(config)
    assert get_setting(config, "relay") == "auto"
    assert is_enabled(config, "relay") is True


def test_all_settings_have_valid_category():
    valid_cats = {name for name, _ in CATEGORIES}
    for name, defn in SETTINGS.items():
        assert defn.category in valid_cats, f"{name} has unknown category '{defn.category}'"


# --- github_api read-write tests ---


def test_valid_values_for_normal_setting():
    """Normal settings accept only auto/on/off."""
    assert valid_values_for("relay") == ("auto", "on", "off")


def test_valid_values_for_shared_cache():
    """shared_cache also accepts overlay."""
    vals = valid_values_for("shared_cache")
    assert "auto" in vals
    assert "on" in vals
    assert "off" in vals
    assert "overlay" in vals


def test_valid_values_for_github_api():
    """github_api also accepts read-write."""
    vals = valid_values_for("github_api")
    assert "auto" in vals
    assert "on" in vals
    assert "off" in vals
    assert "read-write" in vals


def test_get_setting_github_api_read_write():
    config = {"security": {"github_api": "read-write"}}
    assert get_setting(config, "github_api") == "read-write"


def test_is_enabled_github_api_read_write():
    """read-write counts as enabled."""
    config = {"security": {"github_api": "read-write"}}
    assert is_enabled(config, "github_api") is True


def test_is_locked_off_github_api_read_write():
    """read-write is not locked off."""
    config = {"security": {"github_api": "read-write"}}
    assert is_locked_off(config, "github_api") is False


def test_has_auto_settings_with_read_write():
    """read-write is an explicit value, not auto."""
    config = {"security": {name: "on" for name in SETTINGS}}
    config["security"]["github_api"] = "read-write"
    assert has_auto_settings(config) is False


def test_resolve_access_level_read_write():
    """read-write config returns LEVEL_GH_READWRITE (4)."""
    from bubble.auth_proxy import LEVEL_GH_READWRITE
    from bubble.github_token import _resolve_access_level

    config = {"security": {"github_api": "read-write"}}
    assert _resolve_access_level(config, gh_enabled=True) == LEVEL_GH_READWRITE


def test_resolve_access_level_on_returns_default():
    """on config returns LEVEL_GH_READ (3)."""
    from bubble.auth_proxy import LEVEL_GH_READ
    from bubble.github_token import _resolve_access_level

    config = {"security": {"github_api": "on"}}
    assert _resolve_access_level(config, gh_enabled=True) == LEVEL_GH_READ


def test_resolve_access_level_off_returns_git_only():
    """off config returns LEVEL_GIT_ONLY (1)."""
    from bubble.auth_proxy import LEVEL_GIT_ONLY
    from bubble.github_token import _resolve_access_level

    config = {"security": {"github_api": "off"}}
    assert _resolve_access_level(config, gh_enabled=True) == LEVEL_GIT_ONLY


def test_resolve_access_level_gh_disabled():
    """gh not enabled returns LEVEL_GIT_ONLY regardless of config."""
    from bubble.auth_proxy import LEVEL_GIT_ONLY
    from bubble.github_token import _resolve_access_level

    config = {"security": {"github_api": "read-write"}}
    assert _resolve_access_level(config, gh_enabled=False) == LEVEL_GIT_ONLY


def test_security_set_github_api_read_write(tmp_data_dir):
    from bubble.cli import main

    runner = CliRunner()
    result = runner.invoke(main, ["security", "set", "github-api", "read-write"])
    assert result.exit_code == 0
    assert "Set security.github-api = read-write" in result.output

    from bubble.config import load_config

    config = load_config()
    assert config["security"]["github_api"] == "read-write"


def test_security_set_read_write_rejected_for_other_settings(tmp_data_dir):
    """read-write is only valid for github_api, not other settings."""
    from bubble.cli import main

    runner = CliRunner()
    result = runner.invoke(main, ["security", "set", "relay", "read-write"])
    assert result.exit_code != 0
    assert "Invalid value" in result.output


def test_config_set_github_api_read_write(tmp_data_dir):
    """config set also accepts read-write for github-api."""
    from bubble.cli import main

    runner = CliRunner()
    result = runner.invoke(main, ["config", "set", "github-api", "read-write"])
    assert result.exit_code == 0
    assert "Set security.github-api = read-write" in result.output

    from bubble.config import load_config

    config = load_config()
    assert config["security"]["github_api"] == "read-write"


def test_security_posture_shows_read_write(tmp_data_dir, capsys):
    """security posture display shows read-write hint for github-api."""
    from bubble.security import print_security_posture

    config = {"security": {"github_api": "read-write"}}
    print_security_posture(config)
    captured = capsys.readouterr()
    assert "read-write" in captured.out
    assert "mutations" in captured.out


def test_invalid_raw_config_falls_back_to_auto():
    """Typos in hand-edited config.toml are treated as auto (fail-closed)."""
    config = {"security": {"github_api": "readwrtie"}}
    assert get_setting(config, "github_api") == "auto"
    # auto_default is "on", so it's enabled but at the default level (not read-write)
    assert is_enabled(config, "github_api") is True

    from bubble.auth_proxy import LEVEL_GH_READ
    from bubble.github_token import _resolve_access_level

    assert _resolve_access_level(config, gh_enabled=True) == LEVEL_GH_READ


def test_invalid_raw_config_other_setting():
    """Typos in any setting fall back to auto."""
    config = {"security": {"relay": "yse"}}
    assert get_setting(config, "relay") == "auto"


def test_permissive_preserves_read_write():
    """permissive does not downgrade github_api from read-write to on."""
    config = {"security": {"github_api": "read-write"}}
    changed = apply_preset_permissive(config)
    # github_api should NOT be in the changed list
    assert "github_api" not in changed
    # Value should still be read-write
    assert config["security"]["github_api"] == "read-write"


def test_permissive_still_sets_non_explicit():
    """permissive sets auto settings to on even when github_api is read-write."""
    config = {"security": {"github_api": "read-write"}}
    changed = apply_preset_permissive(config)
    # Other settings should be changed to "on"
    assert "relay" in changed
    assert config["security"]["relay"] == "on"


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


# --- should_include_credentials tests ---


def test_should_include_credentials_locked_off_overrides_true():
    config = {"security": {"claude_credentials": "off"}}
    assert should_include_credentials(True, config, "claude_credentials") is False


def test_should_include_credentials_locked_off_overrides_false():
    config = {"security": {"claude_credentials": "off"}}
    assert should_include_credentials(False, config, "claude_credentials") is False


def test_should_include_credentials_requested_true():
    config = {"security": {"claude_credentials": "on"}}
    assert should_include_credentials(True, config, "claude_credentials") is True


def test_should_include_credentials_requested_false_security_on():
    """Security 'on' enables credentials even when the resolved flag is False."""
    config = {"security": {"claude_credentials": "on"}}
    assert should_include_credentials(False, config, "claude_credentials") is True


def test_should_include_credentials_requested_false_security_auto():
    """Auto with auto_default=on behaves like 'on'."""
    config = {}  # auto (default)
    assert should_include_credentials(False, config, "claude_credentials") is True


def test_should_include_credentials_requested_true_security_auto():
    config = {}
    assert should_include_credentials(True, config, "claude_credentials") is True


# --- shared_cache overlay tests ---


def test_get_setting_shared_cache_overlay():
    config = {"security": {"shared_cache": "overlay"}}
    assert get_setting(config, "shared_cache") == "overlay"


def test_is_enabled_shared_cache_overlay():
    """overlay counts as enabled (shared mounts are still used)."""
    config = {"security": {"shared_cache": "overlay"}}
    assert is_enabled(config, "shared_cache") is True


def test_is_locked_off_shared_cache_overlay():
    """overlay is not locked off."""
    config = {"security": {"shared_cache": "overlay"}}
    assert is_locked_off(config, "shared_cache") is False


def test_security_set_shared_cache_overlay(tmp_data_dir):
    from bubble.cli import main

    runner = CliRunner()
    result = runner.invoke(main, ["security", "set", "shared-cache", "overlay"])
    assert result.exit_code == 0
    assert "Set security.shared-cache = overlay" in result.output

    from bubble.config import load_config

    config = load_config()
    assert config["security"]["shared_cache"] == "overlay"


def test_security_set_overlay_rejected_for_other_settings(tmp_data_dir):
    """overlay is only valid for shared_cache, not other settings."""
    from bubble.cli import main

    runner = CliRunner()
    result = runner.invoke(main, ["security", "set", "relay", "overlay"])
    assert result.exit_code != 0
    assert "Invalid value" in result.output


def test_security_posture_shows_overlay(tmp_data_dir, capsys):
    """security posture display shows overlay description for shared-cache."""
    from bubble.security import print_security_posture

    config = {"security": {"shared_cache": "overlay"}}
    print_security_posture(config)
    captured = capsys.readouterr()
    assert "overlay" in captured.out
    assert "per-container writable overlay" in captured.out


def test_permissive_preserves_overlay():
    """permissive does not downgrade shared_cache from overlay to on."""
    config = {"security": {"shared_cache": "overlay"}}
    changed = apply_preset_permissive(config)
    assert "shared_cache" not in changed
    assert config["security"]["shared_cache"] == "overlay"
