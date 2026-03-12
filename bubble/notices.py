"""Visual separator management for startup notices."""

import sys

import click

SEPARATOR = "───"


def maybe_print_welcome(notices=None):
    """Print a welcome banner on first run if stderr is a TTY.

    Call this from interactive command paths after ``load_config()`` has created
    the config file.  Relies on the ``first_run`` flag set by the ``main()``
    group callback (before ``load_config`` ran).
    """
    ctx = click.get_current_context(silent=True)
    if ctx is None or not (ctx.obj or {}).get("first_run"):
        return
    if not sys.stderr.isatty():
        return
    # Only show once per invocation
    ctx.obj["first_run"] = False
    if notices:
        notices.begin()

    from . import __version__
    from .config import CONFIG_FILE

    click.echo(
        f"bubble v{__version__} \u2014 containerized dev environments\n"
        f"Config created at {CONFIG_FILE}\n"
        "Run 'bubble -h' for usage, 'bubble doctor' to check setup.",
        err=True,
    )


class Notices:
    """Track startup notice groups and print separators between them.

    Call ``begin()`` before each logically distinct notice group.  The first
    call is a no-op; subsequent calls print a thin horizontal separator so
    messages from different subsystems are visually distinct.
    """

    def __init__(self):
        self._printed = False

    @property
    def has_output(self) -> bool:
        """Whether any notice group has been started."""
        return self._printed

    def begin(self):
        """Start a new notice group, printing a separator if needed."""
        if self._printed:
            click.echo(SEPARATOR, err=True)
        self._printed = True

    def finish(self):
        """Print a final separator after all notice groups (if any)."""
        if self._printed:
            click.echo(SEPARATOR, err=True)
