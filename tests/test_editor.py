"""Tests for editor support (emacs, neovim, vscode, shell)."""

import subprocess

from bubble.cli import _apply_editor_to_image, _editor_image_suffix
from bubble.images.builder import IMAGES
from bubble.vscode import open_editor


class TestEditorImageSuffix:
    def test_vscode_suffix(self):
        assert _editor_image_suffix("vscode") == "-vscode"

    def test_shell_no_suffix(self):
        assert _editor_image_suffix("shell") == ""

    def test_emacs_suffix(self):
        assert _editor_image_suffix("emacs") == "-emacs"

    def test_neovim_suffix(self):
        assert _editor_image_suffix("neovim") == "-neovim"


class TestApplyEditorToImage:
    def test_base_vscode(self):
        assert _apply_editor_to_image("base", "vscode") == "base-vscode"

    def test_base_emacs(self):
        assert _apply_editor_to_image("base", "emacs") == "base-emacs"

    def test_base_neovim(self):
        assert _apply_editor_to_image("base", "neovim") == "base-neovim"

    def test_base_shell(self):
        assert _apply_editor_to_image("base", "shell") == "base"

    def test_lean_vscode(self):
        assert _apply_editor_to_image("lean", "vscode") == "lean-vscode"

    def test_lean_emacs(self):
        assert _apply_editor_to_image("lean", "emacs") == "lean-emacs"

    def test_lean_neovim(self):
        assert _apply_editor_to_image("lean", "neovim") == "lean-neovim"

    def test_lean_shell(self):
        assert _apply_editor_to_image("lean", "shell") == "lean"

    def test_toolchain_vscode(self):
        assert _apply_editor_to_image("lean-v4.27.0", "vscode") == "lean-vscode-v4.27.0"

    def test_toolchain_emacs(self):
        assert _apply_editor_to_image("lean-v4.27.0", "emacs") == "lean-emacs-v4.27.0"

    def test_toolchain_neovim(self):
        assert _apply_editor_to_image("lean-v4.27.0", "neovim") == "lean-neovim-v4.27.0"

    def test_toolchain_shell(self):
        assert _apply_editor_to_image("lean-v4.27.0", "shell") == "lean-v4.27.0"

    def test_toolchain_rc_emacs(self):
        assert _apply_editor_to_image("lean-v4.27.0-rc2", "emacs") == "lean-emacs-v4.27.0-rc2"


class TestImageRegistry:
    """Verify the IMAGES registry has the expected editor variants."""

    def test_base_vscode_exists(self):
        assert "base-vscode" in IMAGES
        assert IMAGES["base-vscode"]["parent"] == "base"
        assert IMAGES["base-vscode"]["script"] == "vscode.sh"

    def test_base_emacs_exists(self):
        assert "base-emacs" in IMAGES
        assert IMAGES["base-emacs"]["parent"] == "base"

    def test_base_neovim_exists(self):
        assert "base-neovim" in IMAGES
        assert IMAGES["base-neovim"]["parent"] == "base"

    def test_lean_is_core(self):
        """lean image is the core image (elan + leantar, no editor)."""
        assert "lean" in IMAGES
        assert IMAGES["lean"]["parent"] == "base"
        assert IMAGES["lean"]["script"] == "lean.sh"

    def test_lean_vscode_exists(self):
        assert "lean-vscode" in IMAGES
        assert IMAGES["lean-vscode"]["parent"] == "lean"
        assert IMAGES["lean-vscode"]["script"] == "vscode.sh"

    def test_lean_emacs_exists(self):
        assert "lean-emacs" in IMAGES
        assert IMAGES["lean-emacs"]["parent"] == "base-emacs"

    def test_lean_neovim_exists(self):
        assert "lean-neovim" in IMAGES
        assert IMAGES["lean-neovim"]["parent"] == "base-neovim"

    def test_no_lean_core_image(self):
        """lean-core was renamed to lean."""
        assert "lean-core" not in IMAGES


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
