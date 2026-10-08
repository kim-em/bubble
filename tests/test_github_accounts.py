"""Named accounts remain isolated across a shared proxy; no real GitHub credentials/network."""

import base64
import os
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from click.testing import CliRunner

from bubble import auth_proxy as ap
from bubble import github_token as gt
from bubble.cli import main


@pytest.fixture(autouse=True)
def restore_selection_environment(monkeypatch):
    # Selection mutates the process environment; track every written key even if initially absent.
    for key in ("GH_TOKEN", "GITHUB_TOKEN", "GH_HOST", "BUBBLE_GITHUB_ACCOUNT"):
        monkeypatch.setenv(key, os.environ.get(key, ""))


def test_named_credential_lookup_and_identity_check(monkeypatch):
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    calls = []

    def run(args, **kw):
        calls.append((args, kw))
        if args[1:3] == ["auth", "token"]:
            return SimpleNamespace(returncode=0, stdout="alice-secret\n")
        assert kw["env"]["GH_TOKEN"] == "alice-secret"
        return SimpleNamespace(returncode=0, stdout="Alice\n")

    monkeypatch.setattr(gt.subprocess, "run", run)
    assert gt.select_github_account("alice") == "alice-secret"
    assert calls[0][0][-2:] == ["--user", "alice"]
    assert os.environ["BUBBLE_GITHUB_ACCOUNT"] == "alice"
    # The secret is never an argument, including identity verification.
    assert all("alice-secret" not in args for args, _ in calls)
    with pytest.raises(RuntimeError, match="does not authenticate"):
        gt.select_github_account("bob")
    with pytest.raises(RuntimeError, match="GitHub account must"):
        gt.select_github_account("--bob")


def test_missing_credential_fails_without_echoing_stderr(monkeypatch):
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setattr(
        gt.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(returncode=1, stdout="", stderr="secret"),
    )
    with pytest.raises(RuntimeError, match="No GitHub credential") as exc:
        gt.select_github_account("alice")
    assert "secret" not in str(exc.value)


def test_cli_selects_account_before_opening_target(monkeypatch):
    selected = []
    opened = []
    monkeypatch.setattr(gt, "select_github_account", lambda value: selected.append(value))
    monkeypatch.setattr("bubble.cli._open_single", lambda target, **kw: opened.append(target))
    result = CliRunner().invoke(main, ["open", "owner/repo", "--github-account", "alice"])
    assert result.exit_code == 0, result.output
    assert selected == ["alice"] and opened == ["owner/repo"]


def test_registry_keeps_selected_token_only_on_host(tmp_path, monkeypatch):
    monkeypatch.setattr(ap, "AUTH_PROXY_TOKENS", tmp_path / "auth-tokens.json")
    monkeypatch.setenv("BUBBLE_GITHUB_ACCOUNT", "alice")
    monkeypatch.setattr(gt, "select_github_account", lambda _: "raw-host-secret")
    proxy_token = ap.generate_auth_token("alice-container", "owner", "repo")
    assert "raw-host-secret" not in proxy_token
    entry = ap._load_tokens()[proxy_token]
    assert entry["github_account"] == "alice" and entry["github_token"] == "raw-host-secret"
    assert (tmp_path / "auth-tokens.json").stat().st_mode & 0o777 == 0o600
    assert not list(tmp_path.glob(".auth-tokens.json.*"))
    ap.remove_auth_tokens("alice-container")
    assert ap._load_tokens() == {}


def handler_for(account, credential):
    handler = ap.AuthProxyHandler.__new__(ap.AuthProxyHandler)
    handler.headers = {"X-Bubble-Token": "scoped-token"}
    handler.token_registry = SimpleNamespace(
        lookup=lambda _: {
            "container": account,
            "owner": "owner",
            "repo": "repo",
            "rest_api": True,
            "graphql_read": "whitelisted",
            "graphql_write": "whitelisted",
            "github_account": account,
            "github_token": credential,
        }
    )
    handler.wfile = BytesIO()
    handler.send_response = MagicMock()
    handler.send_header = MagicMock()
    handler.end_headers = MagicMock()
    handler._authenticate()
    return handler


def test_concurrent_rest_git_and_graphql_use_their_own_credential(monkeypatch):
    default = ap.GitHubTokenRefresher("default-secret")
    monkeypatch.setattr(ap.AuthProxyHandler, "token_refresher", default, raising=False)
    seen = []

    class Response(BytesIO):
        status = 200

        def getheaders(self):
            return []

        def __enter__(self):
            return self

        def __exit__(self, *_):
            self.close()

    def forward(req, **kw):
        auth = req.get_header("Authorization")
        if auth.startswith("Basic "):
            auth = base64.b64decode(auth[6:]).decode()
        seen.append((req.full_url, auth))
        return Response(b"{}")

    monkeypatch.setattr(ap, "build_opener", lambda *_: SimpleNamespace(open=forward))
    monkeypatch.setattr(ap.ssl, "create_default_context", lambda: None)
    alice, bob = handler_for("alice", "alice-secret"), handler_for("bob", "bob-secret")

    def exercise(handler):
        for _ in range(5):
            handler._forward_to_github(
                "GET",
                "https://api.github.com/repos/owner/repo",
                None,
                "c",
                "/api",
                host=ap.GITHUB_API_HOST,
            )
            handler._forward_to_github(
                "POST",
                "https://github.com/owner/repo/git-receive-pack",
                b"",
                "c",
                "/git",
                host=ap.GITHUB_HOST,
            )
            handler._github_graphql_query("query { viewer { login } }", {})

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(exercise, (alice, bob)))
    assert len(seen) == 30
    assert sum("alice-secret" in auth for _, auth in seen) == 15
    assert sum("bob-secret" in auth for _, auth in seen) == 15
    assert all("default-secret" not in auth for _, auth in seen)
    assert default.token == "default-secret"
    assert alice.token_refresher.refresh() == "alice-secret"
    assert bob.token_refresher.refresh() == "bob-secret"


def test_preflight_cache_does_not_reuse_another_accounts_permissions(monkeypatch):
    alice, bob = handler_for("alice", "alice-secret"), handler_for("bob", "bob-secret")
    monkeypatch.setattr(ap.AuthProxyHandler, "_preflight_cache", {})
    monkeypatch.setattr(ap.AuthProxyHandler, "_repo_node_id_cache", {})
    alice._preflight_cache_put("node", "owner/private")
    assert alice._preflight_cache_get("node") == (True, "owner/private")
    assert bob._preflight_cache_get("node") == (False, None)
    alice.rate_limiter = bob.rate_limiter = SimpleNamespace(check=lambda _: True)
    alice._github_graphql_query = lambda *a: {"data": {"repository": {"id": "alice-node"}}}
    bob._github_graphql_query = lambda *a: {"data": {"repository": {"id": "bob-node"}}}
    assert alice._get_repo_node_id("owner", "private", "c") == "alice-node"
    assert bob._get_repo_node_id("owner", "private", "c") == "bob-node"


def test_missing_selected_credential_never_falls_back_to_default():
    handler = handler_for("alice", "")
    handler.send_response.assert_called_with(403)
    assert b"has no credential" in handler.wfile.getvalue()


def test_old_daemon_is_rejected_for_a_selected_account(monkeypatch):
    monkeypatch.setenv("BUBBLE_GITHUB_ACCOUNT", "alice")
    endpoint = {
        "tcp": {"host": "127.0.0.1", "port": 7654},
        "pid": os.getpid(),
        "capabilities": ["allow-push"],
    }
    assert not gt._endpoint_alive(endpoint)
    endpoint["capabilities"].append("github-account")
    assert gt._endpoint_alive(endpoint)
