"""Visual separator management for startup notices."""

import click

SEPARATOR = "───"


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
