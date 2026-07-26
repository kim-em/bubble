"""The 'images' command group: list, build, delete."""

import re
import sys
from datetime import datetime, timedelta, timezone

import click

from ..config import load_config
from ..setup import get_runtime


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
            click.echo(f"{'LOGICAL':<20} {'ALIAS':<42} {'SIZE':<12} {'CREATED':<20}")
            click.echo("-" * 98)
            for img in images:
                aliases = ", ".join(a["name"] for a in img.get("aliases", []))
                logical = (img.get("properties") or {}).get("user.bubble.logical_name", "-")
                size_mb = img.get("size", 0) / (1024 * 1024)
                created = img.get("created_at", "")[:19]
                click.echo(f"{logical:<20} {aliases:<42} {size_mb:>8.1f} MB  {created:<20}")
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
        from ..lean import LEAN_VERSION_RE

        version_str = image_name.removeprefix("lean-") if image_name.startswith("lean-") else None
        tc_match = LEAN_VERSION_RE.fullmatch(version_str) if version_str else None
        if tc_match:
            from ..images.builder import build_lean_toolchain_image

            version = tc_match.group(0)
            try:
                build_lean_toolchain_image(runtime, version, force=force)
            except Exception as e:
                click.echo(str(e), err=True)
                sys.exit(1)
        else:
            from ..images.builder import build_image

            try:
                build_image(runtime, image_name, force=force)
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
            # Logical managed name: prefer the variant desired by the current
            # recipe, then accept an unambiguous older managed variant.
            from ..images.builder import desired_image_alias

            try:
                desired = desired_image_alias(runtime, image_name)
            except ValueError:
                desired = ""
            if desired and runtime.image_exists(desired):
                runtime.image_delete(desired)
                click.echo(f"Deleted image '{image_name}' ({desired}).")
                return
            # Check if it matches a fingerprint prefix
            images = runtime.list_images()
            logical_matches = [
                img
                for img in images
                if (img.get("properties") or {}).get("user.bubble.logical_name") == image_name
            ]
            if len(logical_matches) == 1:
                runtime.image_delete(logical_matches[0]["fingerprint"])
                click.echo(f"Deleted image '{image_name}'.")
                return
            if len(logical_matches) > 1:
                click.echo(
                    f"Multiple variants exist for '{image_name}'; delete a physical alias instead.",
                    err=True,
                )
                sys.exit(1)
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

    @images_group.command("prune")
    @click.option("--older-than", type=click.IntRange(min=0), default=30, show_default=True)
    @click.option("--execute", is_flag=True, help="Actually delete the selected stale images.")
    def images_prune(older_than, execute):
        """Find stale build-keyed Bubble images; dry-run unless --execute is supplied."""
        config = load_config()
        runtime = get_runtime(config, ensure_ready=False)
        cutoff = datetime.now(timezone.utc) - timedelta(days=older_than)
        managed = []
        for img in runtime.list_images():
            props = dict(img.get("properties") or {})
            is_managed = props.get("user.bubble.managed") == "true"
            aliases = [a.get("name", "") for a in img.get("aliases", [])]
            legacy = next(
                (
                    alias
                    for alias in aliases
                    if alias in ("base", "lean", "python")
                    or re.fullmatch(r"lean-v\d+(?:[.-][A-Za-z0-9]+)*", alias)
                ),
                "",
            )
            if not is_managed and not legacy:
                continue
            if legacy:
                props["user.bubble.logical_name"] = legacy
                props["user.bubble.legacy"] = "true"
            raw_created = img.get("created_at") or ""
            try:
                created = datetime.fromisoformat(raw_created.replace("Z", "+00:00"))
            except ValueError:
                continue
            managed.append((img, props, created))

        newest: dict[str, str] = {}
        for img, props, created in managed:
            if props.get("user.bubble.legacy") == "true":
                continue
            logical = props.get("user.bubble.logical_name", "")
            current = newest.get(logical)
            if current is None:
                newest[logical] = img.get("fingerprint", "")
                continue
            current_created = next(c for i, _p, c in managed if i.get("fingerprint", "") == current)
            if created > current_created:
                newest[logical] = img.get("fingerprint", "")

        in_use = {c.image for c in runtime.list_containers() if c.image}
        desired_aliases = set()
        from ..images.builder import desired_image_alias

        for _img, props, _created in managed:
            logical = props.get("user.bubble.logical_name", "")
            if not logical or props.get("user.bubble.legacy") == "true":
                continue
            try:
                desired_aliases.add(desired_image_alias(runtime, logical))
            except ValueError:
                pass
        selected = []
        for img, props, created in managed:
            fp = img.get("fingerprint", "")
            if not fp or created >= cutoff or fp in in_use:
                continue
            aliases = {a.get("name", "") for a in img.get("aliases", [])}
            if aliases & desired_aliases:
                continue
            if newest.get(props.get("user.bubble.logical_name", "")) == fp:
                continue
            selected.append((img, props))

        if not selected:
            click.echo("No stale Bubble images to prune.")
            return
        for img, props in selected:
            aliases = ", ".join(a["name"] for a in img.get("aliases", []))
            click.echo(f"{'Deleting' if execute else 'Would delete'} {aliases}")
            if execute:
                runtime.image_delete(img["fingerprint"])
        if not execute:
            click.echo("Dry run only; rerun with --execute to delete them.")
