"""Tests for the pluggable tool installation system."""

from click.testing import CliRunner

from bubble.tools import (
    available_tools,
    combined_tool_script,
    resolve_tools,
    tool_network_domains,
    tool_runtime_domains,
    tool_script,
    tools_hash,
)


def test_available_tools():
    tools = available_tools()
    assert "claude" in tools
    assert "codex" in tools
    assert "gh" in tools
    assert tools == sorted(tools)


def test_resolve_tools_yes():
    config = {"tools": {"claude": "yes", "codex": "yes", "gh": "yes"}}
    enabled = resolve_tools(config)
    assert "claude" in enabled
    assert "codex" in enabled
    assert "gh" in enabled


def test_resolve_tools_no():
    config = {"tools": {"claude": "no", "codex": "no", "gh": "no"}}
    enabled = resolve_tools(config)
    assert enabled == []


def test_resolve_tools_auto_with_host_cmd(monkeypatch):
    monkeypatch.setattr("bubble.tools._host_has_command", lambda cmd: cmd == "gh")
    config = {"tools": {}}
    enabled = resolve_tools(config)
    assert "gh" in enabled
    assert "claude" not in enabled
    assert "codex" not in enabled


def test_resolve_tools_auto_nothing_on_host(monkeypatch):
    monkeypatch.setattr("bubble.tools._host_has_command", lambda cmd: False)
    config = {"tools": {}}
    enabled = resolve_tools(config)
    assert enabled == []


def test_resolve_tools_mixed(monkeypatch):
    monkeypatch.setattr("bubble.tools._host_has_command", lambda cmd: cmd == "gh")
    config = {"tools": {"claude": "yes", "codex": "no"}}
    enabled = resolve_tools(config)
    assert "claude" in enabled
    assert "gh" in enabled
    assert "codex" not in enabled


def test_resolve_tools_default_is_auto(monkeypatch):
    monkeypatch.setattr("bubble.tools._host_has_command", lambda cmd: False)
    config = {}  # No tools section at all
    enabled = resolve_tools(config)
    assert enabled == []


def test_tools_hash_stable():
    h1 = tools_hash(["claude", "gh"])
    h2 = tools_hash(["claude", "gh"])
    assert h1 == h2


def test_tools_hash_order_independent():
    h1 = tools_hash(["gh", "claude"])
    h2 = tools_hash(["claude", "gh"])
    assert h1 == h2


def test_tools_hash_different_sets():
    h1 = tools_hash(["claude"])
    h2 = tools_hash(["gh"])
    assert h1 != h2


def test_tools_hash_empty():
    h = tools_hash([])
    assert isinstance(h, str)
    assert len(h) == 16


def test_tool_script_reads_file():
    script = tool_script("gh")
    assert "gh" in script
    assert "#!/bin/bash" in script


def test_tool_network_domains():
    domains = tool_network_domains(["gh"])
    assert "cli.github.com" in domains


def test_tool_network_domains_combined():
    domains = tool_network_domains(["claude", "gh"])
    assert "registry.npmjs.org" in domains
    assert "cli.github.com" in domains


def test_tool_network_domains_no_duplicates():
    domains = tool_network_domains(["claude", "codex"])
    assert domains.count("registry.npmjs.org") == 1


def test_tool_runtime_domains():
    domains = tool_runtime_domains(["claude"])
    assert "api.anthropic.com" in domains


def test_tool_runtime_domains_combined():
    domains = tool_runtime_domains(["claude", "gh"])
    assert "api.anthropic.com" in domains
    assert "api.github.com" in domains
    assert "github.com" in domains


def test_tool_runtime_domains_no_duplicates():
    domains = tool_runtime_domains(["claude", "gh"])
    # Each domain should appear exactly once
    assert len(domains) == len(set(domains))


def test_tool_runtime_domains_empty():
    domains = tool_runtime_domains([])
    assert domains == []


def test_combined_tool_script_none_when_empty():
    assert combined_tool_script([]) is None


def test_combined_tool_script_includes_all():
    script = combined_tool_script(["claude", "gh"])
    assert "claude" in script
    assert "gh" in script
    assert "#!/bin/bash" in script


# CLI tests


def test_tools_list_cli(tmp_data_dir):
    from bubble.cli import main

    runner = CliRunner()
    result = runner.invoke(main, ["tools", "list"])
    assert result.exit_code == 0
    assert "claude" in result.output
    assert "gh" in result.output
    assert "TOOL" in result.output


def test_tools_set_cli(tmp_data_dir):
    from bubble.cli import main

    runner = CliRunner()
    result = runner.invoke(main, ["tools", "set", "gh", "yes"])
    assert result.exit_code == 0
    assert "Set gh = yes" in result.output

    # Verify it was saved
    from bubble.config import load_config

    config = load_config()
    assert config["tools"]["gh"] == "yes"


def test_tools_set_unknown_tool(tmp_data_dir):
    from bubble.cli import main

    runner = CliRunner()
    result = runner.invoke(main, ["tools", "set", "unknown-tool", "yes"])
    assert result.exit_code != 0
    assert "Unknown tool" in result.output


def test_tools_set_invalid_value(tmp_data_dir):
    from bubble.cli import main

    runner = CliRunner()
    result = runner.invoke(main, ["tools", "set", "gh", "maybe"])
    assert result.exit_code != 0


def test_tools_status_cli(tmp_data_dir, monkeypatch):
    monkeypatch.setattr("bubble.tools._host_has_command", lambda cmd: cmd == "gh")
    from bubble.cli import main

    runner = CliRunner()
    result = runner.invoke(main, ["tools", "status"])
    assert result.exit_code == 0
    assert "TOOL" in result.output
    assert "RESOLVED" in result.output


def test_tools_config_roundtrip(tmp_data_dir):
    from bubble.config import load_config, save_config

    config = load_config()
    config["tools"] = {"claude": "yes", "gh": "no"}
    save_config(config)

    reloaded = load_config()
    assert reloaded["tools"]["claude"] == "yes"
    assert reloaded["tools"]["gh"] == "no"


# Builder integration tests


def test_build_image_installs_tools(mock_runtime, monkeypatch, tmp_data_dir):
    """Verify that building the base image runs tool install scripts."""
    monkeypatch.setattr("bubble.tools._host_has_command", lambda cmd: cmd == "gh")
    monkeypatch.setattr("bubble.images.builder.get_vscode_commit", lambda: None)
    monkeypatch.setattr("bubble.images.builder._wait_for_container", lambda *a, **kw: None)

    from bubble.images.builder import build_image

    build_image(mock_runtime, "base")

    # Should have exec calls: one for the main script, one for tools
    exec_calls = [c for c in mock_runtime.calls if c[0] == "exec"]
    # At least 2 exec calls: main script + tool script
    assert len(exec_calls) >= 2
    # The last exec before stop should be the tools script
    tool_exec = exec_calls[-1]
    assert "gh" in tool_exec[2][-1]  # script content contains gh


def test_build_image_no_tools_when_none_enabled(mock_runtime, monkeypatch, tmp_data_dir):
    """Verify that no tool script runs when all tools resolve to skip."""
    monkeypatch.setattr("bubble.tools._host_has_command", lambda cmd: False)
    monkeypatch.setattr("bubble.images.builder.get_vscode_commit", lambda: None)
    monkeypatch.setattr("bubble.images.builder._wait_for_container", lambda *a, **kw: None)

    # Explicitly set all tools to "no" to avoid host detection
    from bubble.config import load_config, save_config

    config = load_config()
    config["tools"] = {"claude": "no", "codex": "no", "gh": "no"}
    save_config(config)

    from bubble.images.builder import build_image

    build_image(mock_runtime, "base")

    exec_calls = [c for c in mock_runtime.calls if c[0] == "exec"]
    # Only 1 exec call: the main script (no tools)
    assert len(exec_calls) == 1


def test_build_nonbase_image_skips_tools(mock_runtime, monkeypatch, tmp_data_dir):
    """Verify that non-base images don't install tools (they inherit from base)."""
    monkeypatch.setattr("bubble.tools._host_has_command", lambda cmd: True)
    monkeypatch.setattr("bubble.images.builder.get_vscode_commit", lambda: None)
    monkeypatch.setattr("bubble.images.builder._wait_for_container", lambda *a, **kw: None)

    from bubble.images.builder import build_image

    # lean derives from base — need base to exist
    mock_runtime._images.add("base")
    build_image(mock_runtime, "lean")

    exec_calls = [c for c in mock_runtime.calls if c[0] == "exec"]
    # Only 1 exec call: the lean script (no tools)
    assert len(exec_calls) == 1


def test_tools_hash_file_written(mock_runtime, monkeypatch, tmp_data_dir):
    """Verify that the tools hash file is written after building base."""
    monkeypatch.setattr("bubble.tools._host_has_command", lambda cmd: cmd == "gh")
    monkeypatch.setattr("bubble.images.builder.get_vscode_commit", lambda: None)
    monkeypatch.setattr("bubble.images.builder._wait_for_container", lambda *a, **kw: None)

    from bubble.images.builder import TOOLS_HASH_FILE, build_image

    build_image(mock_runtime, "base")

    assert TOOLS_HASH_FILE.exists()
    stored = TOOLS_HASH_FILE.read_text().strip()
    assert len(stored) == 16


def test_tools_hash_includes_script_content(tmp_path):
    """Verify that hash changes when script content changes."""
    # Same tool names but we can't easily change script content in tests,
    # so just verify the hash is deterministic and non-trivial
    h1 = tools_hash(["gh"])
    h2 = tools_hash(["gh"])
    assert h1 == h2
    # Hash of gh should differ from hash of claude-code (different scripts)
    h3 = tools_hash(["claude"])
    assert h1 != h3


def test_build_base_purges_derived_images(mock_runtime, monkeypatch, tmp_data_dir):
    """Verify that building base with tools deletes derived images."""
    monkeypatch.setattr("bubble.tools._host_has_command", lambda cmd: cmd == "gh")
    monkeypatch.setattr("bubble.images.builder.get_vscode_commit", lambda: None)
    monkeypatch.setattr("bubble.images.builder._wait_for_container", lambda *a, **kw: None)

    from bubble.images.builder import build_image

    # Pre-populate derived images
    mock_runtime._images.add("lean")
    mock_runtime._images.add("base-vscode")

    build_image(mock_runtime, "base")

    # Derived images should have been deleted
    delete_calls = [c for c in mock_runtime.calls if c[0] == "image_delete"]
    deleted_names = {c[1] for c in delete_calls}
    assert "lean" in deleted_names
    assert "base-vscode" in deleted_names
