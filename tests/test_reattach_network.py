"""Tests for issue #285: re-applying the network allowlist after a stop/start.

After ``incus stop`` destroys the container's network namespace, the next
``incus start`` recreates it empty (default-ACCEPT iptables). Without explicit
replay, the bubble silently comes back up unprotected. ``ensure_running``
performs the replay; on replay failure it stops the container so we fail
closed instead of leaving an unprotected container running.
"""

import pytest

from bubble.container_helpers import (
    ensure_running,
    reapply_network_after_restart,
    recover_extra_domains,
)
from bubble.lifecycle import register_bubble
from bubble.runtime.base import ContainerInfo


class _RecordingRuntime:
    """Minimal runtime that records exec/start/stop/unfreeze calls."""

    def __init__(self, name: str, state: str = "stopped"):
        self._info = ContainerInfo(name=name, state=state)
        self.exec_calls: list[list[str]] = []
        self.start_calls: list[str] = []
        self.stop_calls: list[str] = []
        self.unfreeze_calls: list[str] = []
        self.exec_should_raise = False

    def list_containers(self, fast: bool = True):
        return [self._info]

    def start(self, name: str):
        self.start_calls.append(name)

    def stop(self, name: str):
        self.stop_calls.append(name)

    def unfreeze(self, name: str):
        self.unfreeze_calls.append(name)

    def exec(self, name: str, command, **kwargs):
        if self.exec_should_raise:
            raise RuntimeError("simulated exec failure")
        self.exec_calls.append(list(command))
        return ""


def _iptables_scripts(runtime: _RecordingRuntime) -> list[str]:
    return [
        cmd[-1] for cmd in runtime.exec_calls if cmd[:2] == ["bash", "-c"] and "iptables" in cmd[-1]
    ]


class TestReapplyNetworkAfterRestart:
    def test_reapplies_with_stored_extra_domains(self, tmp_data_dir, monkeypatch):
        register_bubble(
            "lean4-master",
            "leanprover/lean4",
            network_enabled=True,
            extra_domains=["releases.lean-lang.org"],
        )
        runtime = _RecordingRuntime("lean4-master", state="running")
        monkeypatch.setattr("bubble.config.load_config", lambda: {})

        reapply_network_after_restart(runtime, "lean4-master")

        scripts = _iptables_scripts(runtime)
        assert scripts
        joined = "\n".join(scripts)
        assert "iptables -P OUTPUT DROP" in joined
        assert "ip6tables -P OUTPUT DROP" in joined
        assert "releases.lean-lang.org" in joined

    def test_skips_when_network_disabled(self, tmp_data_dir, monkeypatch):
        register_bubble("plain", "org/repo", network_enabled=False)
        runtime = _RecordingRuntime("plain", state="running")
        monkeypatch.setattr("bubble.config.load_config", lambda: {})

        reapply_network_after_restart(runtime, "plain")

        assert runtime.exec_calls == []

    def test_legacy_entry_defaults_to_reapplying(self, tmp_data_dir, monkeypatch):
        register_bubble("legacy", "org/repo")  # no network kwargs
        runtime = _RecordingRuntime("legacy", state="running")
        monkeypatch.setattr("bubble.config.load_config", lambda: {})

        reapply_network_after_restart(runtime, "legacy")

        assert _iptables_scripts(runtime), "expected legacy bubble to re-apply allowlist"

    def test_persists_empty_domains_distinct_from_legacy(self, tmp_data_dir):
        register_bubble("py-bubble", "org/repo", network_enabled=True, extra_domains=[])
        from bubble.lifecycle import get_bubble_info

        info = get_bubble_info("py-bubble")
        assert info["extra_domains"] == []  # explicit empty list, not missing


class TestRecoverExtraDomains:
    def test_returns_none_without_org_repo(self, tmp_data_dir):
        assert recover_extra_domains({}) is None

    def test_returns_none_when_no_bare_repo(self, tmp_data_dir):
        assert recover_extra_domains({"org_repo": "unknown/repo"}) is None

    def test_prefers_commit_over_branch(self, tmp_data_dir, monkeypatch):
        # Verify that recovery passes ``commit`` (not ``branch``) as the ref so
        # branch tip drift in the bare mirror doesn't widen the allowlist.
        bare_repo = tmp_data_dir / "git" / "repo.git"
        bare_repo.mkdir(parents=True)

        seen_refs = []

        def fake_select_hook(path, ref):
            seen_refs.append(ref)
            return None

        monkeypatch.setattr("bubble.hooks.select_hook", fake_select_hook)

        recover_extra_domains({"org_repo": "owner/repo", "branch": "main", "commit": "deadbeef"})
        assert seen_refs == ["deadbeef"]


class TestEnsureRunningReapplies:
    def test_stopped_container_replays_iptables(self, tmp_data_dir, monkeypatch):
        register_bubble(
            "stopped-bubble",
            "org/repo",
            network_enabled=True,
            extra_domains=["example.com"],
        )
        runtime = _RecordingRuntime("stopped-bubble", state="stopped")
        monkeypatch.setattr("bubble.config.load_config", lambda: {})

        ensure_running(runtime, "stopped-bubble")

        assert runtime.start_calls == ["stopped-bubble"]
        assert _iptables_scripts(runtime), "expected iptables replay"

    def test_frozen_container_does_not_replay(self, tmp_data_dir, monkeypatch):
        register_bubble(
            "paused",
            "org/repo",
            network_enabled=True,
            extra_domains=["example.com"],
        )
        runtime = _RecordingRuntime("paused", state="frozen")
        monkeypatch.setattr("bubble.config.load_config", lambda: {})

        ensure_running(runtime, "paused")

        # Frozen containers preserve their network namespace; no replay needed.
        assert runtime.unfreeze_calls == ["paused"]
        assert _iptables_scripts(runtime) == []

    def test_running_container_does_not_replay(self, tmp_data_dir, monkeypatch):
        register_bubble(
            "live",
            "org/repo",
            network_enabled=True,
            extra_domains=["example.com"],
        )
        runtime = _RecordingRuntime("live", state="running")
        monkeypatch.setattr("bubble.config.load_config", lambda: {})

        ensure_running(runtime, "live")

        assert runtime.start_calls == []
        assert _iptables_scripts(runtime) == []

    def test_replay_failure_stops_container(self, tmp_data_dir, monkeypatch):
        """Fail-closed: a failing replay must not leave an unprotected container up."""
        register_bubble(
            "fail-bubble",
            "org/repo",
            network_enabled=True,
            extra_domains=["example.com"],
        )
        runtime = _RecordingRuntime("fail-bubble", state="stopped")
        runtime.exec_should_raise = True
        monkeypatch.setattr("bubble.config.load_config", lambda: {})

        with pytest.raises(Exception):
            ensure_running(runtime, "fail-bubble")

        assert runtime.start_calls == ["fail-bubble"]
        assert runtime.stop_calls == ["fail-bubble"], "container must be stopped on replay failure"
