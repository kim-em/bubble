"""Tests for --ephemeral: pop the bubble after --command exits."""

import subprocess

import pytest
from click.testing import CliRunner

from bubble.cli import main


class TestEphemeralValidation:
    def test_ephemeral_without_command_errors(self):
        """--ephemeral requires --command."""
        runner = CliRunner()
        result = runner.invoke(main, ["open", "--ephemeral", "."])
        assert result.exit_code == 1
        assert "--ephemeral requires --command" in result.output

    def test_ephemeral_with_no_interactive_errors(self):
        """--ephemeral cannot be combined with --no-interactive."""
        runner = CliRunner()
        result = runner.invoke(
            main,
            ["open", "--ephemeral", "--no-interactive", "--command", "true", "."],
        )
        assert result.exit_code == 1
        assert "--ephemeral cannot be combined with --no-interactive" in result.output


class TestEphemeralPopExitCode:
    """Ephemeral pop happens via _ephemeral_pop_and_exit."""

    def test_pops_and_propagates_exit_code(self, monkeypatch):
        from bubble import finalization

        called = {}

        def fake_destroy(name, info=None):
            called["name"] = name
            return True, ""

        monkeypatch.setattr("bubble.commands.lifecycle.destroy_bubble", fake_destroy)

        with pytest.raises(SystemExit) as exc:
            finalization._ephemeral_pop_and_exit("my-bubble", 7)
        assert exc.value.code == 7
        assert called["name"] == "my-bubble"

    def test_propagates_zero_exit_code(self, monkeypatch):
        from bubble import finalization

        monkeypatch.setattr(
            "bubble.commands.lifecycle.destroy_bubble", lambda name, info=None: (True, "")
        )

        with pytest.raises(SystemExit) as exc:
            finalization._ephemeral_pop_and_exit("my-bubble", 0)
        assert exc.value.code == 0

    def test_destroy_failure_still_propagates_exit_code(self, monkeypatch):
        """If destroy_bubble fails, we still propagate the command's exit code."""
        from bubble import finalization

        monkeypatch.setattr(
            "bubble.commands.lifecycle.destroy_bubble",
            lambda name, info=None: (False, "container busy"),
        )

        with pytest.raises(SystemExit) as exc:
            finalization._ephemeral_pop_and_exit("my-bubble", 3)
        assert exc.value.code == 3

    def test_destroy_exception_still_propagates_exit_code(self, monkeypatch):
        """If destroy_bubble raises, we still propagate the command's exit code."""
        from bubble import finalization

        def boom(name, info=None):
            raise RuntimeError("kaboom")

        monkeypatch.setattr("bubble.commands.lifecycle.destroy_bubble", boom)

        with pytest.raises(SystemExit) as exc:
            finalization._ephemeral_pop_and_exit("my-bubble", 5)
        assert exc.value.code == 5


class TestDestroyBubble:
    """destroy_bubble preserves local state on failure."""

    def test_native_outside_native_dir_fails(self, tmp_path, monkeypatch):
        """Refuses to delete native paths outside NATIVE_DIR; preserves state."""
        from bubble.commands import lifecycle

        # Build an info entry with a path outside NATIVE_DIR
        bad_path = tmp_path / "outside-native-dir"
        bad_path.mkdir()
        info = {"native": True, "native_path": str(bad_path)}

        unregistered = []
        monkeypatch.setattr(
            "bubble.commands.lifecycle.unregister_bubble",
            lambda name: unregistered.append(name),
        )
        monkeypatch.setattr(
            "bubble.commands.lifecycle._cleanup_tokens",
            lambda *a, **kw: None,
        )

        ok, err = lifecycle.destroy_bubble("foo", info=info)
        assert ok is False
        assert "Refusing to delete" in err
        assert unregistered == []  # local state preserved
        assert bad_path.exists()  # path not deleted

    def test_local_delete_failure_preserves_state(self, monkeypatch):
        """If runtime.delete raises an unrecognized error, leave registry intact."""
        from bubble.commands import lifecycle

        unregistered = []
        cleaned = []
        ssh_removed = []

        class FakeRuntime:
            def delete(self, name, force=True):
                raise subprocess.CalledProcessError(
                    1, ["incus", "delete", name], stderr="some unrecoverable error"
                )

        monkeypatch.setattr("bubble.commands.lifecycle.load_config", lambda: {})
        monkeypatch.setattr(
            "bubble.commands.lifecycle.get_runtime",
            lambda config, ensure_ready=False: FakeRuntime(),
        )
        monkeypatch.setattr(
            "bubble.commands.lifecycle.unregister_bubble",
            lambda name: unregistered.append(name),
        )
        monkeypatch.setattr(
            "bubble.commands.lifecycle._cleanup_tokens",
            lambda *a, **kw: cleaned.append(a),
        )
        monkeypatch.setattr(
            "bubble.commands.lifecycle.remove_ssh_config",
            lambda name: ssh_removed.append(name),
        )

        # Pretend it's a local container (no remote_host, not native)
        info = {}
        ok, err = lifecycle.destroy_bubble("foo", info=info)
        assert ok is False
        assert "unrecoverable" in err
        assert unregistered == []
        assert cleaned == []
        assert ssh_removed == []

    def test_local_already_gone_succeeds_and_cleans_up(self, monkeypatch):
        """If runtime says container is already gone, treat as success."""
        from bubble.commands import lifecycle

        unregistered = []

        class FakeRuntime:
            def delete(self, name, force=True):
                raise subprocess.CalledProcessError(
                    1, ["incus", "delete", name], stderr="Error: container not found"
                )

        monkeypatch.setattr("bubble.commands.lifecycle.load_config", lambda: {})
        monkeypatch.setattr(
            "bubble.commands.lifecycle.get_runtime",
            lambda config, ensure_ready=False: FakeRuntime(),
        )
        monkeypatch.setattr(
            "bubble.commands.lifecycle.unregister_bubble",
            lambda name: unregistered.append(name),
        )
        monkeypatch.setattr("bubble.commands.lifecycle._cleanup_tokens", lambda *a, **kw: None)
        monkeypatch.setattr("bubble.commands.lifecycle.remove_ssh_config", lambda name: None)

        ok, err = lifecycle.destroy_bubble("foo", info={})
        assert ok is True
        assert err == ""
        assert unregistered == ["foo"]
