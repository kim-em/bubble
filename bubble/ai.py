"""AI provider integration for bubble containers.

Handles prompt generation and task injection for the configured preferred
AI provider (default: Claude Code). Provider-neutral where possible;
provider-specific details (binary names, env vars, prompt file paths)
are dispatched via the ``[ai] preferred`` config key.
"""

import json
import re
import shlex
import subprocess
from pathlib import Path

from .config import DATA_DIR
from .runtime.base import ContainerRuntime

# Provider-specific task commands.
# Each reads the prompt from .vscode/ai-prompt.txt, runs the AI tool
# with autonomous permissions, then deletes the prompt file.
_TASK_COMMANDS = {
    "claude": (
        "test -f .vscode/ai-prompt.txt && ANTHROPIC_API_KEY= CLAUDECODE="
        ' claude --dangerously-skip-permissions "$(cat .vscode/ai-prompt.txt)"'
        " && rm -f .vscode/ai-prompt.txt"
    ),
    "codex": (
        "test -f .vscode/ai-prompt.txt &&"
        ' codex --approval-mode full-auto "$(cat .vscode/ai-prompt.txt)"'
        " && rm -f .vscode/ai-prompt.txt"
    ),
}

SUPPORTED_PROVIDERS = frozenset(_TASK_COMMANDS)


def _task_command_for(provider: str) -> str:
    """Return the VS Code task shell command for the given AI provider.

    Raises ``ValueError`` for unknown providers so typos are caught early.
    """
    try:
        return _TASK_COMMANDS[provider]
    except KeyError:
        supported = ", ".join(sorted(SUPPORTED_PROVIDERS))
        raise ValueError(f"Unknown AI provider {provider!r} (supported: {supported})") from None


AI_TASK_COMMAND = _TASK_COMMANDS["claude"]

TEMPLATES_DIR = DATA_DIR / "templates"

# Ordered autonomy levels. Each level implies all lower levels.
AUTONOMY_LEVELS = ("read", "plan", "implement", "pr", "merge")

# Valid values for the second_opinion setting.
SECOND_OPINION_VALUES = ("auto", "on", "off")

# Issue prompt instructions keyed by autonomy level.
_ISSUE_INSTRUCTIONS = {
    "read": (
        "Please read and understand the issue. "
        "Summarize the problem and any relevant context, but take no further action."
    ),
    "plan": (
        "Please read and understand the issue, then propose a plan to fix it. "
        "Describe what files need to change and how, but do not implement anything yet."
    ),
    "implement": (
        "Please claim this issue (assign it to yourself if possible), "
        "then implement a fix or feature as described. "
        "Work on a branch named `{branch}`. Do not commit or open a PR."
    ),
    "pr": (
        "Please claim this issue (assign it to yourself if possible), "
        "then implement a fix or feature as described. "
        "Work on a branch named `{branch}`, and open a PR when done."
    ),
    "merge": (
        "Please claim this issue (assign it to yourself if possible), "
        "then implement a fix or feature as described. "
        "Work on a branch named `{branch}`, open a PR, "
        "rebase onto the default branch, watch CI, and merge when it passes."
    ),
}

_SECOND_OPINION_SUFFIX = (
    "\n\nBefore proceeding, get a second opinion from another AI "
    "(e.g. Codex) to review your approach."
)

# Default issue prompt template. Placeholders:
#   {owner}, {repo}, {issue_num}, {title}, {body}, {comments},
#   {comments_section} (pre-formatted, empty when no comments), {branch},
#   {instructions}
_DEFAULT_ISSUE_TEMPLATE = (
    'Please read and understand GitHub issue #{issue_num}: "{title}".\n'
    "\n"
    "Issue description:\n"
    "{body}\n"
    "{comments_section}"
    "\n{instructions}"
)

# Default PR prompt template. Placeholders:
#   {owner}, {repo}, {pr_num}, {title}, {body}, {branch}
_DEFAULT_PR_TEMPLATE = (
    'You are working on PR #{pr_num}: "{title}" in {owner}/{repo}'
    " on branch `{branch}`.\n"
    "\n"
    "PR description:\n"
    "{body}\n"
    "\n"
    "Please:\n"
    "1. Check the CI status for this PR using the GitHub API.\n"
    "2. Build a numbered table of all PR comments (both review-level and"
    " inline) with columns for: comment number, author, a summary of the"
    " comment, and whether it has a response yet.\n"
    "\n"
    "This gives an overview of where things stand with this PR."
)


def _load_template(kind: str) -> str | None:
    """Load a custom template from ~/.bubble/templates/<kind>.txt.

    Returns the template string, or None if no custom template exists.
    Falls back gracefully on permission errors or encoding issues.
    """
    path = TEMPLATES_DIR / f"{kind}.txt"
    if path.is_file():
        try:
            return path.read_text()
        except (OSError, UnicodeError):
            from .output import detail

            detail(f"Warning: could not read template {path}, using default.", err=True)
    return None


# Matches simple {name} placeholders — no attribute access, indexing, or format specs.
_PLACEHOLDER_RE = re.compile(r"\{(\w+)\}")


def _render_template(template: str, **kwargs) -> str:
    """Render a template by substituting simple {name} placeholders.

    Only plain identifiers are replaced (e.g. {title}, {pr_num}).
    Attribute access ({x.y}), indexing ({x[0]}), and format specs ({x:>10})
    are left untouched. Unknown placeholders are also left as-is.
    """

    def _replace(m: re.Match) -> str:
        key = m.group(1)
        if key in kwargs:
            return str(kwargs[key])
        return m.group(0)  # leave unknown placeholders as-is

    return _PLACEHOLDER_RE.sub(_replace, template)


def _fetch_github_item(owner: str, repo: str, endpoint: str, jq: str) -> str | None:
    """Fetch a GitHub API endpoint via gh CLI, returning stdout or None on failure."""
    try:
        result = subprocess.run(
            ["gh", "api", f"repos/{owner}/{repo}/{endpoint}", "--jq", jq],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode != 0:
            return None
        return result.stdout
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None


def _resolve_second_opinion(second_opinion: str, config: dict | None = None) -> bool:
    """Resolve the second_opinion setting to a boolean.

    'on' → True, 'off' → False, 'auto' → True if codex will be
    available in the container (checked via tool resolution, not host PATH).
    """
    if second_opinion == "on":
        return True
    if second_opinion == "off":
        return False
    # auto: check if codex is resolved as an enabled tool
    if config is not None:
        from .tools import resolve_tools

        return "codex" in resolve_tools(config)
    # Fallback when no config available: check host PATH
    import shutil

    return shutil.which("codex") is not None


def generate_issue_prompt(
    owner: str,
    repo: str,
    issue_num: str,
    branch: str,
    autonomy: str = "plan",
    second_opinion: str = "auto",
    config: dict | None = None,
) -> str | None:
    """Fetch GitHub issue details and generate an AI prompt.

    Returns the prompt string, or None if the issue can't be fetched.
    Uses a custom template from ~/.bubble/templates/issue.txt if present,
    otherwise falls back to the built-in default.

    The autonomy level controls what action instructions are included:
      read, plan, implement, pr, merge.
    """
    raw = _fetch_github_item(owner, repo, f"issues/{issue_num}", ".title,.body")
    if raw is None:
        return None
    lines = raw.split("\n", 1)
    title = lines[0] if lines else ""
    body = lines[1].strip() if len(lines) > 1 else ""

    # Fetch comments (first 4000 chars)
    comments_text = ""
    raw_comments = _fetch_github_item(owner, repo, f"issues/{issue_num}/comments", ".[].body")
    if raw_comments and raw_comments.strip():
        comments_text = raw_comments.strip()[:4000]

    comments_section = ""
    if comments_text:
        comments_section = f"\nComments:\n{comments_text}\n"

    # Build instructions from autonomy level
    if autonomy not in AUTONOMY_LEVELS:
        autonomy = "plan"
    instructions = _ISSUE_INSTRUCTIONS[autonomy]
    # The instructions may contain {branch} placeholder
    instructions = instructions.format(branch=branch)

    # Append second opinion request if enabled
    if _resolve_second_opinion(second_opinion, config=config):
        instructions += _SECOND_OPINION_SUFFIX

    custom_template = _load_template("issue")
    template = custom_template or _DEFAULT_ISSUE_TEMPLATE

    prompt = _render_template(
        template,
        owner=owner,
        repo=repo,
        issue_num=issue_num,
        title=title,
        body=body,
        comments=comments_text,
        comments_section=comments_section,
        branch=branch,
        instructions=instructions,
    )

    # If a custom template didn't use {instructions}, append them so
    # autonomy/second-opinion settings are never silently lost.
    if custom_template and "{instructions}" not in custom_template:
        prompt = prompt.rstrip() + "\n\n" + instructions

    return prompt


def generate_pr_prompt(owner: str, repo: str, pr_num: str, branch: str) -> str | None:
    """Fetch GitHub PR details and generate an AI prompt.

    Returns the prompt string, or None if the PR can't be fetched.
    Uses a custom template from ~/.bubble/templates/pr.txt if present,
    otherwise falls back to the built-in default.
    """
    raw = _fetch_github_item(owner, repo, f"pulls/{pr_num}", ".title,.body")
    if raw is None:
        return None
    lines = raw.split("\n", 1)
    title = lines[0] if lines else ""
    body = lines[1].strip() if len(lines) > 1 else ""

    template = _load_template("pr") or _DEFAULT_PR_TEMPLATE
    return _render_template(
        template,
        owner=owner,
        repo=repo,
        pr_num=pr_num,
        title=title,
        body=body,
        branch=branch,
    )


# Known-safe keys to copy from the host's ~/.claude.json.
# Only cosmetic/UX settings — no credentials, MCP config, or host-specific paths.
_CLAUDE_JSON_SAFE_KEYS = frozenset(
    {
        "theme",
        "hasCompletedOnboarding",
        "numStartups",
        "preferredNotifChannel",
        "autoUpdaterStatus",
        "effortCalloutV2Dismissed",
    }
)


def setup_claude_settings(
    runtime: ContainerRuntime,
    container: str,
    project_dir: str,
):
    """Pre-populate ~/.claude.json in the container to skip the first-run wizard.

    Copies allowlisted settings (theme, onboarding state, etc.) from the host's
    ~/.claude.json if it exists. Always ensures hasCompletedOnboarding=True
    and pre-trusts the project directory. This runs for ALL bubbles, not just
    those with AI task injection.

    Best-effort: failures are logged but do not abort bubble creation.
    """
    host_claude_json = Path.home() / ".claude.json"

    # Extract only allowlisted keys from host settings
    settings: dict = {}
    if host_claude_json.is_file():
        try:
            host_data = json.loads(host_claude_json.read_text())
            if isinstance(host_data, dict):
                settings = {k: v for k, v in host_data.items() if k in _CLAUDE_JSON_SAFE_KEYS}
        except (OSError, json.JSONDecodeError):
            pass

    # Ensure onboarding is marked complete
    settings["hasCompletedOnboarding"] = True
    n = settings.get("numStartups", 0)
    settings["numStartups"] = (n if isinstance(n, int) else 0) + 1

    # Pre-trust the project directory
    settings["projects"] = {
        project_dir: {"hasTrustDialogAccepted": True, "allowedTools": []},
    }

    # Write to container (best-effort — don't abort bubble creation on failure)
    settings_json = shlex.quote(json.dumps(settings, indent=2))
    try:
        runtime.exec(
            container,
            ["su", "-", "user", "-c", f"printf '%s' {settings_json} > ~/.claude.json"],
        )
    except Exception:
        from .output import detail

        detail("Warning: could not pre-populate Claude Code settings.", err=True)


def inject_ai_task(
    runtime: ContainerRuntime,
    container: str,
    project_dir: str,
    prompt: str,
    config: dict | None = None,
    quiet: bool = False,
):
    """Inject AI auto-start task into a container's VS Code configuration.

    Dispatches to the configured preferred AI provider (default: Claude).

    - Writes prompt to .vscode/ai-prompt.txt
    - Creates/updates .vscode/tasks.json with AI task (runOn: folderOpen)
    - Configures .vscode/settings.json for automatic tasks
    - Adds generated files to git exclude
    - Pre-trusts the project directory in the preferred provider's config
    """
    provider = "claude"
    if config:
        provider = config.get("ai", {}).get("preferred", "claude")

    task_command = _task_command_for(provider)

    q_dir = shlex.quote(project_dir)
    q_prompt = shlex.quote(prompt)

    # Create .vscode directory
    runtime.exec(container, ["su", "-", "user", "-c", f"mkdir -p {q_dir}/.vscode"])

    # Write prompt to file
    runtime.exec(
        container,
        ["su", "-", "user", "-c", f"printf '%s' {q_prompt} > {q_dir}/.vscode/ai-prompt.txt"],
    )

    # Create or update tasks.json with AI task
    ai_task = {
        "label": "AI",
        "type": "shell",
        "command": task_command,
        "runOptions": {"runOn": "folderOpen"},
        "presentation": {"reveal": "always", "panel": "dedicated"},
    }
    tasks_json_str = shlex.quote(json.dumps(ai_task))

    # Script: if tasks.json exists, add AI task (removing old Claude and AI labels);
    # otherwise create new file
    script = (
        f"cd {q_dir} && "
        f"if [ -f .vscode/tasks.json ]; then "
        f'  python3 -c "'
        f"import json,sys; "
        f"t=json.load(open('.vscode/tasks.json')); "
        f"t['tasks']=[x for x in t.get('tasks',[])"
        f" if x.get('label') not in ('Claude','AI')]+[json.loads(sys.argv[1])]; "
        f"json.dump(t,open('.vscode/tasks.json','w'),indent=2)"
        f'" {tasks_json_str}; '
        f"else "
        f'  python3 -c "'
        f"import json,sys; "
        f"json.dump({{'version':'2.0.0','tasks':[json.loads(sys.argv[1])]}},open('.vscode/tasks.json','w'),indent=2)"
        f'" {tasks_json_str}; '
        f"fi"
    )
    runtime.exec(container, ["su", "-", "user", "-c", script])

    # Configure settings.json for automatic tasks
    settings_script = (
        f"cd {q_dir} && "
        f'python3 -c "'
        f"import json,os; "
        f"p='.vscode/settings.json'; "
        f"s=json.load(open(p)) if os.path.exists(p) else {{}}; "
        f"s['terminal.integrated.defaultLocation']='editor'; "
        f"s['task.allowAutomaticTasks']='on'; "
        f"json.dump(s,open(p,'w'),indent=2)"
        f'"'
    )
    runtime.exec(container, ["su", "-", "user", "-c", settings_script])

    # Add generated files to git exclude
    exclude_script = (
        f"cd {q_dir} && "
        f"GIT_DIR=$(git rev-parse --git-dir) && "
        f"mkdir -p $GIT_DIR/info && "
        f"for f in .vscode/ai-prompt.txt .vscode/claude-prompt.txt"
        f" .vscode/settings.json .vscode/tasks.json; do "
        f'  grep -qxF "$f" $GIT_DIR/info/exclude 2>/dev/null'
        f' || echo "$f" >> $GIT_DIR/info/exclude; '
        f"done"
    )
    runtime.exec(container, ["su", "-", "user", "-c", exclude_script])

    if not quiet:
        from .output import detail

        label = provider.capitalize()
        detail(f"{label} task injected (will start on VS Code folder open).")
