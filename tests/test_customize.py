"""Tests for user-defined image customization script."""

import pytest

import bubble.images.builder as builder


@pytest.fixture(autouse=True)
def logical_aliases(monkeypatch):
    monkeypatch.setattr(builder, "desired_image_alias", lambda _runtime, name: name)


def test_customize_hash_no_script(tmp_data_dir):
    """Returns None when no customize script exists."""
    assert not builder.CUSTOMIZE_SCRIPT.exists()
    assert builder.customize_hash() is None


def test_customize_hash_with_script(tmp_data_dir):
    """Returns a stable hash when the script exists."""
    builder.CUSTOMIZE_SCRIPT.write_text("#!/bin/bash\napt-get install -y ripgrep\n")
    h = builder.customize_hash()
    assert h is not None
    assert len(h) == 16
    # Stable
    assert builder.customize_hash() == h


def test_customize_hash_changes_with_content(tmp_data_dir):
    """Hash changes when script content changes."""
    builder.CUSTOMIZE_SCRIPT.write_text("#!/bin/bash\napt-get install -y ripgrep\n")
    h1 = builder.customize_hash()
    builder.CUSTOMIZE_SCRIPT.write_text("#!/bin/bash\napt-get install -y tmux\n")
    h2 = builder.customize_hash()
    assert h1 != h2


def test_build_image_runs_customize_script(mock_runtime, monkeypatch, tmp_data_dir):
    """Building any image runs the customize script as the final step."""
    monkeypatch.setattr("bubble.tools._host_has_command", lambda cmd: False)
    monkeypatch.setattr("bubble.images.builder.get_vscode_commit", lambda: None)
    monkeypatch.setattr("bubble.images.builder.wait_for_container", lambda *a, **kw: None)

    from bubble.config import load_config, save_config

    config = load_config()
    config["tools"] = {"claude": "no", "codex": "no", "elan": "no"}
    config["editor"] = "shell"
    save_config(config)

    builder.CUSTOMIZE_SCRIPT.write_text("#!/bin/bash\napt-get install -y ripgrep\n")

    mock_runtime._images.discard("base")
    builder.build_image(mock_runtime, "base")

    exec_calls = [c for c in mock_runtime.calls if c[0] == "exec"]
    # 2 exec calls: main script + customize script
    assert len(exec_calls) == 2
    # Last exec should be the customize script
    assert "ripgrep" in exec_calls[-1][2][-1]


def test_build_image_skips_customize_when_absent(mock_runtime, monkeypatch, tmp_data_dir):
    """No customize exec when script doesn't exist."""
    monkeypatch.setattr("bubble.tools._host_has_command", lambda cmd: False)
    monkeypatch.setattr("bubble.images.builder.get_vscode_commit", lambda: None)
    monkeypatch.setattr("bubble.images.builder.wait_for_container", lambda *a, **kw: None)

    from bubble.config import load_config, save_config

    config = load_config()
    config["tools"] = {"claude": "no", "codex": "no", "elan": "no"}
    config["editor"] = "shell"
    save_config(config)

    mock_runtime._images.discard("base")
    builder.build_image(mock_runtime, "base")

    exec_calls = [c for c in mock_runtime.calls if c[0] == "exec"]
    # Only 1 exec call: main script (no customize)
    assert len(exec_calls) == 1


def test_build_lean_toolchain_runs_customize(mock_runtime, monkeypatch, tmp_data_dir):
    """Lean toolchain image build also runs the customize script."""
    monkeypatch.setattr("bubble.images.builder.wait_for_container", lambda *a, **kw: None)

    mock_runtime._images.add("lean")

    builder.CUSTOMIZE_SCRIPT.write_text("#!/bin/bash\napt-get install -y fd-find\n")

    builder.build_lean_toolchain_image(mock_runtime, "v4.16.0")

    exec_calls = [c for c in mock_runtime.calls if c[0] == "exec"]
    # 2 exec calls: lean-toolchain script + customize script
    assert len(exec_calls) == 2
    assert "fd-find" in exec_calls[-1][2][-1]


def test_nonbase_image_runs_customize(mock_runtime, monkeypatch, tmp_data_dir):
    """Non-base images (e.g. lean) also run the customize script."""
    monkeypatch.setattr("bubble.tools._host_has_command", lambda cmd: False)
    monkeypatch.setattr("bubble.images.builder.get_vscode_commit", lambda: None)
    monkeypatch.setattr("bubble.images.builder.wait_for_container", lambda *a, **kw: None)

    mock_runtime._images.add("base")

    builder.CUSTOMIZE_SCRIPT.write_text("#!/bin/bash\necho custom\n")

    builder.build_image(mock_runtime, "lean")

    exec_calls = [c for c in mock_runtime.calls if c[0] == "exec"]
    # 2 exec calls: lean script + customize script
    assert len(exec_calls) == 2
    assert "custom" in exec_calls[-1][2][-1]
