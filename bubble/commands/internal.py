"""Hidden 'internal' command group used by bubble-on-bubble RPC over SSH.

These subcommands exist so the local side of a remote bubble session
(``github_token.py``, ``finalization.py``) can act on the remote container
without shipping raw ``incus`` commands over SSH.  The remote bubble's own
:class:`IncusRuntime` applies the right resource prefix (e.g.
``bubble-colima:`` on macOS) without the local side having to know the
remote's runtime topology.

**Stability:** these commands are hidden from ``--help`` and not part of
the user-facing CLI.  They are an IPC, not an API.  Keep verbs narrow —
specifically, never accept opaque shell strings beyond the structured
``--`` payload of ``incus-exec``.

**Version-skew invariant:** callers must only invoke ``bubble internal …``
on a remote where the bubble package has been deployed/updated.  That is
true today for every code path in ``github_token.py`` and
``finalization.py`` because they run after a successful
``bubble open --machine-readable`` round-trip on the same host.
"""

import sys

import click

from ..config import load_config
from ..setup import get_runtime


def register_internal_commands(main):
    """Register the hidden 'internal' command group on the main CLI."""

    @main.group("internal", hidden=True)
    def internal():
        """Hidden RPC entry points used by bubble-on-bubble over SSH."""

    @internal.command(
        "incus-exec",
        # Pass everything after CONTAINER straight to the container.  Without
        # this Click would try to parse '-c', '-l', '--color=auto', etc. as
        # its own options.
        context_settings={"ignore_unknown_options": True, "allow_interspersed_args": False},
    )
    @click.argument("container")
    @click.argument("argv", nargs=-1, type=click.UNPROCESSED, required=True)
    def incus_exec(container, argv):
        """Run ARGV inside CONTAINER via the remote bubble's runtime.

        Equivalent to ``incus exec CONTAINER -- ARGV…`` on the remote, but
        goes through :class:`bubble.runtime.incus.IncusRuntime` so the
        appropriate remote prefix is applied without the caller having to
        know about it.

        Stdout is forwarded to our stdout; runtime errors are surfaced as a
        non-zero exit code.
        """
        config = load_config()
        runtime = get_runtime(config, ensure_ready=False)
        try:
            output = runtime.exec(container, list(argv))
        except RuntimeError as exc:
            click.echo(str(exc), err=True)
            sys.exit(1)
        if output:
            click.echo(output)

    @internal.command("incus-add-device")
    @click.argument("container")
    @click.argument("device_name")
    @click.argument("device_type")
    @click.argument("props", nargs=-1)
    def incus_add_device(container, device_name, device_type, props):
        """Attach a device to CONTAINER via the remote bubble's runtime.

        PROPS are ``KEY=VALUE`` pairs forwarded as keyword arguments to
        :meth:`bubble.runtime.base.ContainerRuntime.add_device`.  Anything
        without an ``=`` is rejected — this command does not accept opaque
        shell fragments.
        """
        config = load_config()
        runtime = get_runtime(config, ensure_ready=False)
        kwargs: dict[str, str] = {}
        for prop in props:
            if "=" not in prop:
                click.echo(
                    f"Invalid prop {prop!r}: expected key=value (no '=' found).",
                    err=True,
                )
                sys.exit(2)
            key, _, value = prop.partition("=")
            if not key:
                click.echo(f"Invalid prop {prop!r}: empty key.", err=True)
                sys.exit(2)
            kwargs[key] = value
        try:
            runtime.add_device(container, device_name, device_type, **kwargs)
        except RuntimeError as exc:
            click.echo(str(exc), err=True)
            sys.exit(1)
