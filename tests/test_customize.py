"""Tests for user-defined image customization script."""

import bubble.images.builder as builder


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
    monkeypatch.setattr("bubble.images.builder._wait_for_container", lambda *a, **kw: None)

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
    monkeypatch.setattr("bubble.images.builder._wait_for_container", lambda *a, **kw: None)

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


def test_customize_hash_file_written(mock_runtime, monkeypatch, tmp_data_dir):
    """Hash file is written after building with a customize script."""
    monkeypatch.setattr("bubble.tools._host_has_command", lambda cmd: False)
    monkeypatch.setattr("bubble.images.builder.get_vscode_commit", lambda: None)
    monkeypatch.setattr("bubble.images.builder._wait_for_container", lambda *a, **kw: None)

    from bubble.config import load_config, save_config

    config = load_config()
    config["tools"] = {"claude": "no", "codex": "no", "elan": "no"}
    config["editor"] = "shell"
    save_config(config)

    builder.CUSTOMIZE_SCRIPT.write_text("#!/bin/bash\necho hello\n")

    mock_runtime._images.discard("base")
    builder.build_image(mock_runtime, "base")

    assert builder.CUSTOMIZE_HASH_FILE.exists()
    stored = builder.CUSTOMIZE_HASH_FILE.read_text().strip()
    assert len(stored) == 16
    assert stored == builder.customize_hash()


def test_customize_hash_file_removed_when_no_script(mock_runtime, monkeypatch, tmp_data_dir):
    """Hash file is removed when customize script doesn't exist."""
    monkeypatch.setattr("bubble.tools._host_has_command", lambda cmd: False)
    monkeypatch.setattr("bubble.images.builder.get_vscode_commit", lambda: None)
    monkeypatch.setattr("bubble.images.builder._wait_for_container", lambda *a, **kw: None)

    from bubble.config import load_config, save_config

    config = load_config()
    config["tools"] = {"claude": "no", "codex": "no", "elan": "no"}
    config["editor"] = "shell"
    save_config(config)

    # Pre-populate hash file as if a previous build had a customize script
    builder.CUSTOMIZE_HASH_FILE.parent.mkdir(parents=True, exist_ok=True)
    builder.CUSTOMIZE_HASH_FILE.write_text("oldhash\n")

    mock_runtime._images.discard("base")
    builder.build_image(mock_runtime, "base")

    assert not builder.CUSTOMIZE_HASH_FILE.exists()


def test_build_lean_toolchain_runs_customize(mock_runtime, monkeypatch, tmp_data_dir):
    """Lean toolchain image build also runs the customize script."""
    monkeypatch.setattr("bubble.images.builder._wait_for_container", lambda *a, **kw: None)

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
    monkeypatch.setattr("bubble.images.builder._wait_for_container", lambda *a, **kw: None)

    mock_runtime._images.add("base")

    builder.CUSTOMIZE_SCRIPT.write_text("#!/bin/bash\necho custom\n")

    builder.build_image(mock_runtime, "lean")

    exec_calls = [c for c in mock_runtime.calls if c[0] == "exec"]
    # 2 exec calls: lean script + customize script
    assert len(exec_calls) == 2
    assert "custom" in exec_calls[-1][2][-1]


def test_nonbase_image_does_not_write_hash(mock_runtime, monkeypatch, tmp_data_dir):
    """Building a non-base image should not update the customize hash file.

    Only the base build should record the hash — otherwise building a derived
    image could falsely mark the system as current while base is still stale.
    """
    monkeypatch.setattr("bubble.tools._host_has_command", lambda cmd: False)
    monkeypatch.setattr("bubble.images.builder.get_vscode_commit", lambda: None)
    monkeypatch.setattr("bubble.images.builder._wait_for_container", lambda *a, **kw: None)

    mock_runtime._images.add("base")

    builder.CUSTOMIZE_SCRIPT.write_text("#!/bin/bash\necho custom\n")

    builder.build_image(mock_runtime, "lean")

    # Hash file should NOT exist — only base builds write it
    assert not builder.CUSTOMIZE_HASH_FILE.exists()
