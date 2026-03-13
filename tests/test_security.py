"""Tests for the configurable security posture system."""

from click.testing import CliRunner

from bubble.security import (
    CATEGORIES,
    GITHUB_AUTO_DEFAULT,
    GITHUB_LEVELS,
    SETTINGS,
    apply_preset_default,
    apply_preset_lockdown,
    apply_preset_permissive,
    display_setting_name,
    filter_github_domains,
    get_github_level,
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
    assert normalize_setting_name("claude-credentials") == "claude_credentials"
    assert normalize_setting_name("host-key-trust") == "host_key_trust"


def test_normalize_setting_name_underscores_unchanged():
    assert normalize_setting_name("github") == "github"
    assert normalize_setting_name("relay") == "relay"


def test_display_setting_name():
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
    assert is_enabled(config, "github") is True
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


def test_is_enabled_github_levels():
    """All non-off github levels count as enabled."""
    for level in GITHUB_LEVELS:
        config = {"security": {"github": level}}
        if level == "off":
            assert is_enabled(config, "github") is False
        else:
            assert is_enabled(config, "github") is True, f"level {level} should be enabled"


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


# --- GitHub level tests ---


def test_get_github_level_auto_default():
    """auto resolves to allowlist-write-graphql."""
    config = {}
    assert get_github_level(config) == "allowlist-write-graphql"


def test_get_github_level_explicit_levels():
    """Each explicit level returns itself."""
    for level in GITHUB_LEVELS:
        config = {"security": {"github": level}}
        assert get_github_level(config) == level


def test_get_github_level_on_treated_as_default():
    """'on' is treated as the auto default."""
    config = {"security": {"github": "on"}}
    assert get_github_level(config) == GITHUB_AUTO_DEFAULT


def test_get_github_level_typo_falls_back():
    """Typos in the github setting fall back to auto (the default level)."""
    config = {"security": {"github": "readwrtie"}}
    assert get_github_level(config) == GITHUB_AUTO_DEFAULT


# --- Legacy migration tests ---


def test_get_github_level_migration_auth_off():
    """Old github_auth=off maps to off."""
    config = {"security": {"github_auth": "off"}}
    assert get_github_level(config) == "off"


def test_get_github_level_migration_inject_on():
    """Old github_token_inject=on maps to direct."""
    config = {"security": {"github_token_inject": "on"}}
    assert get_github_level(config) == "direct"


def test_get_github_level_migration_inject_overrides_auth_off():
    """Token injection wins even when github_auth=off (matches old runtime behavior)."""
    config = {"security": {"github_auth": "off", "github_token_inject": "on"}}
    assert get_github_level(config) == "direct"


def test_get_github_level_migration_api_off():
    """Old github_api=off (with auth on) maps to basic."""
    config = {"security": {"github_api": "off"}}
    assert get_github_level(config) == "basic"


def test_get_github_level_migration_api_read_write():
    """Old github_api=read-write maps to write-graphql."""
    config = {"security": {"github_api": "read-write"}}
    assert get_github_level(config) == "write-graphql"


def test_get_github_level_migration_default():
    """Old defaults (all auto) map to allowlist-write-graphql."""
    config = {"security": {"github_auth": "auto"}}
    assert get_github_level(config) == "allowlist-write-graphql"


def test_get_github_level_new_overrides_legacy():
    """New 'github' key takes precedence over legacy keys."""
    config = {"security": {"github": "basic", "github_auth": "off"}}
    assert get_github_level(config) == "basic"


def test_warn_legacy_github_settings(capsys):
    """Legacy keys trigger a deprecation warning."""
    from bubble.security import warn_legacy_github_settings

    config = {"security": {"github_auth": "on", "github_api": "off"}}
    warn_legacy_github_settings(config)
    captured = capsys.readouterr()
    assert "Deprecated" in captured.err
    assert "github = basic" in captured.err


def test_warn_legacy_no_warning_for_new_config(capsys):
    """No warning when only new-style github key is present."""
    from bubble.security import warn_legacy_github_settings

    config = {"security": {"github": "rest"}}
    warn_legacy_github_settings(config)
    captured = capsys.readouterr()
    assert captured.err == ""


def test_warn_legacy_suppressed_when_new_key_set(capsys):
    """No warning when both legacy and new keys are present (partially migrated)."""
    from bubble.security import warn_legacy_github_settings

    config = {"security": {"github": "rest", "github_auth": "on"}}
    warn_legacy_github_settings(config)
    captured = capsys.readouterr()
    assert captured.err == ""


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
    # The github setting should show level descriptions
    assert "Levels" in result.output
    assert "allowlist-write-graphql" in result.output
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
        if name == "github":
            assert config["security"][name] == "direct"
        else:
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
    # First set everything
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


def test_security_set_github_level(tmp_data_dir):
    """Setting github to a specific level works."""
    from bubble.cli import main

    runner = CliRunner()
    result = runner.invoke(main, ["security", "set", "github", "basic"])
    assert result.exit_code == 0
    assert "Set security.github = basic" in result.output

    from bubble.config import load_config

    config = load_config()
    assert config["security"]["github"] == "basic"


def test_security_set_github_all_levels(tmp_data_dir):
    """All github levels are accepted."""
    from bubble.cli import main

    runner = CliRunner()
    for level in GITHUB_LEVELS:
        result = runner.invoke(main, ["security", "set", "github", level])
        assert result.exit_code == 0, f"Failed to set github to {level}: {result.output}"


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


def test_config_set_github_level(tmp_data_dir):
    """config set also accepts github levels."""
    from bubble.cli import main

    runner = CliRunner()
    result = runner.invoke(main, ["config", "set", "github", "rest"])
    assert result.exit_code == 0
    assert "Set security.github = rest" in result.output

    from bubble.config import load_config

    config = load_config()
    assert config["security"]["github"] == "rest"


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


# --- Preset function tests ---


def test_apply_preset_permissive():
    config = {}
    changed = apply_preset_permissive(config)
    assert len(changed) == len(SETTINGS)
    for name in SETTINGS:
        if name == "github":
            assert config["security"][name] == "direct"
        else:
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


def test_apply_preset_default_cleans_legacy_keys():
    """default preset removes old github_auth/github_api/github_token_inject keys."""
    config = {"security": {"github_auth": "on", "github_api": "read-write"}}
    apply_preset_default(config)
    assert "github_auth" not in config["security"]
    assert "github_api" not in config["security"]


def test_all_settings_have_valid_category():
    valid_cats = {name for name, _ in CATEGORIES}
    for name, defn in SETTINGS.items():
        assert defn.category in valid_cats, f"{name} has unknown category '{defn.category}'"


# --- github level with access level/graphql resolution ---


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


def test_valid_values_for_github():
    """github accepts all graduated levels."""
    vals = valid_values_for("github")
    assert "auto" in vals
    assert "on" in vals
    assert "off" in vals
    for level in GITHUB_LEVELS:
        assert level in vals


def test_resolve_access_level_allowlist_write_graphql():
    """allowlist-write-graphql returns LEVEL_GH_READWRITE."""
    from bubble.auth_proxy import LEVEL_GH_READWRITE
    from bubble.github_token import _resolve_access_level

    config = {"security": {"github": "allowlist-write-graphql"}}
    assert _resolve_access_level(config, gh_enabled=True) == LEVEL_GH_READWRITE


def test_resolve_access_level_basic():
    """basic returns LEVEL_GIT_ONLY."""
    from bubble.auth_proxy import LEVEL_GIT_ONLY
    from bubble.github_token import _resolve_access_level

    config = {"security": {"github": "basic"}}
    assert _resolve_access_level(config, gh_enabled=True) == LEVEL_GIT_ONLY


def test_resolve_access_level_rest():
    """rest returns LEVEL_GH_READWRITE (REST is repo-scoped, writes are safe)."""
    from bubble.auth_proxy import LEVEL_GH_READWRITE
    from bubble.github_token import _resolve_access_level

    config = {"security": {"github": "rest"}}
    assert _resolve_access_level(config, gh_enabled=True) == LEVEL_GH_READWRITE


def test_resolve_access_level_off():
    """off returns LEVEL_GIT_ONLY."""
    from bubble.auth_proxy import LEVEL_GIT_ONLY
    from bubble.github_token import _resolve_access_level

    config = {"security": {"github": "off"}}
    assert _resolve_access_level(config, gh_enabled=True) == LEVEL_GIT_ONLY


def test_resolve_access_level_gh_disabled():
    """gh not enabled returns LEVEL_GIT_ONLY regardless of level."""
    from bubble.auth_proxy import LEVEL_GIT_ONLY
    from bubble.github_token import _resolve_access_level

    config = {"security": {"github": "write-graphql"}}
    assert _resolve_access_level(config, gh_enabled=False) == LEVEL_GIT_ONLY


def test_resolve_graphql_config_allowlist_write_graphql():
    """allowlist-write-graphql returns whitelisted for both."""
    from bubble.github_token import _resolve_graphql_config

    config = {"security": {"github": "allowlist-write-graphql"}}
    assert _resolve_graphql_config(config, gh_enabled=True) == ("whitelisted", "whitelisted")


def test_resolve_graphql_config_allowlist_read_graphql():
    """allowlist-read-graphql returns whitelisted read, none write."""
    from bubble.github_token import _resolve_graphql_config

    config = {"security": {"github": "allowlist-read-graphql"}}
    assert _resolve_graphql_config(config, gh_enabled=True) == ("whitelisted", "none")


def test_resolve_graphql_config_write_graphql():
    """write-graphql returns unrestricted for both."""
    from bubble.github_token import _resolve_graphql_config

    config = {"security": {"github": "write-graphql"}}
    assert _resolve_graphql_config(config, gh_enabled=True) == ("unrestricted", "unrestricted")


def test_resolve_graphql_config_rest():
    """rest returns none for both (no GraphQL)."""
    from bubble.github_token import _resolve_graphql_config

    config = {"security": {"github": "rest"}}
    assert _resolve_graphql_config(config, gh_enabled=True) == ("none", "none")


def test_resolve_graphql_config_basic():
    """basic returns none for both."""
    from bubble.github_token import _resolve_graphql_config

    config = {"security": {"github": "basic"}}
    assert _resolve_graphql_config(config, gh_enabled=True) == ("none", "none")


def test_resolve_graphql_config_off():
    """off returns none for both."""
    from bubble.github_token import _resolve_graphql_config

    config = {"security": {"github": "off"}}
    assert _resolve_graphql_config(config, gh_enabled=True) == ("none", "none")


def test_resolve_graphql_config_gh_disabled():
    """gh not enabled returns none regardless of level."""
    from bubble.github_token import _resolve_graphql_config

    config = {"security": {"github": "write-graphql"}}
    assert _resolve_graphql_config(config, gh_enabled=False) == ("none", "none")


def test_security_set_github_level_via_cli(tmp_data_dir):
    """Setting github to a graduated level via CLI works."""
    from bubble.cli import main

    runner = CliRunner()
    result = runner.invoke(main, ["security", "set", "github", "write-graphql"])
    assert result.exit_code == 0
    assert "Set security.github = write-graphql" in result.output

    from bubble.config import load_config

    config = load_config()
    assert config["security"]["github"] == "write-graphql"


def test_security_set_level_rejected_for_other_settings(tmp_data_dir):
    """GitHub levels are only valid for github, not other settings."""
    from bubble.cli import main

    runner = CliRunner()
    result = runner.invoke(main, ["security", "set", "relay", "basic"])
    assert result.exit_code != 0
    assert "Invalid value" in result.output


def test_security_posture_shows_github_levels(tmp_data_dir, capsys):
    """security posture display shows the graduated levels for github."""
    from bubble.security import print_security_posture

    config = {"security": {"github": "rest"}}
    print_security_posture(config)
    captured = capsys.readouterr()
    assert "rest" in captured.out
    assert "Levels" in captured.out
    assert "direct" in captured.out  # all levels shown


def test_invalid_raw_config_falls_back_to_auto():
    """Typos in hand-edited config.toml are treated as auto (fail-closed)."""
    config = {"security": {"github": "readwrtie"}}
    assert get_setting(config, "github") == "auto"
    # auto resolves to the default level
    assert get_github_level(config) == GITHUB_AUTO_DEFAULT

    from bubble.auth_proxy import LEVEL_GH_READWRITE
    from bubble.github_token import _resolve_access_level

    assert _resolve_access_level(config, gh_enabled=True) == LEVEL_GH_READWRITE

    from bubble.github_token import _resolve_graphql_config

    assert _resolve_graphql_config(config, gh_enabled=True) == ("whitelisted", "whitelisted")


def test_invalid_raw_config_other_setting():
    """Typos in any setting fall back to auto."""
    config = {"security": {"relay": "yse"}}
    assert get_setting(config, "relay") == "auto"


def test_permissive_preserves_direct():
    """permissive does not downgrade github from direct."""
    config = {"security": {"github": "direct"}}
    changed = apply_preset_permissive(config)
    assert "github" not in changed
    assert config["security"]["github"] == "direct"


def test_permissive_sets_github_to_direct():
    """permissive sets github to direct from auto."""
    config = {}
    changed = apply_preset_permissive(config)
    assert "github" in changed
    assert config["security"]["github"] == "direct"


def test_permissive_still_sets_non_explicit():
    """permissive sets auto settings to on even when github is already set."""
    config = {"security": {"github": "direct"}}
    changed = apply_preset_permissive(config)
    # Other settings should be changed to "on"
    assert "relay" in changed
    assert config["security"]["relay"] == "on"


def test_has_auto_settings_with_github_level():
    """Explicit github level is not auto."""
    config = {"security": {name: "on" for name in SETTINGS}}
    config["security"]["github"] = "basic"
    assert has_auto_settings(config) is False


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
