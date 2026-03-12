"""The 'images' command group: list, build, delete."""

import sys

import click

from ..config import load_config
from ..setup import get_runtime


def _base_still_needs_rebuild(config: dict):
    """Return a callback that checks whether any drift marker is still stale.

    Used as ``still_needed`` for ``build_image(..., force=True)`` so that a
    concurrent rebuild that already updated the markers prevents a redundant
    second rebuild.
    """
    from ..images.builder import (
        CUSTOMIZE_HASH_FILE,
        TOOLS_HASH_FILE,
        VSCODE_COMMIT_FILE,
        customize_hash,
        get_vscode_commit,
    )
    from ..tools import resolve_tools, tools_hash

    enabled = resolve_tools(config)
    current_tools_hash = tools_hash(enabled)
    current_vscode_commit = get_vscode_commit() if "vscode" in enabled else None
    current_customize_hash = customize_hash()

    def _check() -> bool:
        # Tools hash drift?
        if TOOLS_HASH_FILE.exists():
            if TOOLS_HASH_FILE.read_text().strip() != current_tools_hash:
                return True
        else:
            return True  # never built
        # VS Code commit drift?
        if current_vscode_commit:
            if not VSCODE_COMMIT_FILE.exists():
                return True
            if VSCODE_COMMIT_FILE.read_text().strip() != current_vscode_commit:
                return True
        # Customize script drift?
        if current_customize_hash is not None or CUSTOMIZE_HASH_FILE.exists():
            stored = (
                CUSTOMIZE_HASH_FILE.read_text().strip() if CUSTOMIZE_HASH_FILE.exists() else None
            )
            if current_customize_hash != stored:
                return True
        return False

    return _check


def register_images_commands(main):
    """Register the 'images' command group on the main CLI group."""

    @main.group("images")
    def images_group():
        """Manage base images."""

    @images_group.command("list")
    def images_list():
        """List available base images."""
        config = load_config()
        runtime = get_runtime(config, ensure_ready=False)
        try:
            images = runtime.list_images()
            if not images:
                click.echo("No images. Run: bubble images build base")
                return
            click.echo(f"{'ALIAS':<25} {'SIZE':<12} {'CREATED':<20}")
            click.echo("-" * 57)
            for img in images:
                aliases = ", ".join(a["name"] for a in img.get("aliases", []))
                size_mb = img.get("size", 0) / (1024 * 1024)
                created = img.get("created_at", "")[:19]
                click.echo(f"{aliases:<25} {size_mb:>8.1f} MB  {created:<20}")
        except Exception as e:
            click.echo(f"Error listing images: {e}", err=True)

    @images_group.command("build")
    @click.argument("image_name", default="base")
    @click.option("--force", is_flag=True, help="Delete and rebuild even if image exists.")
    def images_build(image_name, force):
        """Build an image (base, lean, or lean-v4.X.Y for a specific toolchain)."""
        config = load_config()
        runtime = get_runtime(config)

        # Parse toolchain images: lean-v4.X.Y
        import re

        tc_match = re.fullmatch(r"lean-(v\d+\.\d+\.\d+(?:-rc\d+)?)", image_name)
        if tc_match:
            from ..images.builder import build_lean_toolchain_image

            version = tc_match.group(1)
            try:
                build_lean_toolchain_image(runtime, version, force=force)
            except Exception as e:
                click.echo(str(e), err=True)
                sys.exit(1)
        else:
            from ..images.builder import build_image

            still_needed = None
            if force and image_name == "base":
                still_needed = _base_still_needs_rebuild(config)

            try:
                build_image(runtime, image_name, force=force, still_needed=still_needed)
            except ValueError as e:
                click.echo(str(e), err=True)
                sys.exit(1)

    @images_group.command("delete")
    @click.argument("image_name", required=False)
    @click.option("--all", "delete_all", is_flag=True, help="Delete all images.")
    def images_delete(image_name, delete_all):
        """Delete an image by alias or fingerprint, or --all to delete all images."""
        config = load_config()
        runtime = get_runtime(config, ensure_ready=False)
        if delete_all:
            images = runtime.list_images()
            if not images:
                click.echo("No images to delete.")
                return
            runtime.image_delete_all()
            click.echo(f"Deleted {len(images)} image(s).")
            return
        if not image_name:
            click.echo("Specify an image name or use --all.", err=True)
            sys.exit(1)
        # Try alias first, then fingerprint prefix
        if not runtime.image_exists(image_name):
            # Check if it matches a fingerprint prefix
            images = runtime.list_images()
            matches = [img for img in images if img.get("fingerprint", "").startswith(image_name)]
            if len(matches) == 1:
                fp = matches[0]["fingerprint"]
                runtime.image_delete(fp)
                click.echo(f"Deleted image '{image_name}'.")
                return
            elif len(matches) > 1:
                click.echo(
                    f"Ambiguous fingerprint prefix '{image_name}' matches {len(matches)} images.",
                    err=True,
                )
                sys.exit(1)
            else:
                click.echo(f"Image '{image_name}' not found.", err=True)
                sys.exit(1)
        runtime.image_delete(image_name)
        click.echo(f"Deleted image '{image_name}'.")
