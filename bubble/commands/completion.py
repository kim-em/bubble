"""Shell tab completion support."""

import os
import subprocess

import click
from click.shell_completion import get_completion_class

_SHELLS = ("bash", "zsh", "fish")

# Where persistent completion scripts are conventionally installed.
_INSTALL_PATHS = {
    "bash": {
        "darwin": "$(brew --prefix 2>/dev/null || echo /usr/local)/etc/bash_completion.d/bubble",
        "linux": "~/.local/share/bash-completion/completions/bubble",
    },
    "zsh": {
        "all": "~/.zsh/completions/_bubble",
    },
    "fish": {
        "all": "~/.config/fish/completions/bubble.fish",
    },
}


def _get_completion_script(cli, shell: str) -> str:
    """Generate the completion script for a given shell using Click's API."""
    cls = get_completion_class(shell)
    if cls is None:
        return ""
    comp = cls(cli, {}, "bubble", "_BUBBLE_COMPLETE")
    return comp.source()


def register_completion_command(main):
    """Register the completion command on the main CLI group."""

    @main.command("completion")
    @click.argument("shell", type=click.Choice(_SHELLS))
    @click.option(
        "--install",
        is_flag=True,
        help="Write the completion script to a file and show activation instructions.",
    )
    def completion(shell, install):
        """Output shell completion script for bash, zsh, or fish.

        \b
        Quick setup (eval in shell init):
          eval "$(bubble completion zsh)"
          eval "$(bubble completion bash)"
          bubble completion fish | source

        \b
        Persistent setup (recommended):
          bubble completion zsh --install
          bubble completion bash --install
          bubble completion fish --install
        """
        script = _get_completion_script(main, shell)
        if not script:
            click.echo(f"Error: could not generate {shell} completion script.", err=True)
            raise SystemExit(1)

        if not install:
            click.echo(script, nl=False)
            return

        _install_completion(shell, script)

    return completion


def _install_completion(shell: str, script: str) -> None:
    """Write the completion script to a file and print activation instructions."""
    import platform

    plat = platform.system().lower()

    if shell == "bash":
        paths = _INSTALL_PATHS["bash"]
        path = paths.get(plat, paths.get("linux", ""))
    else:
        path = _INSTALL_PATHS[shell]["all"]

    path = os.path.expanduser(path)
    # Expand $(brew --prefix ...) for bash on macOS
    if "$(" in path:
        try:
            path = subprocess.check_output(["bash", "-c", f"echo {path}"], text=True).strip()
        except (subprocess.CalledProcessError, FileNotFoundError):
            path = os.path.expanduser("~/.local/share/bash-completion/completions/bubble")

    # Warn before overwriting an existing file
    if os.path.exists(path):
        click.echo(f"Overwriting existing completion script at {path}")

    parent = os.path.dirname(path)
    os.makedirs(parent, exist_ok=True)

    with open(path, "w") as f:
        f.write(script)

    click.echo(f"Completion script written to {path}")

    if shell == "zsh":
        click.echo()
        click.echo("Ensure the completions directory is in your fpath. Add to ~/.zshrc:")
        click.echo()
        click.echo("  fpath=(~/.zsh/completions $fpath)")
        click.echo("  autoload -Uz compinit && compinit")
        click.echo()
        click.echo("Then restart your shell or run: source ~/.zshrc")
    elif shell == "bash":
        click.echo()
        click.echo("Then restart your shell or run: source " + path)
    elif shell == "fish":
        click.echo()
        click.echo("Fish will pick this up automatically on next shell start.")
