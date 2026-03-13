"""Tests for the pluggable tool installation system."""

from click.testing import CliRunner

from bubble.tools import (
    available_tools,
    combined_tool_script,
    load_pins,
    resolve_tools,
    tool_runtime_domains,
    tool_script,
    tools_hash,
)


def test_available_tools():
    tools = available_tools()
    assert "claude" in tools
    assert "codex" in tools
    assert "elan" in tools
    assert "gh" in tools
    assert "vscode" in tools
    assert "emacs" in tools
    assert "neovim" in tools
    assert tools == sorted(tools)


def test_resolve_tools_yes():
    config = {"tools": {"claude": "yes", "codex": "yes"}, "editor": "shell"}
    enabled = resolve_tools(config)
    assert "claude" in enabled
    assert "codex" in enabled


def test_resolve_tools_no():
    config = {
        "tools": {"claude": "no", "codex": "no", "elan": "no", "gh": "no"},
        "editor": "shell",
    }
    enabled = resolve_tools(config)
    assert enabled == []


def test_resolve_tools_auto_with_host_cmd(monkeypatch):
    monkeypatch.setattr("bubble.tools._host_has_command", lambda cmd: cmd == "claude")
    config = {"tools": {}, "editor": "shell"}
    enabled = resolve_tools(config)
    assert "claude" in enabled
    assert "codex" not in enabled


def test_resolve_tools_auto_nothing_on_host(monkeypatch):
    monkeypatch.setattr("bubble.tools._host_has_command", lambda cmd: False)
    config = {"editor": "shell"}
    enabled = resolve_tools(config)
    assert enabled == []


def test_resolve_tools_mixed(monkeypatch):
    monkeypatch.setattr("bubble.tools._host_has_command", lambda cmd: cmd == "claude")
    config = {"tools": {"codex": "yes", "elan": "no"}, "editor": "shell"}
    enabled = resolve_tools(config)
    assert "codex" in enabled
    assert "claude" in enabled
    assert "elan" not in enabled


def test_resolve_tools_default_is_auto(monkeypatch):
    monkeypatch.setattr("bubble.tools._host_has_command", lambda cmd: False)
    config = {"editor": "shell"}
    enabled = resolve_tools(config)
    assert enabled == []


def test_resolve_tools_includes_default_editor(monkeypatch):
    """Default editor (vscode) is included even when no tools on host."""
    monkeypatch.setattr("bubble.tools._host_has_command", lambda cmd: False)
    config = {}  # defaults to editor=vscode
    enabled = resolve_tools(config)
    assert "vscode" in enabled


def test_resolve_tools_priority_ordering(monkeypatch):
    """Tools should be ordered by priority: elan (10) before claude (50) before vscode (90)."""
    monkeypatch.setattr("bubble.tools._host_has_command", lambda cmd: True)
    config = {"editor": "vscode"}
    enabled = resolve_tools(config)
    elan_idx = enabled.index("elan")
    claude_idx = enabled.index("claude")
    vscode_idx = enabled.index("vscode")
    assert elan_idx < claude_idx < vscode_idx


def test_tools_hash_stable():
    h1 = tools_hash(["claude", "codex"])
    h2 = tools_hash(["claude", "codex"])
    assert h1 == h2


def test_tools_hash_order_independent():
    h1 = tools_hash(["codex", "claude"])
    h2 = tools_hash(["claude", "codex"])
    assert h1 == h2


def test_tools_hash_different_sets():
    h1 = tools_hash(["claude"])
    h2 = tools_hash(["codex"])
    assert h1 != h2


def test_tools_hash_empty():
    h = tools_hash([])
    assert isinstance(h, str)
    assert len(h) == 16


def test_tool_script_reads_file():
    script = tool_script("claude")
    assert "claude" in script.lower()
    assert "#!/bin/bash" in script


def test_tool_runtime_domains():
    domains = tool_runtime_domains(["claude"])
    assert "api.anthropic.com" in domains


def test_tool_runtime_domains_combined():
    domains = tool_runtime_domains(["claude", "codex"])
    assert "api.anthropic.com" in domains
    assert "api.openai.com" in domains


def test_tool_runtime_domains_no_duplicates():
    domains = tool_runtime_domains(["claude", "codex"])
    # Each domain should appear exactly once
    assert len(domains) == len(set(domains))


def test_tool_runtime_domains_empty():
    domains = tool_runtime_domains([])
    assert domains == []


def test_combined_tool_script_none_when_empty():
    assert combined_tool_script([]) is None


def test_combined_tool_script_includes_all():
    script = combined_tool_script(["claude", "codex"])
    assert "claude" in script.lower()
    assert "codex" in script.lower()
    assert "#!/bin/bash" in script


# Pin tests


def test_load_pins():
    pins = load_pins()
    assert "NODE_VERSION" in pins
    assert "NODE_SHA256_X64" in pins
    assert "NODE_SHA256_ARM64" in pins
    assert "CLAUDE_CODE_VERSION" in pins
    assert "CODEX_VERSION" in pins


def test_pins_are_nonempty():
    pins = load_pins()
    for key, value in pins.items():
        assert isinstance(value, str), f"{key} should be a string"
        assert len(value) > 0, f"{key} should not be empty"


def test_tool_script_injects_pins():
    """Verify that tool scripts have pinned version variables injected."""
    script = tool_script("claude")
    assert "NODE_VERSION=" in script
    assert "CLAUDE_CODE_VERSION=" in script

    script = tool_script("codex")
    assert "CODEX_VERSION=" in script


def test_tool_script_uses_pinned_npm_versions():
    """Verify scripts install specific npm package versions, not unpinned."""
    script = tool_script("claude")
    assert "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" in script

    script = tool_script("codex")
    assert "@openai/codex@${CODEX_VERSION}" in script


def test_tool_script_verifies_node_checksum():
    """Verify Node.js install uses sha256 verification."""
    script = tool_script("claude")
    assert "sha256sum -c" in script
    assert "nodejs.org/dist" in script


def test_tools_hash_changes_with_pins(tmp_path, monkeypatch):
    """Verify that changing pins changes the tools hash."""
    h1 = tools_hash(["claude"])

    # Monkeypatch load_pins to return modified pins
    def patched_load():
        pins = dict(load_pins())
        pins["CLAUDE_CODE_VERSION"] = "0.0.0"
        return pins

    monkeypatch.setattr("bubble.tools.load_pins", patched_load)
    h2 = tools_hash(["claude"])
    assert h1 != h2


# CLI tests


def test_tools_list_cli(tmp_data_dir):
    from bubble.cli import main

    runner = CliRunner()
    result = runner.invoke(main, ["tools", "list"])
    assert result.exit_code == 0
    assert "claude" in result.output
    assert "TOOL" in result.output


def test_tools_set_cli(tmp_data_dir):
    from bubble.cli import main

    runner = CliRunner()
    result = runner.invoke(main, ["tools", "set", "claude", "yes"])
    assert result.exit_code == 0
    assert "Set claude = yes" in result.output

    # Verify it was saved
    from bubble.config import load_config

    config = load_config()
    assert config["tools"]["claude"] == "yes"


def test_tools_set_unknown_tool(tmp_data_dir):
    from bubble.cli import main

    runner = CliRunner()
    result = runner.invoke(main, ["tools", "set", "unknown-tool", "yes"])
    assert result.exit_code != 0
    assert "Unknown tool" in result.output


def test_tools_set_invalid_value(tmp_data_dir):
    from bubble.cli import main

    runner = CliRunner()
    result = runner.invoke(main, ["tools", "set", "claude", "maybe"])
    assert result.exit_code != 0


def test_tools_status_cli(tmp_data_dir, monkeypatch):
    monkeypatch.setattr("bubble.tools._host_has_command", lambda cmd: cmd == "claude")
    from bubble.cli import main

    runner = CliRunner()
    result = runner.invoke(main, ["tools", "status"])
    assert result.exit_code == 0
    assert "TOOL" in result.output
    assert "RESOLVED" in result.output


def test_tools_config_roundtrip(tmp_data_dir):
    from bubble.config import load_config, save_config

    config = load_config()
    config["tools"] = {"claude": "yes", "codex": "no"}
    save_config(config)

    reloaded = load_config()
    assert reloaded["tools"]["claude"] == "yes"
    assert reloaded["tools"]["codex"] == "no"


def test_tools_update_cli(tmp_data_dir, monkeypatch):
    """Verify the tools update command runs and shows output."""
    from bubble.cli import main

    # Mock fetch_latest_pins to avoid network access
    def mock_fetch():
        pins = dict(load_pins())
        pins["CLAUDE_CODE_VERSION"] = "99.99.99"
        return pins

    monkeypatch.setattr("bubble.tools.fetch_latest_pins", mock_fetch)
    # Prevent writing to the real pins.json
    monkeypatch.setattr("bubble.tools.save_pins", lambda pins: None)

    runner = CliRunner()
    result = runner.invoke(main, ["tools", "update"])
    assert result.exit_code == 0
    assert "CLAUDE_CODE_VERSION" in result.output
    assert "99.99.99" in result.output


def test_tools_update_no_changes(tmp_data_dir, monkeypatch):
    """Verify the tools update command handles no-changes case."""
    from bubble.cli import main

    monkeypatch.setattr("bubble.tools.fetch_latest_pins", load_pins)

    runner = CliRunner()
    result = runner.invoke(main, ["tools", "update"])
    assert result.exit_code == 0
    assert "up to date" in result.output


# Builder integration tests


def test_build_image_installs_tools(mock_runtime, monkeypatch, tmp_data_dir):
    """Verify that building the base image runs per-tool install scripts."""
    monkeypatch.setattr("bubble.tools._host_has_command", lambda cmd: cmd == "claude")
    monkeypatch.setattr("bubble.images.builder.get_vscode_commit", lambda: None)
    monkeypatch.setattr("bubble.images.builder.wait_for_container", lambda *a, **kw: None)

    from bubble.images.builder import build_image

    mock_runtime._images.discard("base")
    build_image(mock_runtime, "base")

    # Should have exec calls: base.sh + one per tool (claude, vscode)
    exec_calls = [c for c in mock_runtime.calls if c[0] == "exec"]
    assert len(exec_calls) >= 2
    # Should have a per-tool exec call containing claude
    tool_scripts = [c[2][-1] for c in exec_calls]
    assert any("claude" in s.lower() for s in tool_scripts)


def test_build_image_per_tool_progress(mock_runtime, monkeypatch, tmp_data_dir, capsys):
    """Verify that tool installation shows per-tool progress messages."""
    monkeypatch.setattr("bubble.tools._host_has_command", lambda cmd: cmd in ("claude", "elan"))
    monkeypatch.setattr("bubble.images.builder.get_vscode_commit", lambda: None)
    monkeypatch.setattr("bubble.images.builder.wait_for_container", lambda *a, **kw: None)

    from bubble.images.builder import build_image

    mock_runtime._images.discard("base")
    build_image(mock_runtime, "base")

    captured = capsys.readouterr()
    # elan (priority 10), claude (priority 50), vscode (priority 90)
    assert "elan (1/3)" in captured.out
    assert "claude (2/3)" in captured.out
    assert "vscode (3/3)" in captured.out


def test_build_image_shows_progress_markers(mock_runtime, monkeypatch, tmp_data_dir, capsys):
    """Verify that BUBBLE_PROGRESS markers from build scripts are displayed."""
    monkeypatch.setattr("bubble.tools._host_has_command", lambda cmd: False)
    monkeypatch.setattr("bubble.images.builder.get_vscode_commit", lambda: None)
    monkeypatch.setattr("bubble.images.builder.wait_for_container", lambda *a, **kw: None)

    from bubble.config import load_config, save_config

    config = load_config()
    config["editor"] = "shell"
    save_config(config)

    # Make exec return BUBBLE_PROGRESS lines so the streaming callback can parse them
    mock_runtime.exec_responses["bash"] = (
        "BUBBLE_PROGRESS: Installing system packages...\n"
        "lots of apt output\n"
        "BUBBLE_PROGRESS: Configuring SSH...\n"
    )

    from bubble.images.builder import build_image

    mock_runtime._images.discard("base")
    build_image(mock_runtime, "base")

    captured = capsys.readouterr()
    assert "Installing system packages..." in captured.out
    assert "Configuring SSH..." in captured.out
    # Non-progress lines should not be printed
    assert "lots of apt output" not in captured.out


def test_build_image_no_tools_when_none_enabled(mock_runtime, monkeypatch, tmp_data_dir):
    """Verify that no tool script runs when all tools resolve to skip."""
    monkeypatch.setattr("bubble.tools._host_has_command", lambda cmd: False)
    monkeypatch.setattr("bubble.images.builder.get_vscode_commit", lambda: None)
    monkeypatch.setattr("bubble.images.builder.wait_for_container", lambda *a, **kw: None)

    # Explicitly set all tools to "no" and editor to "shell" to skip everything
    from bubble.config import load_config, save_config

    config = load_config()
    config["tools"] = {"claude": "no", "codex": "no", "elan": "no"}
    config["editor"] = "shell"
    save_config(config)

    from bubble.images.builder import build_image

    mock_runtime._images.discard("base")
    build_image(mock_runtime, "base")

    exec_calls = [c for c in mock_runtime.calls if c[0] == "exec"]
    # Only 1 exec call: the main script (no tools)
    assert len(exec_calls) == 1


def test_build_nonbase_image_skips_tools(mock_runtime, monkeypatch, tmp_data_dir):
    """Verify that non-base images don't install tools (they inherit from base)."""
    monkeypatch.setattr("bubble.tools._host_has_command", lambda cmd: True)
    monkeypatch.setattr("bubble.images.builder.get_vscode_commit", lambda: None)
    monkeypatch.setattr("bubble.images.builder.wait_for_container", lambda *a, **kw: None)

    from bubble.images.builder import build_image

    # lean derives from base — need base to exist
    mock_runtime._images.add("base")
    build_image(mock_runtime, "lean")

    exec_calls = [c for c in mock_runtime.calls if c[0] == "exec"]
    # Only 1 exec call: the lean script (no tools)
    assert len(exec_calls) == 1


def test_tools_hash_file_written(mock_runtime, monkeypatch, tmp_data_dir):
    """Verify that the tools hash file is written after building base."""
    monkeypatch.setattr("bubble.tools._host_has_command", lambda cmd: cmd == "claude")
    monkeypatch.setattr("bubble.images.builder.get_vscode_commit", lambda: None)
    monkeypatch.setattr("bubble.images.builder.wait_for_container", lambda *a, **kw: None)

    from bubble.images.builder import TOOLS_HASH_FILE, build_image

    mock_runtime._images.discard("base")
    build_image(mock_runtime, "base")

    assert TOOLS_HASH_FILE.exists()
    stored = TOOLS_HASH_FILE.read_text().strip()
    assert len(stored) == 16


def test_build_base_purges_derived_images(mock_runtime, monkeypatch, tmp_data_dir):
    """Verify that building base with tools deletes all derived images recursively."""
    monkeypatch.setattr("bubble.tools._host_has_command", lambda cmd: cmd == "claude")
    monkeypatch.setattr("bubble.images.builder.get_vscode_commit", lambda: None)
    monkeypatch.setattr("bubble.images.builder.wait_for_container", lambda *a, **kw: None)

    from bubble.images.builder import build_image

    # Pre-populate derived images, remove base so it actually builds
    mock_runtime._images.discard("base")
    mock_runtime._images.add("lean")

    build_image(mock_runtime, "base")

    # Derived images should have been deleted
    delete_calls = [c for c in mock_runtime.calls if c[0] == "image_delete"]
    deleted_names = {c[1] for c in delete_calls}
    assert "lean" in deleted_names


def test_build_base_purges_dynamic_toolchain_images(mock_runtime, monkeypatch, tmp_data_dir):
    """Verify that building base also purges dynamic toolchain images (lean-v4.x.y)."""
    monkeypatch.setattr("bubble.tools._host_has_command", lambda cmd: cmd == "claude")
    monkeypatch.setattr("bubble.images.builder.get_vscode_commit", lambda: None)
    monkeypatch.setattr("bubble.images.builder.wait_for_container", lambda *a, **kw: None)

    from bubble.images.builder import build_image

    # Pre-populate static + dynamic images, remove base so it actually builds
    mock_runtime._images.discard("base")
    mock_runtime._images.add("lean")
    mock_runtime._images.add("lean-v4-16-0")

    build_image(mock_runtime, "base")

    delete_calls = [c for c in mock_runtime.calls if c[0] == "image_delete"]
    deleted_names = {c[1] for c in delete_calls}
    # Dynamic toolchain images should also be purged
    assert "lean-v4-16-0" in deleted_names


def test_collect_derived_images_recursive():
    """Verify _collect_derived_images walks the full dependency tree."""
    from bubble.images.builder import _collect_derived_images

    # "base" -> "lean" (only two images in simplified hierarchy)
    derived = set(_collect_derived_images("base"))
    assert "lean" in derived


def test_collect_derived_images_leaf():
    """Verify _collect_derived_images returns empty for leaf images."""
    from bubble.images.builder import _collect_derived_images

    assert _collect_derived_images("lean") == []


def test_collect_dynamic_toolchain_aliases(mock_runtime):
    """Verify dynamic toolchain images are found by alias pattern."""
    from bubble.images.builder import _collect_dynamic_toolchain_aliases

    mock_runtime._images.update(
        {
            "lean-v4-16-0",
            "lean-v4-17-0",
            "lean",
        }
    )

    # Only lean-family images in purged set trigger scanning
    aliases = set(_collect_dynamic_toolchain_aliases(mock_runtime, {"lean"}))
    assert "lean-v4-16-0" in aliases
    assert "lean-v4-17-0" in aliases
    assert "lean" not in aliases

    # No lean images in purged set -> no dynamic aliases
    assert _collect_dynamic_toolchain_aliases(mock_runtime, {"base"}) == []
