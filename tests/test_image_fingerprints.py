"""Tests for recipe-keyed Bubble image aliases."""

from bubble.images import builder


def test_same_recipe_has_same_alias_across_private_homes(mock_runtime, tmp_data_dir):
    first = builder.desired_image_alias(mock_runtime, "base")
    second = builder.desired_image_alias(mock_runtime, "base")
    assert first == second
    assert first.startswith("bubble-base-")


def test_customize_content_changes_base_and_derived_aliases(mock_runtime, tmp_data_dir):
    base_before = builder.desired_image_alias(mock_runtime, "base")
    lean_before = builder.desired_image_alias(mock_runtime, "lean")

    builder.CUSTOMIZE_SCRIPT.write_text("#!/bin/sh\necho customized\n")

    assert builder.desired_image_alias(mock_runtime, "base") != base_before
    assert builder.desired_image_alias(mock_runtime, "lean") != lean_before


def test_toolchain_alias_contains_version_and_parent_key(mock_runtime, tmp_data_dir):
    alias = builder.desired_image_alias(mock_runtime, "lean-v4.31.0")
    assert alias.startswith("bubble-lean-v4-31-0-")

    builder.CUSTOMIZE_SCRIPT.write_text("#!/bin/sh\ntrue\n")
    assert builder.desired_image_alias(mock_runtime, "lean-v4.31.0") != alias


def test_build_publishes_only_physical_alias(mock_runtime, monkeypatch, tmp_data_dir):
    monkeypatch.setattr(builder, "wait_for_container", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("bubble.tools._host_has_command", lambda _cmd: False)
    monkeypatch.setattr(builder, "get_vscode_commit", lambda: None)
    mock_runtime._images.clear()

    alias = builder.build_image(mock_runtime, "base")

    assert alias in mock_runtime._images
    assert "base" not in mock_runtime._images
    assert mock_runtime._image_properties[alias]["user.bubble.logical_name"] == "base"


def test_tools_and_vscode_commit_change_alias(mock_runtime, monkeypatch, tmp_data_dir):
    from bubble.config import load_config, save_config

    monkeypatch.setattr(builder, "get_vscode_commit", lambda: "a" * 40)
    config = load_config()
    config["tools"]["codex"] = "yes"
    save_config(config)
    before = builder.desired_image_alias(mock_runtime, "base")

    config["tools"]["codex"] = "no"
    save_config(config)
    tools_changed = builder.desired_image_alias(mock_runtime, "base")
    assert tools_changed != before

    monkeypatch.setattr(builder, "get_vscode_commit", lambda: "b" * 40)
    assert builder.desired_image_alias(mock_runtime, "base") != tools_changed


def test_fingerprinted_builder_is_hidden(mock_runtime, tmp_data_dir):
    name = builder.desired_image_alias(mock_runtime, "base") + "-builder"
    assert builder.is_builder_container(name)


def test_force_rebuilds_desired_variant(mock_runtime, monkeypatch, tmp_data_dir):
    monkeypatch.setattr(builder, "wait_for_container", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("bubble.tools._host_has_command", lambda _cmd: False)
    monkeypatch.setattr(builder, "get_vscode_commit", lambda: None)
    alias = builder.desired_image_alias(mock_runtime, "base")
    mock_runtime._images = {alias}

    assert builder.build_image(mock_runtime, "base", force=True) == alias
    assert ("image_delete", alias) in mock_runtime.calls
