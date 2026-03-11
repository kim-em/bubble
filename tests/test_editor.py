"""Tests for editor support (emacs, neovim, vscode, shell)."""

import subprocess

from bubble.images.builder import IMAGES
from bubble.tools import EDITOR_TOOLS, TOOLS, resolve_tools
from bubble.vscode import open_editor


class TestEditorAsTools:
    """Editors are now installed as pluggable tools, not image variants."""

    def test_vscode_is_a_tool(self):
        assert "vscode" in TOOLS
        assert TOOLS["vscode"]["host_cmd"] == "code"
        assert TOOLS["vscode"]["priority"] == 90

    def test_emacs_is_a_tool(self):
        assert "emacs" in TOOLS
        assert TOOLS["emacs"]["host_cmd"] == "emacs"

    def test_neovim_is_a_tool(self):
        assert "neovim" in TOOLS
        assert TOOLS["neovim"]["host_cmd"] == "nvim"

    def test_editor_tools_set(self):
        assert EDITOR_TOOLS == {"vscode", "emacs", "neovim"}


class TestEditorToolResolution:
    """Editor tool resolution follows the 'editor' config key."""

    def test_default_editor_is_vscode(self, monkeypatch):
        monkeypatch.setattr("bubble.tools._host_has_command", lambda cmd: False)
        config = {}
        enabled = resolve_tools(config)
        assert "vscode" in enabled

    def test_editor_emacs(self, monkeypatch):
        monkeypatch.setattr("bubble.tools._host_has_command", lambda cmd: False)
        config = {"editor": "emacs"}
        enabled = resolve_tools(config)
        assert "emacs" in enabled
        assert "vscode" not in enabled

    def test_editor_neovim(self, monkeypatch):
        monkeypatch.setattr("bubble.tools._host_has_command", lambda cmd: False)
        config = {"editor": "neovim"}
        enabled = resolve_tools(config)
        assert "neovim" in enabled
        assert "vscode" not in enabled

    def test_editor_shell_no_editor_tool(self, monkeypatch):
        monkeypatch.setattr("bubble.tools._host_has_command", lambda cmd: False)
        config = {"editor": "shell"}
        enabled = resolve_tools(config)
        assert "vscode" not in enabled
        assert "emacs" not in enabled
        assert "neovim" not in enabled

    def test_editor_can_be_disabled_via_tools(self, monkeypatch):
        monkeypatch.setattr("bubble.tools._host_has_command", lambda cmd: False)
        config = {"editor": "vscode", "tools": {"vscode": "no"}}
        enabled = resolve_tools(config)
        assert "vscode" not in enabled

    def test_editor_force_yes(self, monkeypatch):
        monkeypatch.setattr("bubble.tools._host_has_command", lambda cmd: False)
        config = {"editor": "shell", "tools": {"emacs": "yes"}}
        enabled = resolve_tools(config)
        assert "emacs" in enabled


class TestImageRegistry:
    """Verify the simplified IMAGES registry (no editor variants)."""

    def test_only_base_lean_and_python(self):
        assert set(IMAGES.keys()) == {"base", "lean", "python"}

    def test_base_exists(self):
        assert IMAGES["base"]["script"] == "base.sh"
        assert IMAGES["base"]["parent"] == "images:ubuntu/24.04"

    def test_lean_exists(self):
        assert "lean" in IMAGES
        assert IMAGES["lean"]["parent"] == "base"
        assert IMAGES["lean"]["script"] == "lean.sh"

    def test_no_editor_variants(self):
        for name in IMAGES:
            assert "-vscode" not in name
            assert "-emacs" not in name
            assert "-neovim" not in name


class TestOpenEditorEmacs:
    def test_emacs_ssh_command(self, monkeypatch):
        """Emacs editor should SSH with -t and run emacs in project dir."""
        calls = []
        monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: calls.append(cmd))
        open_editor("emacs", "test-bubble", "/home/user/project")
        assert len(calls) == 1
        cmd = calls[0]
        assert cmd[0] == "ssh"
        assert cmd[1] == "bubble-test-bubble"
        assert cmd[2] == "-t"
        assert "emacs ." in cmd[3]
        assert "cd" in cmd[3]
        assert "/home/user/project" in cmd[3]

    def test_emacs_checks_build_marker(self, monkeypatch):
        """Emacs SSH command should include marker file check for auto-build."""
        calls = []
        monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: calls.append(cmd))
        open_editor("emacs", "test-bubble", "/home/user/project")
        cmd = calls[0][3]
        assert ".bubble-fetch-cache" in cmd
        assert "build.log" in cmd


class TestOpenEditorNeovim:
    def test_neovim_ssh_command(self, monkeypatch):
        """Neovim editor should SSH with -t and run nvim in project dir."""
        calls = []
        monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: calls.append(cmd))
        open_editor("neovim", "test-bubble", "/home/user/project")
        assert len(calls) == 1
        cmd = calls[0]
        assert cmd[0] == "ssh"
        assert cmd[1] == "bubble-test-bubble"
        assert cmd[2] == "-t"
        assert "nvim ." in cmd[3]
        assert "/home/user/project" in cmd[3]

    def test_neovim_checks_build_marker(self, monkeypatch):
        """Neovim SSH command should include marker file check for auto-build."""
        calls = []
        monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: calls.append(cmd))
        open_editor("neovim", "test-bubble", "/home/user/project")
        cmd = calls[0][3]
        assert ".bubble-fetch-cache" in cmd
        assert "build.log" in cmd


class TestOpenEditorShell:
    def test_shell_no_command(self, monkeypatch):
        """Shell editor without command should just SSH."""
        calls = []
        monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: calls.append(cmd))
        open_editor("shell", "test-bubble")
        assert calls == [["ssh", "bubble-test-bubble"]]

    def test_shell_with_command(self, monkeypatch):
        """Shell editor with command should SSH and pass the command."""
        calls = []
        monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: calls.append(cmd))
        open_editor("shell", "test-bubble", command=["lake", "build"])
        assert calls == [["ssh", "bubble-test-bubble", "lake", "build"]]
