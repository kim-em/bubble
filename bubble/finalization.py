"""Post-clone finalization: hooks, SSH, registration, and editor launch."""

import json
import shlex

import click

from .container_helpers import setup_git_config, setup_ssh
from .lifecycle import register_bubble
from .security import is_enabled
from .vscode import open_editor


def finalize_bubble(
    runtime,
    name,
    t,
    hook,
    image_name,
    checkout_branch,
    short,
    network,
    config,
    editor,
    no_interactive,
    machine_readable=False,
    git_name="",
    git_email="",
    command=None,
    ai_prompt="",
    ephemeral=False,
):
    """Post-clone setup: hooks, SSH, registration, and attach.

    Note: GitHub auth (proxy or token injection) is set up BEFORE clone
    in _open_single(), not here. Network allowlisting strips github.com
    when using the auth proxy, so git must be routed through the proxy
    before any clone/fetch operations.
    """
    q_short = shlex.quote(short)
    repo_dir = f"/home/user/{short}"
    subdir = hook.project_subdir() if hook else ""
    project_dir = f"{repo_dir}/{subdir}" if subdir else repo_dir
    if hook:
        hook.post_clone(runtime, name, project_dir)

    # Add a "github" remote with SSH-format URL for gh CLI host discovery.
    # The global url.insteadOf rewrites HTTPS github.com URLs to the proxy,
    # and git remote -v applies insteadOf when displaying URLs, so gh sees
    # only proxy URLs and can't match any HTTPS remote to github.com.
    # SSH-format URLs (git@github.com:...) bypass the HTTPS insteadOf rule,
    # letting gh discover the host without needing to actually use the remote.
    if t.owner and t.repo:
        q_repo = shlex.quote(f"git@github.com:{t.owner}/{t.repo}.git")
        q_dir = shlex.quote(repo_dir)
        add_cmd = f"cd {q_dir} && git remote add github {q_repo} 2>/dev/null || true"
        try:
            runtime.exec(name, ["su", "-", "user", "-c", add_cmd])
        except Exception:
            pass

    # Pre-populate Claude Code settings to skip the first-run wizard
    from .ai import setup_claude_settings

    setup_claude_settings(runtime, name, project_dir)

    # Inject AI task if prompt is provided
    if ai_prompt:
        from .ai import inject_ai_task

        inject_ai_task(runtime, name, project_dir, ai_prompt, config=config, quiet=machine_readable)

    if not machine_readable:
        from .output import step

        step("Setting up SSH access...")
    setup_ssh(
        runtime,
        name,
        host_key_trust=is_enabled(config, "host_key_trust"),
        config=config,
    )
    setup_git_config(runtime, name, git_name, git_email)

    commit = ""
    try:
        commit = runtime.exec(
            name,
            ["su", "-", "user", "-c", f"cd /home/user/{q_short} && git rev-parse HEAD"],
        ).strip()
    except Exception:
        pass

    extra_domains = list(hook.network_domains()) if hook else []
    register_bubble(
        name,
        t.org_repo,
        branch=checkout_branch or (t.ref if t.kind == "branch" else ""),
        commit=commit,
        pr=int(t.ref) if t.kind == "pr" else 0,
        base_image=image_name,
        project_dir=project_dir,
        network_enabled=network,
        extra_domains=extra_domains,
    )

    workspace_file = hook.workspace_file(project_dir) if hook else None

    if machine_readable:
        machine_readable_output(
            "created",
            name,
            project_dir=project_dir,
            workspace_file=workspace_file,
            org_repo=t.org_repo,
            image=image_name,
            branch=checkout_branch or (t.ref if t.kind == "branch" else ""),
        )
        return

    from .container_helpers import maybe_install_automation, maybe_install_skill

    maybe_install_automation()
    maybe_install_skill()

    from .output import detail, step

    step(f"Bubble '{name}' created successfully.")
    detail(f"SSH:  ssh bubble-{name}")
    detail("List: bubble list")
    detail(f"Pop:  bubble pop {name}")

    if not no_interactive:
        echo_editor_opening(editor)
        exit_code = open_editor(
            editor, name, project_dir, workspace_file=workspace_file, command=command
        )
        if ephemeral and command:
            _ephemeral_pop_and_exit(name, exit_code)


def _ephemeral_pop_and_exit(name: str, exit_code: int):
    """Pop the bubble after an --ephemeral --command run and exit.

    Pop is best-effort: if it fails we still propagate the command's exit
    code so callers can detect command failure. On pop failure, local state
    (registry, SSH config) is preserved by destroy_bubble so the user can
    retry with `bubble pop -f`.
    """
    import sys

    from .commands.lifecycle import destroy_bubble
    from .output import detail

    detail(f"Popping ephemeral bubble '{name}'...")
    error = ""
    try:
        ok, error = destroy_bubble(name)
    except Exception as e:
        ok = False
        error = str(e)
    if not ok:
        click.echo(
            f"Warning: failed to pop ephemeral bubble '{name}': "
            f"{error or 'unknown error'}. Run 'bubble pop -f {name}' to clean up.",
            err=True,
        )
    sys.exit(exit_code)


def echo_editor_opening(editor: str):
    """Print a status message for the editor being opened."""
    from .output import step

    labels = {
        "vscode": "Opening VSCode...",
        "emacs": "Opening Emacs...",
        "neovim": "Opening Neovim...",
        "shell": "Connecting via SSH...",
    }
    step(labels.get(editor, f"Opening {editor}..."))


def machine_readable_output(status: str, name: str, **kwargs):
    """Output JSON for --machine-readable mode."""
    data = {"status": status, "name": name}
    data.update({k: v for k, v in kwargs.items() if v is not None})
    click.echo(json.dumps(data))


def inject_local_ssh_keys(remote_host, container_name: str, config: dict | None = None):
    """Inject local SSH public keys into a remote container's authorized_keys.

    The remote `bubble open` only injects the remote host's keys. For the
    chained ProxyCommand (local → remote → container) to work, the local
    machine's keys must also be present.
    """
    from .container_helpers import collect_authorized_keys
    from .remote import _ssh_run

    pub_keys = collect_authorized_keys(config)
    if not pub_keys:
        return

    # Pipe the keys via stdin rather than embedding them in the shell
    # command — public-key comments may legally contain quotes, '$',
    # backticks, etc., which would otherwise be mangled or expanded.
    keys_blob = "\n".join(pub_keys) + "\n"
    _ssh_run(
        remote_host,
        [
            "bubble",
            "internal",
            "incus-exec",
            "--with-stdin",
            container_name,
            "su",
            "-",
            "user",
            "-c",
            "mkdir -p ~/.ssh && chmod 700 ~/.ssh "
            "&& cat >> ~/.ssh/authorized_keys "
            "&& chmod 600 ~/.ssh/authorized_keys",
        ],
        timeout=15,
        input=keys_blob,
    )
