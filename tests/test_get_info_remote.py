"""Regression test for issue #300: container lookup on a non-default remote.

On macOS/Colima, ``IncusRuntime`` is constructed with
``remote="bubble-colima"``. Recent incus clients treat a single
concatenated ``list`` token (``incus list <remote>:<name>``) as a name
filter on the *default* remote, so it matches nothing on a non-default
remote and ``_get_info`` raised "Container not found" even when the
container was running (breaking the base-image rebuild path).

``_get_info`` must instead pass the remote scope and the name filter as
*separate* arguments (``incus list <remote>: name=<name>``).
"""

from __future__ import annotations

import json
import subprocess

import pytest

from bubble.runtime.incus import IncusRuntime


def _fake_subprocess(records, containers):
    """A ``_run_subprocess`` stand-in that records argv and serves JSON.

    The fake models incus's ``name=`` filtering so tests exercise the real
    behavior ``_get_info`` depends on, not just the argv shape: a ``list``
    with a ``name=<value>`` token returns *containers* whose names contain
    ``<value>`` as a substring, mirroring incus versions where ``name=``
    over-matches.
    """

    def run(self, cmd, *, capture=True):
        records.append(cmd)
        name_filter = next((a[len("name=") :] for a in cmd if a.startswith("name=")), None)
        if name_filter is not None:
            result = [c for c in containers if name_filter in c["name"]]
        else:
            result = containers
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(result), stderr="")

    return run


def _container(name, status="Running"):
    return {"name": name, "status": status, "state": {}}


def test_get_info_uses_separate_remote_and_filter_tokens(monkeypatch):
    records: list[list[str]] = []
    monkeypatch.setattr(
        IncusRuntime,
        "_run_subprocess",
        _fake_subprocess(records, [_container("base-builder")]),
    )
    rt = IncusRuntime(remote="bubble-colima")
    info = rt._get_info("base-builder")
    assert info.name == "base-builder"
    # Exactly one incus invocation, with the remote scope and name filter as
    # *separate* argv tokens (never the broken "bubble-colima:base-builder").
    assert len(records) == 1
    cmd = records[0]
    assert cmd == ["incus", "list", "bubble-colima:", "name=base-builder", "--format=json"]
    assert "bubble-colima:base-builder" not in cmd


def test_get_info_no_remote_omits_scope_token(monkeypatch):
    records: list[list[str]] = []
    monkeypatch.setattr(
        IncusRuntime,
        "_run_subprocess",
        _fake_subprocess(records, [_container("foo")]),
    )
    rt = IncusRuntime()
    info = rt._get_info("foo")
    assert info.name == "foo"
    assert records[0] == ["incus", "list", "name=foo", "--format=json"]


def test_get_info_exact_match_among_substring_matches(monkeypatch):
    # Some incus versions match `name=base` as a substring, returning both
    # "base" and "base-builder"; _get_info must return the exact match.
    records: list[list[str]] = []
    monkeypatch.setattr(
        IncusRuntime,
        "_run_subprocess",
        _fake_subprocess(records, [_container("base-builder"), _container("base")]),
    )
    rt = IncusRuntime(remote="bubble-colima")
    info = rt._get_info("base")
    assert info.name == "base"


def test_get_info_not_found_raises(monkeypatch):
    records: list[list[str]] = []
    monkeypatch.setattr(
        IncusRuntime,
        "_run_subprocess",
        _fake_subprocess(records, []),
    )
    rt = IncusRuntime(remote="bubble-colima")
    with pytest.raises(RuntimeError, match="not found"):
        rt._get_info("missing")


def test_get_info_superstring_only_is_not_a_match(monkeypatch):
    # Asking for "base-build" when only "base-builder" exists must NOT match.
    records: list[list[str]] = []
    monkeypatch.setattr(
        IncusRuntime,
        "_run_subprocess",
        _fake_subprocess(records, [_container("base-builder")]),
    )
    rt = IncusRuntime(remote="bubble-colima")
    with pytest.raises(RuntimeError, match="not found"):
        rt._get_info("base-build")


def test_launch_then_lookup_on_remote(monkeypatch):
    """Reproduces issue #300 end-to-end: launch base-builder on the
    bubble-colima remote, then look it up via the separate-token filter."""
    records: list[list[str]] = []
    monkeypatch.setattr(
        IncusRuntime,
        "_run_subprocess",
        _fake_subprocess(records, [_container("base-builder")]),
    )
    rt = IncusRuntime(remote="bubble-colima")
    info = rt.launch("base-builder", "base")
    assert info.name == "base-builder"
    launch_cmd, list_cmd = records
    # launch uses remote:name as a single resource identifier (correct).
    assert launch_cmd == ["incus", "launch", "bubble-colima:base", "bubble-colima:base-builder"]
    # the follow-up lookup must NOT concatenate remote and name.
    assert "bubble-colima:base-builder" not in list_cmd
    assert "bubble-colima:" in list_cmd
    assert "name=base-builder" in list_cmd
