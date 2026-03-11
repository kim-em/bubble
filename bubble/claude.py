"""Claude Code integration for bubble containers."""

import json
import shlex
import subprocess

import click

from .runtime.base import ContainerRuntime

# Claude task command: reads prompt from file, runs Claude with skip-permissions,
# then deletes the prompt file so reopening is clean.
CLAUDE_TASK_COMMAND = (
    "test -f .vscode/claude-prompt.txt && ANTHROPIC_API_KEY= CLAUDECODE="
    ' claude --dangerously-skip-permissions "$(cat .vscode/claude-prompt.txt)"'
    " && rm -f .vscode/claude-prompt.txt"
)


def generate_issue_prompt(owner: str, repo: str, issue_num: str, branch: str) -> str | None:
    """Fetch GitHub issue details and generate a Claude prompt.

    Returns the prompt string, or None if the issue can't be fetched.
    """
    try:
        result = subprocess.run(
            [
                "gh",
                "api",
                f"repos/{owner}/{repo}/issues/{issue_num}",
                "--jq",
                ".title,.body",
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode != 0:
            return None
        lines = result.stdout.split("\n", 1)
        title = lines[0] if lines else ""
        body = lines[1].strip() if len(lines) > 1 else ""
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None

    # Fetch comments (first 4000 chars)
    comments_text = ""
    try:
        result = subprocess.run(
            [
                "gh",
                "api",
                f"repos/{owner}/{repo}/issues/{issue_num}/comments",
                "--jq",
                ".[].body",
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode == 0 and result.stdout.strip():
            comments_text = result.stdout.strip()[:4000]
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    prompt = (
        f'Please read and understand GitHub issue #{issue_num}: "{title}".\n'
        f"\n"
        f"Issue description:\n"
        f"{body}\n"
    )
    if comments_text:
        prompt += f"\nComments:\n{comments_text}\n"
    prompt += (
        f"\nPlease claim this issue (assign it to yourself if possible), "
        f"then implement a fix or feature as described. "
        f"Work on a branch named `{branch}`, and open a PR when done."
    )
    return prompt


def inject_claude_task(runtime: ContainerRuntime, container: str, project_dir: str, prompt: str):
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

    # Pre-trust the project directory in .claude.json
    trust_script = (
        f'python3 -c "'
        f"import json,os; "
        f"p=os.path.expanduser('~/.claude.json'); "
        f"d=json.load(open(p)) if os.path.exists(p) else {{}}; "
        f"d.setdefault('projects',{{}}); "
        f"proj=d['projects'].setdefault({shlex.quote(project_dir)!r},{{}}); "  # noqa: E501
        f"proj['hasTrustDialogAccepted']=True; "
        f"proj.setdefault('allowedTools',[]); "
        f"json.dump(d,open(p,'w'),indent=2)"
        f'"'
    )
    runtime.exec(container, ["su", "-", "user", "-c", trust_script])

    click.echo("  Claude Code task injected (will start on VS Code folder open).")
