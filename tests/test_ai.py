"""Tests for the AI provider integration module."""

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from bubble.ai import (
    _DEFAULT_ISSUE_TEMPLATE,
    _DEFAULT_PR_TEMPLATE,
    AI_TASK_COMMAND,
    AUTONOMY_LEVELS,
    SECOND_OPINION_VALUES,
    _load_template,
    _render_template,
    _resolve_second_opinion,
    generate_issue_prompt,
    generate_pr_prompt,
    inject_ai_task,
    setup_claude_settings,
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
            # Default autonomy is "plan" so instructions mention proposing a plan
            assert "propose a plan" in prompt

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
        (template_dir / "issue.txt").write_text(
            "Issue #{issue_num} by {owner}: {title}\n{body}\n{instructions}"
        )

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

            prompt = generate_issue_prompt("acme", "widgets", "7", "issue-7", second_opinion="off")

            assert prompt is not None
            assert prompt.startswith("Issue #7 by acme: Bug report\nSomething broke.")
            assert "propose a plan" in prompt

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
            # Default template renders with the issue number
            assert "#1" in prompt


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
        for placeholder in ["issue_num", "title", "body", "instructions"]:
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
        # (Claude trust is now handled by setup_claude_settings, not here)
        assert runtime.exec.call_count == 5

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

    def test_codex_provider_same_call_count(self):
        """Codex and Claude now have the same call count (trust moved out)."""
        runtime = MagicMock()
        config = {"ai": {"preferred": "codex"}}
        inject_ai_task(runtime, "container-1", "/home/user/project", "Do something", config=config)

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

    def test_unknown_provider_raises(self):
        """Unknown provider in config raises ValueError, not silent fallback."""
        runtime = MagicMock()
        config = {"ai": {"preferred": "cluade"}}
        with pytest.raises(ValueError, match="Unknown AI provider 'cluade'"):
            inject_ai_task(
                runtime, "container-1", "/home/user/project", "Do something", config=config
            )


class TestTaskCommandValidation:
    def test_known_providers_succeed(self):
        from bubble.ai import _task_command_for

        assert "claude" in _task_command_for("claude")
        assert "codex" in _task_command_for("codex")

    def test_unknown_provider_raises(self):
        from bubble.ai import _task_command_for

        with pytest.raises(ValueError, match="Unknown AI provider"):
            _task_command_for("gemini")


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
            assert "propose a plan" in result

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


class TestAutonomyLevels:
    """Test autonomy-level-aware issue prompt generation."""

    def _make_mock_run(self):
        """Create mock subprocess results for a basic issue fetch."""
        issue_result = MagicMock()
        issue_result.returncode = 0
        issue_result.stdout = "Fix bug\nDescription of the bug."
        comments_result = MagicMock()
        comments_result.returncode = 0
        comments_result.stdout = ""
        return [issue_result, comments_result]

    def test_read_level(self):
        with patch("bubble.ai.subprocess.run") as mock_run:
            mock_run.side_effect = self._make_mock_run()
            prompt = generate_issue_prompt("o", "r", "1", "issue-1", autonomy="read")
            assert "take no further action" in prompt
            assert "branch" not in prompt.lower() or "issue-1" not in prompt

    def test_plan_level(self):
        with patch("bubble.ai.subprocess.run") as mock_run:
            mock_run.side_effect = self._make_mock_run()
            prompt = generate_issue_prompt("o", "r", "1", "issue-1", autonomy="plan")
            assert "propose a plan" in prompt
            assert "do not implement" in prompt.lower()

    def test_implement_level(self):
        with patch("bubble.ai.subprocess.run") as mock_run:
            mock_run.side_effect = self._make_mock_run()
            prompt = generate_issue_prompt("o", "r", "1", "issue-1", autonomy="implement")
            assert "implement" in prompt.lower()
            assert "issue-1" in prompt
            assert "Do not commit" in prompt

    def test_pr_level(self):
        with patch("bubble.ai.subprocess.run") as mock_run:
            mock_run.side_effect = self._make_mock_run()
            prompt = generate_issue_prompt("o", "r", "1", "issue-1", autonomy="pr")
            assert "implement" in prompt.lower()
            assert "open a PR" in prompt
            assert "issue-1" in prompt

    def test_merge_level(self):
        with patch("bubble.ai.subprocess.run") as mock_run:
            mock_run.side_effect = self._make_mock_run()
            prompt = generate_issue_prompt("o", "r", "1", "issue-1", autonomy="merge")
            assert "merge" in prompt.lower()
            assert "CI" in prompt or "ci" in prompt.lower()

    def test_invalid_autonomy_defaults_to_plan(self):
        with patch("bubble.ai.subprocess.run") as mock_run:
            mock_run.side_effect = self._make_mock_run()
            prompt = generate_issue_prompt("o", "r", "1", "issue-1", autonomy="bogus")
            assert "propose a plan" in prompt

    def test_autonomy_levels_tuple(self):
        assert AUTONOMY_LEVELS == ("read", "plan", "implement", "pr", "merge")

    def test_second_opinion_values_tuple(self):
        assert SECOND_OPINION_VALUES == ("auto", "on", "off")


class TestSecondOpinion:
    def _make_mock_run(self):
        issue_result = MagicMock()
        issue_result.returncode = 0
        issue_result.stdout = "Title\nBody"
        comments_result = MagicMock()
        comments_result.returncode = 0
        comments_result.stdout = ""
        return [issue_result, comments_result]

    def test_second_opinion_on(self):
        with patch("bubble.ai.subprocess.run") as mock_run:
            mock_run.side_effect = self._make_mock_run()
            prompt = generate_issue_prompt("o", "r", "1", "b", second_opinion="on")
            assert "second opinion" in prompt.lower()

    def test_second_opinion_off(self):
        with patch("bubble.ai.subprocess.run") as mock_run:
            mock_run.side_effect = self._make_mock_run()
            prompt = generate_issue_prompt("o", "r", "1", "b", second_opinion="off")
            assert "second opinion" not in prompt.lower()

    def test_second_opinion_auto_with_codex_tool(self):
        """auto mode uses tool resolution when config is provided."""
        with patch("bubble.ai.subprocess.run") as mock_run:
            mock_run.side_effect = self._make_mock_run()
            config = {"tools": {"codex": "yes"}}
            prompt = generate_issue_prompt("o", "r", "1", "b", second_opinion="auto", config=config)
            assert "second opinion" in prompt.lower()

    def test_second_opinion_auto_without_codex_tool(self):
        """auto mode disabled when codex tool is explicitly off."""
        with patch("bubble.ai.subprocess.run") as mock_run:
            mock_run.side_effect = self._make_mock_run()
            config = {"tools": {"codex": "no"}}
            prompt = generate_issue_prompt("o", "r", "1", "b", second_opinion="auto", config=config)
            assert "second opinion" not in prompt.lower()

    def test_second_opinion_auto_fallback_no_config(self):
        """auto mode falls back to shutil.which when no config provided."""
        with (
            patch("bubble.ai.subprocess.run") as mock_run,
            patch("shutil.which", return_value="/usr/bin/codex"),
        ):
            mock_run.side_effect = self._make_mock_run()
            prompt = generate_issue_prompt("o", "r", "1", "b", second_opinion="auto")
            assert "second opinion" in prompt.lower()

    def test_resolve_second_opinion_on(self):
        assert _resolve_second_opinion("on") is True

    def test_resolve_second_opinion_off(self):
        assert _resolve_second_opinion("off") is False

    def test_resolve_second_opinion_auto_with_config(self):
        config = {"tools": {"codex": "yes"}}
        assert _resolve_second_opinion("auto", config=config) is True
        config_no = {"tools": {"codex": "no"}}
        assert _resolve_second_opinion("auto", config=config_no) is False

    def test_resolve_second_opinion_auto_without_config(self):
        with patch("shutil.which", return_value="/usr/bin/codex"):
            assert _resolve_second_opinion("auto") is True
        with patch("shutil.which", return_value=None):
            assert _resolve_second_opinion("auto") is False


class TestSetupClaudeSettings:
    """Tests for setup_claude_settings — pre-populating ~/.claude.json."""

    def test_writes_claude_json(self):
        """Should exec a command to write ~/.claude.json in the container."""
        runtime = MagicMock()
        with patch("bubble.ai.Path.home") as mock_home:
            mock_home.return_value = Path("/nonexistent")
            setup_claude_settings(runtime, "container-1", "/home/user/project")

        assert runtime.exec.call_count == 1
        call = runtime.exec.call_args_list[0]
        assert call[0][0] == "container-1"
        cmd = call[0][1]
        assert cmd[0] == "su"
        assert "~/.claude.json" in cmd[-1]

    def test_sets_onboarding_complete(self):
        """Should set hasCompletedOnboarding=True even without host file."""
        runtime = MagicMock()
        with patch("bubble.ai.Path.home") as mock_home:
            mock_home.return_value = Path("/nonexistent")
            setup_claude_settings(runtime, "c1", "/home/user/project")

        script = runtime.exec.call_args_list[0][0][1][-1]
        assert "hasCompletedOnboarding" in script

    def test_copies_allowlisted_settings(self, tmp_path):
        """Should copy only allowlisted settings from host ~/.claude.json."""
        host_config = {"theme": "dark", "hasCompletedOnboarding": True, "numStartups": 5}
        host_file = tmp_path / ".claude.json"
        host_file.write_text(json.dumps(host_config))

        runtime = MagicMock()
        with patch("bubble.ai.Path.home", return_value=tmp_path):
            setup_claude_settings(runtime, "c1", "/home/user/project")

        script = runtime.exec.call_args_list[0][0][1][-1]
        # Theme should be in the written JSON
        assert "dark" in script

    def test_excludes_non_allowlisted_keys(self, tmp_path):
        """Should NOT copy unknown/sensitive keys from host config."""
        host_config = {
            "theme": "dark",
            "hasCompletedOnboarding": True,
            "mcpServers": {"dangerous": {}},
            "secretApiKey": "sk-secret",
            "projects": {"/host/path": {"hasTrustDialogAccepted": True}},
        }
        host_file = tmp_path / ".claude.json"
        host_file.write_text(json.dumps(host_config))

        runtime = MagicMock()
        with patch("bubble.ai.Path.home", return_value=tmp_path):
            setup_claude_settings(runtime, "c1", "/home/user/myproject")

        script = runtime.exec.call_args_list[0][0][1][-1]
        # Allowlisted keys present
        assert "dark" in script
        # Non-allowlisted keys absent
        assert "mcpServers" not in script
        assert "secretApiKey" not in script
        assert "sk-secret" not in script
        # Host project paths absent, container project present
        assert "/host/path" not in script
        assert "/home/user/myproject" in script

    def test_trusts_project_dir(self, tmp_path):
        """Should pre-trust the container's project directory."""
        runtime = MagicMock()
        with patch("bubble.ai.Path.home", return_value=tmp_path):
            setup_claude_settings(runtime, "c1", "/home/user/repo")

        script = runtime.exec.call_args_list[0][0][1][-1]
        assert "/home/user/repo" in script
        assert "hasTrustDialogAccepted" in script

    def test_handles_corrupt_host_file(self, tmp_path):
        """Should handle corrupt host ~/.claude.json gracefully."""
        host_file = tmp_path / ".claude.json"
        host_file.write_text("not valid json{{{")

        runtime = MagicMock()
        with patch("bubble.ai.Path.home", return_value=tmp_path):
            setup_claude_settings(runtime, "c1", "/home/user/project")

        # Should still succeed with default settings
        assert runtime.exec.call_count == 1
        script = runtime.exec.call_args_list[0][0][1][-1]
        assert "hasCompletedOnboarding" in script

    def test_increments_num_startups(self, tmp_path):
        """Should increment numStartups from host value."""
        host_config = {"numStartups": 10}
        host_file = tmp_path / ".claude.json"
        host_file.write_text(json.dumps(host_config))

        runtime = MagicMock()
        with patch("bubble.ai.Path.home", return_value=tmp_path):
            setup_claude_settings(runtime, "c1", "/home/user/project")

        script = runtime.exec.call_args_list[0][0][1][-1]
        assert '"numStartups": 11' in script

    def test_handles_non_dict_json(self, tmp_path):
        """Should handle ~/.claude.json containing a non-dict (list, string, null)."""
        for content in ["[]", '"hello"', "null", "42"]:
            host_file = tmp_path / ".claude.json"
            host_file.write_text(content)

            runtime = MagicMock()
            with patch("bubble.ai.Path.home", return_value=tmp_path):
                setup_claude_settings(runtime, "c1", "/home/user/project")

            assert runtime.exec.call_count == 1
            script = runtime.exec.call_args_list[0][0][1][-1]
            assert "hasCompletedOnboarding" in script

    def test_exec_failure_is_best_effort(self):
        """Should not raise if runtime.exec fails — just warn."""
        runtime = MagicMock()
        runtime.exec.side_effect = RuntimeError("container gone")

        with patch("bubble.ai.Path.home") as mock_home:
            mock_home.return_value = Path("/nonexistent")
            # Should NOT raise
            setup_claude_settings(runtime, "c1", "/home/user/project")


class TestCustomTemplateBackcompat:
    """Custom templates without {instructions} should still get autonomy instructions."""

    def _make_mock_run(self):
        issue_result = MagicMock()
        issue_result.returncode = 0
        issue_result.stdout = "Title\nBody"
        comments_result = MagicMock()
        comments_result.returncode = 0
        comments_result.stdout = ""
        return [issue_result, comments_result]

    def test_custom_template_without_instructions_appends(self, tmp_path):
        """A legacy template missing {instructions} still gets autonomy text appended."""
        template_dir = tmp_path / "templates"
        template_dir.mkdir()
        (template_dir / "issue.txt").write_text("Issue #{issue_num}: {title}\n{body}")

        with (
            patch("bubble.ai.TEMPLATES_DIR", template_dir),
            patch("bubble.ai.subprocess.run") as mock_run,
        ):
            mock_run.side_effect = self._make_mock_run()
            prompt = generate_issue_prompt("o", "r", "1", "b", autonomy="pr")
            assert "Issue #1: Title" in prompt
            assert "open a PR" in prompt

    def test_custom_template_with_instructions_not_duplicated(self, tmp_path):
        """A template with {instructions} doesn't get them appended again."""
        template_dir = tmp_path / "templates"
        template_dir.mkdir()
        (template_dir / "issue.txt").write_text("{title}\n{instructions}")

        with (
            patch("bubble.ai.TEMPLATES_DIR", template_dir),
            patch("bubble.ai.subprocess.run") as mock_run,
        ):
            mock_run.side_effect = self._make_mock_run()
            prompt = generate_issue_prompt("o", "r", "1", "b", autonomy="pr")
            # instructions should appear exactly once
            assert prompt.count("open a PR") == 1
