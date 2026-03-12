"""Centralized output formatting for consistent indentation.

Two-level model:
- step(msg): Top-level steps, no indent
- detail(msg): Sub-details, 2-space indent
"""

import click


def step(msg: str, **kwargs):
    """Print a top-level step message (no indent)."""
    click.echo(msg, **kwargs)


def detail(msg: str, **kwargs):
    """Print a sub-detail message (2-space indent)."""
    click.echo(f"  {msg}", **kwargs)
