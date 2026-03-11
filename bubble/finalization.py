"""Post-clone finalization: hooks, SSH, registration, and editor launch."""

import json
import shlex
from pathlib import Path

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
    claude_prompt="",
    gh_token=False,
):
    """Post-clone setup: hooks, SSH, registration, and attach."""
    q_short = shlex.quote(short)
    project_dir = f"/home/user/{short}"
    if hook:
        hook.post_clone(runtime, name, project_dir)

    # Inject GitHub token if requested
    if gh_token:
        from .github_token import setup_gh_token

        setup_gh_token(runtime, name, machine_readable=machine_readable)

    # Inject Claude Code task if prompt is provided
    if claude_prompt:
        from .claude import inject_claude_task

        inject_claude_task(runtime, name, project_dir, claude_prompt, quiet=machine_readable)

    if not machine_readable:
        click.echo("Setting up SSH access...")
    setup_ssh(runtime, name, host_key_trust=is_enabled(config, "host_key_trust"))
    setup_git_config(runtime, name, git_name, git_email)

    commit = ""
    try:
        commit = runtime.exec(
            name,
            ["su", "-", "user", "-c", f"cd /home/user/{q_short} && git rev-parse HEAD"],
        ).strip()
    except Exception:
        pass

    register_bubble(
        name,
        t.org_repo,
        branch=checkout_branch or (t.ref if t.kind == "branch" else ""),
        commit=commit,
        pr=int(t.ref) if t.kind == "pr" else 0,
        base_image=image_name,
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

    click.echo(f"Bubble '{name}' created successfully.")
    click.echo(f"  SSH: ssh bubble-{name}")

    if not no_interactive:
        echo_editor_opening(editor)
        open_editor(editor, name, project_dir, workspace_file=workspace_file, command=command)


def echo_editor_opening(editor: str):
    """Print a status message for the editor being opened."""
    labels = {
        "vscode": "Opening VSCode...",
        "emacs": "Opening Emacs...",
        "neovim": "Opening Neovim...",
        "shell": "Connecting via SSH...",
    }
    click.echo(labels.get(editor, f"Opening {editor}..."))


def machine_readable_output(status: str, name: str, **kwargs):
    """Output JSON for --machine-readable mode."""
    data = {"status": status, "name": name}
    data.update({k: v for k, v in kwargs.items() if v is not None})
    click.echo(json.dumps(data))


def inject_local_ssh_keys(remote_host, container_name: str):
    """Inject local SSH public keys into a remote container's authorized_keys.

    The remote `bubble open` only injects the remote host's keys. For the
    chained ProxyCommand (local → remote → container) to work, the local
    machine's keys must also be present.
    """
    from .remote import _ssh_run

    ssh_dir = Path.home() / ".ssh"
    pub_keys = []
    for key_file in ["id_ed25519.pub", "id_rsa.pub", "id_ecdsa.pub"]:
        key_path = ssh_dir / key_file
        if key_path.exists():
            pub_keys.append(key_path.read_text().strip())
    if not pub_keys:
        return

    keys_str = "\\n".join(pub_keys)
    # Append local keys to the container's authorized_keys via incus exec
    _ssh_run(
        remote_host,
        [
            "incus",
            "exec",
            container_name,
            "--",
            "su",
            "-",
            "user",
            "-c",
            f"mkdir -p ~/.ssh && chmod 700 ~/.ssh "
            f'&& printf "{keys_str}\\n" >> ~/.ssh/authorized_keys '
            f"&& chmod 600 ~/.ssh/authorized_keys",
        ],
        timeout=15,
    )
