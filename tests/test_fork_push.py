"""Tests for fork-PR push support (issue #319).

Covers the fork-repo resolution helpers that feed the auth proxy token's
``push_repos`` set: ``pr_fork_repo`` (auto-detect a fork-headed PR's head
repo) and the CLI's ``_resolve_push_repos`` (combine explicit
``--allow-push`` flags with the detected fork).
"""

from unittest.mock import patch

from bubble.target import Target


def _pr_target(owner="FormalFrontier", repo="TauCeti", ref="42"):
    return Target(owner=owner, repo=repo, kind="pr", ref=ref, original=f"{owner}/{repo}/pull/{ref}")


def _repo_target(owner="FormalFrontier", repo="TauCeti"):
    return Target(owner=owner, repo=repo, kind="repo", ref="", original=f"{owner}/{repo}")


class TestPrForkRepo:
    def test_non_pr_target_returns_none(self):
        from bubble.clone import pr_fork_repo

        assert pr_fork_repo(_repo_target()) is None

    def test_fork_headed_pr_returns_head_repo(self):
        from bubble import clone

        with patch.object(
            clone,
            "_get_pr_metadata",
            return_value=("branch", "contributor/TauCeti", "https://github.com/x.git"),
        ):
            assert clone.pr_fork_repo(_pr_target()) == "contributor/TauCeti"

    def test_same_repo_pr_returns_none(self):
        from bubble import clone

        with patch.object(
            clone,
            "_get_pr_metadata",
            return_value=("branch", "FormalFrontier/TauCeti", "https://github.com/x.git"),
        ):
            assert clone.pr_fork_repo(_pr_target()) is None

    def test_metadata_unavailable_returns_none(self):
        from bubble import clone

        with patch.object(clone, "_get_pr_metadata", return_value=None):
            assert clone.pr_fork_repo(_pr_target()) is None


class TestResolvePushRepos:
    def test_explicit_allow_push_only(self):
        from bubble.cli import _resolve_push_repos

        repos = _resolve_push_repos(_repo_target(), ["Contributor/TauCeti"])
        assert repos == ["contributor/tauceti"]

    def test_malformed_allow_push_skipped(self):
        from bubble.cli import _resolve_push_repos

        repos = _resolve_push_repos(_repo_target(), ["noslash", "a/b/c", "good/fork"])
        assert repos == ["good/fork"]

    def test_combines_flag_and_pr_fork(self):
        from bubble import cli

        with patch.object(cli, "pr_fork_repo", return_value="contributor/TauCeti"):
            repos = cli._resolve_push_repos(_pr_target(), ["explicit/fork"])
        assert repos == ["explicit/fork", "contributor/tauceti"]

    def test_dedupes_flag_and_pr_fork(self):
        from bubble import cli

        with patch.object(cli, "pr_fork_repo", return_value="Contributor/TauCeti"):
            repos = cli._resolve_push_repos(_pr_target(), ["contributor/tauceti"])
        assert repos == ["contributor/tauceti"]

    def test_no_target_no_flags(self):
        from bubble.cli import _resolve_push_repos

        assert _resolve_push_repos(None, ()) == []

    def test_reuses_supplied_pr_meta_without_refetch(self):
        from bubble import clone

        pr_meta = ("branch", "contributor/TauCeti", "https://github.com/x.git")
        with patch.object(clone, "_get_pr_metadata") as mock_fetch:
            repos = clone.pr_fork_repo(_pr_target(), pr_meta)
        assert repos == "contributor/TauCeti"
        mock_fetch.assert_not_called()
