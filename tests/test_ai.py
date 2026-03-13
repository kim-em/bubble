"""Tests for the AI provider integration module."""

import subprocess
from unittest.mock import MagicMock, patch

from bubble.ai import (
    _DEFAULT_ISSUE_TEMPLATE,
    _DEFAULT_PR_TEMPLATE,
    AI_TASK_COMMAND,
    _load_template,
    _render_template,
    generate_issue_prompt,
    generate_pr_prompt,
    inject_ai_task,
)
from bubble.cli import _resolve_ai_prompt_locally


class TestGenerateIssuePrompt:
    def test_basic_prompt(self):
        with patch("bubble.ai.subprocess.run") as mock_run:
            # First call: fetch issue
            issue_result = MagicMock()
            issue_result.returncode = 0
            issue_result.stdout = "Fix the bug\nThe widget is broken when X happens."

            # Second call: fetch comments
            comments_result = MagicMock()
            comments_result.returncode = 0
            comments_result.stdout = "I can reproduce this."

            mock_run.side_effect = [issue_result, comments_result]

            prompt = generate_issue_prompt("owner", "repo", "42", "issue-42")

            assert prompt is not None
            assert '#42: "Fix the bug"' in prompt
            assert "The widget is broken when X happens." in prompt
            assert "I can reproduce this." in prompt
            assert "issue-42" in prompt

    def test_no_comments(self):
        with patch("bubble.ai.subprocess.run") as mock_run:
            issue_result = MagicMock()
            issue_result.returncode = 0
            issue_result.stdout = "Title\nBody text"

            comments_result = MagicMock()
            comments_result.returncode = 0
            comments_result.stdout = ""

            mock_run.side_effect = [issue_result, comments_result]

            prompt = generate_issue_prompt("owner", "repo", "1", "issue-1")

            assert prompt is not None
            assert "Comments:" not in prompt

    def test_issue_fetch_failure(self):
        with patch("bubble.ai.subprocess.run") as mock_run:
            result = MagicMock()
            result.returncode = 1
            mock_run.return_value = result

            prompt = generate_issue_prompt("owner", "repo", "999", "issue-999")
            assert prompt is None

    def test_gh_not_found(self):
        with patch("bubble.ai.subprocess.run", side_effect=FileNotFoundError):
            prompt = generate_issue_prompt("owner", "repo", "1", "issue-1")
            assert prompt is None

    def test_gh_timeout(self):
        with patch(
            "bubble.ai.subprocess.run",
            side_effect=subprocess.TimeoutExpired("gh", 15),
        ):
            prompt = generate_issue_prompt("owner", "repo", "1", "issue-1")
            assert prompt is None

    def test_comments_truncated(self):
        with patch("bubble.ai.subprocess.run") as mock_run:
            issue_result = MagicMock()
            issue_result.returncode = 0
            issue_result.stdout = "Title\nBody"

            comments_result = MagicMock()
            comments_result.returncode = 0
            comments_result.stdout = "x" * 5000

            mock_run.side_effect = [issue_result, comments_result]

            prompt = generate_issue_prompt("owner", "repo", "1", "issue-1")
            assert prompt is not None
            # Comments should be truncated to 4000 chars
            assert len(prompt) < 5000

    def test_custom_template(self, tmp_path):
        template_dir = tmp_path / "templates"
        template_dir.mkdir()
        (template_dir / "issue.txt").write_text("Issue #{issue_num} by {owner}: {title}\n{body}")

        with (
            patch("bubble.ai.TEMPLATES_DIR", template_dir),
            patch("bubble.ai.subprocess.run") as mock_run,
        ):
            issue_result = MagicMock()
            issue_result.returncode = 0
            issue_result.stdout = "Bug report\nSomething broke."

            comments_result = MagicMock()
            comments_result.returncode = 0
            comments_result.stdout = ""

            mock_run.side_effect = [issue_result, comments_result]

            prompt = generate_issue_prompt("acme", "widgets", "7", "issue-7")

            assert prompt is not None
            assert prompt == "Issue #7 by acme: Bug report\nSomething broke."

    def test_includes_owner_and_repo(self):
        """Default issue template doesn't include owner/repo, but they're available."""
        with patch("bubble.ai.subprocess.run") as mock_run:
            issue_result = MagicMock()
            issue_result.returncode = 0
            issue_result.stdout = "Title\nBody"

            comments_result = MagicMock()
            comments_result.returncode = 0
            comments_result.stdout = ""

            mock_run.side_effect = [issue_result, comments_result]

            prompt = generate_issue_prompt("myorg", "myrepo", "1", "issue-1")
            assert prompt is not None
            # Default template doesn't use {owner}/{repo} but it should still render
            assert "issue-1" in prompt


class TestGeneratePrPrompt:
    def test_basic_pr_prompt(self):
        with patch("bubble.ai.subprocess.run") as mock_run:
            pr_result = MagicMock()
            pr_result.returncode = 0
            pr_result.stdout = "Add feature X\nThis PR adds feature X to the system."
            mock_run.return_value = pr_result

            prompt = generate_pr_prompt("owner", "repo", "42", "feature-x")

            assert prompt is not None
            assert '#42: "Add feature X"' in prompt
            assert "This PR adds feature X to the system." in prompt
            assert "owner/repo" in prompt
            assert "feature-x" in prompt
            assert "CI status" in prompt

    def test_pr_fetch_failure(self):
        with patch("bubble.ai.subprocess.run") as mock_run:
            result = MagicMock()
            result.returncode = 1
            mock_run.return_value = result

            prompt = generate_pr_prompt("owner", "repo", "999", "pr-999")
            assert prompt is None

    def test_gh_not_found(self):
        with patch("bubble.ai.subprocess.run", side_effect=FileNotFoundError):
            prompt = generate_pr_prompt("owner", "repo", "1", "pr-1")
            assert prompt is None

    def test_gh_timeout(self):
        with patch(
            "bubble.ai.subprocess.run",
            side_effect=subprocess.TimeoutExpired("gh", 15),
        ):
            prompt = generate_pr_prompt("owner", "repo", "1", "pr-1")
            assert prompt is None

    def test_pr_prompt_mentions_comments_table(self):
        with patch("bubble.ai.subprocess.run") as mock_run:
            pr_result = MagicMock()
            pr_result.returncode = 0
            pr_result.stdout = "Title\nBody"
            mock_run.return_value = pr_result

            prompt = generate_pr_prompt("owner", "repo", "1", "pr-1")
            assert prompt is not None
            assert "comment" in prompt.lower()
            assert "author" in prompt.lower()

    def test_pr_prompt_mentions_ci_status(self):
        with patch("bubble.ai.subprocess.run") as mock_run:
            pr_result = MagicMock()
            pr_result.returncode = 0
            pr_result.stdout = "Title\nBody"
            mock_run.return_value = pr_result

            prompt = generate_pr_prompt("owner", "repo", "5", "pr-5")
            assert prompt is not None
            assert "CI" in prompt or "checks" in prompt.lower()

    def test_custom_template(self, tmp_path):
        template_dir = tmp_path / "templates"
        template_dir.mkdir()
        (template_dir / "pr.txt").write_text("PR #{pr_num}: {title} on {branch}")

        with (
            patch("bubble.ai.TEMPLATES_DIR", template_dir),
            patch("bubble.ai.subprocess.run") as mock_run,
        ):
            pr_result = MagicMock()
            pr_result.returncode = 0
            pr_result.stdout = "Fix typo\nFixed a typo in docs."
            mock_run.return_value = pr_result

            prompt = generate_pr_prompt("acme", "widgets", "10", "fix-typo")

            assert prompt is not None
            assert prompt == "PR #10: Fix typo on fix-typo"

    def test_empty_body(self):
        with patch("bubble.ai.subprocess.run") as mock_run:
            pr_result = MagicMock()
            pr_result.returncode = 0
            pr_result.stdout = "Title only"
            mock_run.return_value = pr_result

            prompt = generate_pr_prompt("owner", "repo", "1", "pr-1")
            assert prompt is not None
            assert "Title only" in prompt


class TestTemplateSystem:
    def test_render_template_basic(self):
        result = _render_template("Hello {name}, you have {count} items.", name="Bob", count="3")
        assert result == "Hello Bob, you have 3 items."

    def test_render_template_unknown_placeholders(self):
        result = _render_template("Hello {name}, {unknown} here.", name="Bob")
        assert result == "Hello Bob, {unknown} here."

    def test_render_template_unclosed_brace(self):
        # Unclosed brace — regex only matches {word}, so this is left as-is
        result = _render_template("Hello {name", name="Bob")
        assert result == "Hello {name"

    def test_render_attribute_access_ignored(self):
        # {title.__class__} should NOT be expanded — only simple {name} placeholders
        result = _render_template("Value: {title.__class__}", title="hello")
        assert result == "Value: {title.__class__}"

    def test_render_index_access_ignored(self):
        # {missing[0]} should be left as-is
        result = _render_template("Item: {missing[0]}", name="Bob")
        assert result == "Item: {missing[0]}"

    def test_render_format_spec_ignored(self):
        # {name:>10} should be left as-is (not a simple placeholder)
        result = _render_template("Padded: {name:>10}", name="Bob")
        assert result == "Padded: {name:>10}"

    def test_render_dot_placeholder_ignored(self):
        # {missing.attr} should be left as-is
        result = _render_template("Ref: {missing.attr}", title="hello")
        assert result == "Ref: {missing.attr}"

    def test_load_template_missing(self, tmp_path):
        with patch("bubble.ai.TEMPLATES_DIR", tmp_path):
            assert _load_template("nonexistent") is None

    def test_load_template_exists(self, tmp_path):
        (tmp_path / "issue.txt").write_text("Custom: {title}")
        with patch("bubble.ai.TEMPLATES_DIR", tmp_path):
            assert _load_template("issue") == "Custom: {title}"

    def test_load_template_permission_error(self, tmp_path):
        f = tmp_path / "bad.txt"
        f.write_text("content")
        f.chmod(0o000)
        try:
            with patch("bubble.ai.TEMPLATES_DIR", tmp_path):
                assert _load_template("bad") is None
        finally:
            f.chmod(0o644)

    def test_default_issue_template_has_required_placeholders(self):
        for placeholder in ["issue_num", "title", "body", "branch"]:
            assert f"{{{placeholder}}}" in _DEFAULT_ISSUE_TEMPLATE

    def test_default_pr_template_has_required_placeholders(self):
        for placeholder in ["pr_num", "title", "body", "branch", "owner", "repo"]:
            assert f"{{{placeholder}}}" in _DEFAULT_PR_TEMPLATE


class TestAITaskCommand:
    def test_command_reads_prompt_file(self):
        assert "ai-prompt.txt" in AI_TASK_COMMAND

    def test_command_skips_permissions(self):
        assert "--dangerously-skip-permissions" in AI_TASK_COMMAND

    def test_command_clears_api_key(self):
        assert "ANTHROPIC_API_KEY=" in AI_TASK_COMMAND

    def test_command_deletes_prompt_after(self):
        assert "rm -f .vscode/ai-prompt.txt" in AI_TASK_COMMAND


class TestInjectAITask:
    def test_calls_runtime_exec(self):
        runtime = MagicMock()
        inject_ai_task(runtime, "container-1", "/home/user/project", "Do something")

        # Should have been called multiple times:
        # 1. mkdir .vscode
        # 2. write prompt
        # 3. create/update tasks.json
        # 4. configure settings.json
        # 5. add to git exclude
        # 6. pre-trust in .claude.json (default provider is claude)
        assert runtime.exec.call_count == 6

    def test_all_calls_use_su_user(self):
        runtime = MagicMock()
        inject_ai_task(runtime, "my-container", "/home/user/project", "prompt text")

        for call in runtime.exec.call_args_list:
            args = call[0]
            assert args[0] == "my-container"
            cmd = args[1]
            assert cmd[0] == "su"
            assert cmd[1] == "-"
            assert cmd[2] == "user"

    def test_trust_script_sets_onboarding_fields(self):
        runtime = MagicMock()
        inject_ai_task(runtime, "c1", "/home/user/project", "prompt")

        # The last exec call is the trust script
        trust_call = runtime.exec.call_args_list[-1]
        script = trust_call[0][1][-1]  # the -c argument
        assert "hasCompletedOnboarding" in script
        assert "numStartups" in script
        assert "isinstance" in script  # defensive coercion

    def test_codex_provider_skips_trust(self):
        """When preferred provider is codex, no Claude trust script is run."""
        runtime = MagicMock()
        config = {"ai": {"preferred": "codex"}}
        inject_ai_task(runtime, "container-1", "/home/user/project", "Do something", config=config)

        # Without the Claude trust script, should be 5 calls (not 6)
        assert runtime.exec.call_count == 5

    def test_codex_provider_uses_codex_command(self):
        """When preferred provider is codex, the task command uses codex binary."""
        runtime = MagicMock()
        config = {"ai": {"preferred": "codex"}}
        inject_ai_task(runtime, "container-1", "/home/user/project", "Do something", config=config)

        # The tasks.json creation call (3rd call) should contain codex command
        tasks_call = runtime.exec.call_args_list[2]
        script = tasks_call[0][1][-1]  # the -c argument
        assert "codex" in script


class TestResolveAIPromptLocally:
    def test_env_var_takes_priority(self):
        with patch.dict("os.environ", {"BUBBLE_AI_PROMPT": "env prompt"}):
            result = _resolve_ai_prompt_locally("owner/repo/issues/1")
            assert result == "env prompt"

    def test_issue_target_generates_prompt(self):
        with (
            patch.dict("os.environ", {}, clear=True),
            patch("bubble.ai.subprocess.run") as mock_run,
        ):
            issue_result = MagicMock()
            issue_result.returncode = 0
            issue_result.stdout = "Fix bug\nDescription"
            comments_result = MagicMock()
            comments_result.returncode = 0
            comments_result.stdout = ""
            mock_run.side_effect = [issue_result, comments_result]

            result = _resolve_ai_prompt_locally("https://github.com/owner/repo/issues/42")
            assert "issue #42" in result
            assert "issue-42" in result

    def test_pr_target_generates_prompt(self):
        with (
            patch.dict("os.environ", {}, clear=True),
            patch("bubble.ai.subprocess.run") as mock_run,
        ):
            pr_result = MagicMock()
            pr_result.returncode = 0
            pr_result.stdout = "Add feature\nPR body"
            mock_run.return_value = pr_result

            result = _resolve_ai_prompt_locally("https://github.com/owner/repo/pull/99")
            assert "#99" in result
            assert "pr-99" in result

    def test_non_issue_target_returns_empty(self):
        with patch.dict("os.environ", {}, clear=True):
            result = _resolve_ai_prompt_locally("owner/repo")
            assert result == ""

    def test_parse_failure_returns_empty(self):
        with patch.dict("os.environ", {}, clear=True):
            result = _resolve_ai_prompt_locally("")
            assert result == ""
