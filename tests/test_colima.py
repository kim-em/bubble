"""Tests for bubble.runtime.colima.

These cover the parts that don't actually need a running Colima:
command construction, path constants, and the incus-remote setup
logic with a mocked subprocess.
"""

import json
import subprocess
from pathlib import Path

import pytest


class TestColimaArgs:
    def test_uses_global_profile_flag(self):
        from bubble.runtime.colima import BUBBLE_COLIMA_PROFILE, _colima_args

        assert _colima_args("status") == [
            "colima",
            "--profile",
            BUBBLE_COLIMA_PROFILE,
            "status",
        ]

    def test_works_for_ssh_subcommand(self):
        """colima ssh does not accept positional profile, must use --profile."""
        from bubble.runtime.colima import _colima_args

        args = _colima_args("ssh", "--", "echo", "hi")
        # --profile must come before the subcommand
        assert args[0] == "colima"
        assert args[1] == "--profile"
        assert "ssh" in args
        ssh_idx = args.index("ssh")
        profile_idx = args.index("--profile")
        assert profile_idx < ssh_idx


class TestPathConstants:
    def test_paths_use_bubble_profile_name(self):
        from bubble.runtime.colima import (
            BUBBLE_COLIMA_PROFILE,
            COLIMA_LIMA_DIR,
            COLIMA_PROFILE_DIR,
        )

        assert BUBBLE_COLIMA_PROFILE == "bubble-colima"
        assert COLIMA_PROFILE_DIR == Path.home() / ".colima" / "bubble-colima"
        assert COLIMA_LIMA_DIR == Path.home() / ".colima" / "_lima" / "bubble-colima"


@pytest.fixture
def fake_socket(tmp_path, monkeypatch):
    """Make COLIMA_PROFILE_DIR resolve to a tmp dir with an incus.sock present."""
    from bubble.runtime import colima as colima_mod

    profile_dir = tmp_path / ".colima" / "bubble-colima"
    profile_dir.mkdir(parents=True)
    sock = profile_dir / "incus.sock"
    sock.write_text("")  # presence only
    monkeypatch.setattr(colima_mod, "COLIMA_PROFILE_DIR", profile_dir)
    return sock


class _FakeRun:
    """Records subprocess.run calls and returns scripted responses."""

    def __init__(self, scripts: dict):
        # scripts maps a tuple key (the first 3 argv tokens, e.g.
        # ("incus","remote","get-default")) to a CompletedProcess.
        self.scripts = scripts
        self.calls: list[list[str]] = []

    def __call__(self, args, **kwargs):
        self.calls.append(list(args))
        key = tuple(args[:3])
        if key in self.scripts:
            return self.scripts[key]
        # Default: stub success with empty output
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")


def _completed(stdout="", returncode=0):
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr="")


class TestEnsureIncusRemote:
    def test_noop_when_socket_missing(self, tmp_path, monkeypatch):
        from bubble.runtime import colima as colima_mod

        empty_dir = tmp_path / "empty"
        empty_dir.mkdir()
        monkeypatch.setattr(colima_mod, "COLIMA_PROFILE_DIR", empty_dir)

        fake = _FakeRun({})
        monkeypatch.setattr(colima_mod.subprocess, "run", fake)
        colima_mod._ensure_incus_remote()
        assert fake.calls == []

    def test_noop_when_default_already_correct(self, fake_socket, monkeypatch):
        from bubble.runtime import colima as colima_mod

        fake = _FakeRun(
            {
                ("incus", "remote", "get-default"): _completed(
                    stdout=colima_mod.BUBBLE_INCUS_REMOTE + "\n"
                ),
            }
        )
        monkeypatch.setattr(colima_mod.subprocess, "run", fake)
        colima_mod._ensure_incus_remote()
        # We should have asked for the default and stopped there.
        assert fake.calls == [["incus", "remote", "get-default"]]

    def test_adds_and_switches_when_remote_missing(self, fake_socket, monkeypatch):
        from bubble.runtime import colima as colima_mod

        fake = _FakeRun(
            {
                ("incus", "remote", "get-default"): _completed(stdout="local\n"),
                ("incus", "remote", "list"): _completed(stdout=json.dumps({})),
            }
        )
        monkeypatch.setattr(colima_mod.subprocess, "run", fake)
        colima_mod._ensure_incus_remote()

        cmds = [tuple(c[:3]) for c in fake.calls]
        assert ("incus", "remote", "add") in cmds
        assert ("incus", "remote", "switch") in cmds

    def test_refuses_to_clobber_alias_with_wrong_address(self, fake_socket, monkeypatch, capsys):
        from bubble.runtime import colima as colima_mod

        bogus_remotes = {
            colima_mod.BUBBLE_INCUS_REMOTE: {"Addr": "unix:///somewhere/else.sock"},
        }
        fake = _FakeRun(
            {
                ("incus", "remote", "get-default"): _completed(stdout="local\n"),
                ("incus", "remote", "list"): _completed(stdout=json.dumps(bogus_remotes)),
            }
        )
        monkeypatch.setattr(colima_mod.subprocess, "run", fake)
        colima_mod._ensure_incus_remote()

        # We must NOT have called `add` (alias exists) or `switch` (alias is stale).
        cmds = [tuple(c[:3]) for c in fake.calls]
        assert ("incus", "remote", "add") not in cmds
        assert ("incus", "remote", "switch") not in cmds
        # User should have been told.
        err = capsys.readouterr().err
        assert "Refusing to overwrite" in err

    def test_switches_when_alias_already_points_at_us(self, fake_socket, monkeypatch):
        from bubble.runtime import colima as colima_mod

        expected_addr = f"unix://{fake_socket}"
        good_remotes = {
            colima_mod.BUBBLE_INCUS_REMOTE: {"Addr": expected_addr},
        }
        fake = _FakeRun(
            {
                ("incus", "remote", "get-default"): _completed(stdout="local\n"),
                ("incus", "remote", "list"): _completed(stdout=json.dumps(good_remotes)),
            }
        )
        monkeypatch.setattr(colima_mod.subprocess, "run", fake)
        colima_mod._ensure_incus_remote()

        cmds = [tuple(c[:3]) for c in fake.calls]
        # No `add` (alias already exists) but yes `switch`.
        assert ("incus", "remote", "add") not in cmds
        assert ("incus", "remote", "switch") in cmds
