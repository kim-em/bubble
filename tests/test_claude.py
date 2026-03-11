"""Tests for the Claude Code integration module."""

import subprocess
from unittest.mock import MagicMock, patch

from bubble.claude import CLAUDE_TASK_COMMAND, generate_issue_prompt, inject_claude_task


class TestGenerateIssuePrompt:
    def test_basic_prompt(self):
        with patch("bubble.claude.subprocess.run") as mock_run:
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
        with patch("bubble.claude.subprocess.run") as mock_run:
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
        with patch("bubble.claude.subprocess.run") as mock_run:
            result = MagicMock()
            result.returncode = 1
            mock_run.return_value = result

            prompt = generate_issue_prompt("owner", "repo", "999", "issue-999")
            assert prompt is None

    def test_gh_not_found(self):
        with patch("bubble.claude.subprocess.run", side_effect=FileNotFoundError):
            prompt = generate_issue_prompt("owner", "repo", "1", "issue-1")
            assert prompt is None

    def test_gh_timeout(self):
        with patch(
            "bubble.claude.subprocess.run",
            side_effect=subprocess.TimeoutExpired("gh", 15),
        ):
            prompt = generate_issue_prompt("owner", "repo", "1", "issue-1")
            assert prompt is None

    def test_comments_truncated(self):
        with patch("bubble.claude.subprocess.run") as mock_run:
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


class TestClaudeTaskCommand:
    def test_command_reads_prompt_file(self):
        assert "claude-prompt.txt" in CLAUDE_TASK_COMMAND

    def test_command_skips_permissions(self):
        assert "--dangerously-skip-permissions" in CLAUDE_TASK_COMMAND

    def test_command_clears_api_key(self):
        assert "ANTHROPIC_API_KEY=" in CLAUDE_TASK_COMMAND

    def test_command_deletes_prompt_after(self):
        assert "rm -f .vscode/claude-prompt.txt" in CLAUDE_TASK_COMMAND


class TestInjectClaudeTask:
    def test_calls_runtime_exec(self):
        runtime = MagicMock()
        inject_claude_task(runtime, "container-1", "/home/user/project", "Do something")

        # Should have been called multiple times:
        # 1. mkdir .vscode
        # 2. write prompt
        # 3. create/update tasks.json
        # 4. configure settings.json
        # 5. add to git exclude
        # 6. pre-trust in .claude.json
        assert runtime.exec.call_count == 6

    def test_all_calls_use_su_user(self):
        runtime = MagicMock()
        inject_claude_task(runtime, "my-container", "/home/user/project", "prompt text")

        for call in runtime.exec.call_args_list:
            args = call[0]
            assert args[0] == "my-container"
            cmd = args[1]
            assert cmd[0] == "su"
            assert cmd[1] == "-"
            assert cmd[2] == "user"

    def test_trust_script_sets_onboarding_fields(self):
        runtime = MagicMock()
        inject_claude_task(runtime, "c1", "/home/user/project", "prompt")

        # The last exec call is the trust script
        trust_call = runtime.exec.call_args_list[-1]
        script = trust_call[0][1][-1]  # the -c argument
        assert "hasCompletedOnboarding" in script
        assert "numStartups" in script
        assert "isinstance" in script  # defensive coercion
