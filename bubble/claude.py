"""Claude Code integration for bubble containers."""

import json
import re
import shlex
import subprocess

import click

from .config import DATA_DIR
from .runtime.base import ContainerRuntime

# Claude task command: reads prompt from file, runs Claude with skip-permissions,
# then deletes the prompt file so reopening is clean.
CLAUDE_TASK_COMMAND = (
    "test -f .vscode/claude-prompt.txt && ANTHROPIC_API_KEY= CLAUDECODE="
    ' claude --dangerously-skip-permissions "$(cat .vscode/claude-prompt.txt)"'
    " && rm -f .vscode/claude-prompt.txt"
)

TEMPLATES_DIR = DATA_DIR / "templates"

# Default issue prompt template. Placeholders:
#   {owner}, {repo}, {issue_num}, {title}, {body}, {comments},
#   {comments_section} (pre-formatted, empty when no comments), {branch}
_DEFAULT_ISSUE_TEMPLATE = (
    'Please read and understand GitHub issue #{issue_num}: "{title}".\n'
    "\n"
    "Issue description:\n"
    "{body}\n"
    "{comments_section}"
    "\nPlease claim this issue (assign it to yourself if possible), "
    "then implement a fix or feature as described. "
    "Work on a branch named `{branch}`, and open a PR when done."
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
            click.echo(f"  Warning: could not read template {path}, using default.", err=True)
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


def generate_issue_prompt(owner: str, repo: str, issue_num: str, branch: str) -> str | None:
    """Fetch GitHub issue details and generate a Claude prompt.

    Returns the prompt string, or None if the issue can't be fetched.
    Uses a custom template from ~/.bubble/templates/issue.txt if present,
    otherwise falls back to the built-in default.
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

    template = _load_template("issue") or _DEFAULT_ISSUE_TEMPLATE
    return _render_template(
        template,
        owner=owner,
        repo=repo,
        issue_num=issue_num,
        title=title,
        body=body,
        comments=comments_text,
        comments_section=comments_section,
        branch=branch,
    )


def generate_pr_prompt(owner: str, repo: str, pr_num: str, branch: str) -> str | None:
    """Fetch GitHub PR details and generate a Claude prompt.

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


def inject_claude_task(
    runtime: ContainerRuntime, container: str, project_dir: str, prompt: str, quiet: bool = False
):
    """Inject Claude auto-start task into a container's VS Code configuration.

    - Writes prompt to .vscode/claude-prompt.txt
    - Creates/updates .vscode/tasks.json with Claude task (runOn: folderOpen)
    - Configures .vscode/settings.json for automatic tasks
    - Adds generated files to git exclude
    - Pre-trusts the project directory in .claude.json
    """
    q_dir = shlex.quote(project_dir)
    q_prompt = shlex.quote(prompt)

    # Create .vscode directory
    runtime.exec(container, ["su", "-", "user", "-c", f"mkdir -p {q_dir}/.vscode"])

    # Write prompt to file
    runtime.exec(
        container,
        ["su", "-", "user", "-c", f"printf '%s' {q_prompt} > {q_dir}/.vscode/claude-prompt.txt"],
    )

    # Create or update tasks.json with Claude task
    claude_task = {
        "label": "Claude",
        "type": "shell",
        "command": CLAUDE_TASK_COMMAND,
        "runOptions": {"runOn": "folderOpen"},
        "presentation": {"reveal": "always", "panel": "dedicated"},
    }
    tasks_json_str = shlex.quote(json.dumps(claude_task))

    # Script: if tasks.json exists, add Claude task; otherwise create new file
    script = (
        f"cd {q_dir} && "
        f"if [ -f .vscode/tasks.json ]; then "
        f'  python3 -c "'
        f"import json,sys; "
        f"t=json.load(open('.vscode/tasks.json')); "
        f"t['tasks']=[x for x in t.get('tasks',[])"
        f" if x.get('label')!='Claude']+[json.loads(sys.argv[1])]; "
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
        f"for f in .vscode/claude-prompt.txt .vscode/settings.json .vscode/tasks.json; do "
        f'  grep -qxF "$f" $GIT_DIR/info/exclude 2>/dev/null'
        f' || echo "$f" >> $GIT_DIR/info/exclude; '
        f"done"
    )
    runtime.exec(container, ["su", "-", "user", "-c", exclude_script])

    # Pre-trust the project directory and skip onboarding in .claude.json
    trust_script = (
        f'python3 -c "'
        f"import json,os; "
        f"p=os.path.expanduser('~/.claude.json'); "
        f"d=json.load(open(p)) if os.path.exists(p) else {{}}; "
        f"d['hasCompletedOnboarding']=True; "
        f"n=d.get('numStartups',0); d['numStartups']=(n if isinstance(n,int) else 0)+1; "
        f"d.setdefault('projects',{{}}); "
        f"proj=d['projects'].setdefault({shlex.quote(project_dir)!r},{{}}); "  # noqa: E501
        f"proj['hasTrustDialogAccepted']=True; "
        f"proj.setdefault('allowedTools',[]); "
        f"json.dump(d,open(p,'w'),indent=2)"
        f'"'
    )
    runtime.exec(container, ["su", "-", "user", "-c", trust_script])

    if not quiet:
        from .output import detail

        detail("Claude Code task injected (will start on VS Code folder open).")
