"""Image detection, building, and background rebuild management."""

import shutil
import subprocess
import sys

import click

from .config import load_config
from .hooks import select_hook
from .images.builder import VSCODE_COMMIT_FILE, get_vscode_commit, is_build_locked
from .runtime.base import ContainerRuntime


def _spawn_background_bubble(args: list[str], log_path: str):
    """Spawn a background bubble command, detached from the current process.

    Tries `bubble` on PATH first, falls back to `sys.executable -m bubble`.
    """
    bubble_cmd = shutil.which("bubble")
    if bubble_cmd:
        cmd = [bubble_cmd] + args
    else:
        cmd = [sys.executable, "-m", "bubble"] + args
    log_file = open(log_path, "w")
    try:
        subprocess.Popen(
            cmd,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    finally:
        log_file.close()


def maybe_rebuild_base_image():
    """If VS Code has updated since the base image was built, rebuild in background.

    Only triggers when vscode is an enabled tool — otherwise there's nothing
    to rebuild even if the host has `code` installed.
    """
    from .config import load_config
    from .tools import resolve_tools

    config = load_config()
    if "vscode" not in resolve_tools(config):
        return
    commit = get_vscode_commit()
    if not commit:
        return
    if VSCODE_COMMIT_FILE.exists() and VSCODE_COMMIT_FILE.read_text().strip() == commit:
        return
    if is_build_locked("base-vscode"):
        return
    _spawn_background_bubble(
        ["images", "build", "base"],
        "/tmp/bubble-vscode-rebuild.log",
    )


def maybe_rebuild_tools(runtime: ContainerRuntime):
    """If the resolved tool set has changed since base was built, rebuild base now.

    Rebuilds synchronously so that the container launched afterwards uses a
    fresh image with the correct tools baked in. The rebuild also purges
    derived images (lean, etc.) so they get rebuilt on next use.
    """
    from .images.builder import TOOLS_HASH_FILE, build_image
    from .tools import resolve_tools, tools_hash

    config = load_config()
    enabled = resolve_tools(config)
    current_hash = tools_hash(enabled)

    if TOOLS_HASH_FILE.exists() and TOOLS_HASH_FILE.read_text().strip() == current_hash:
        return

    click.echo("Tools configuration changed, rebuilding base image...")
    build_image(runtime, "base")


def maybe_rebuild_customize():
    """If the user customization script has changed, trigger a background rebuild of all images.

    Compares the current hash of ~/.bubble/customize.sh against the stored
    hash from the last build. If different (or script was added/removed),
    triggers a background base image rebuild. Derived images are purged
    during the rebuild so they pick up the changes on next use.
    """
    from .images.builder import CUSTOMIZE_HASH_FILE, customize_hash

    current = customize_hash()

    if CUSTOMIZE_HASH_FILE.exists():
        stored = CUSTOMIZE_HASH_FILE.read_text().strip()
    else:
        stored = None

    # No script and no previous hash — nothing to do
    if current is None and stored is None:
        return
    # Hash matches — nothing to do
    if current == stored:
        return

    if is_build_locked("base"):
        return

    if current is None:
        click.echo("Customization script removed, rebuilding base image in background...")
    elif stored is None:
        click.echo("Customization script detected, rebuilding base image in background...")
    else:
        click.echo("Customization script changed, rebuilding base image in background...")

    _spawn_background_bubble(
        ["images", "build", "base"],
        "/tmp/bubble-customize-rebuild.log",
    )


def detect_and_build_image(runtime, ref_path, t):
    """Detect language hook and ensure image exists. Returns (hook, image_name)."""
    if t.kind == "pr":
        hook_ref = f"refs/pull/{t.ref}/head"
    elif t.kind in ("branch", "commit"):
        hook_ref = t.ref
    else:
        # "repo" and "issue" use the default branch
        hook_ref = "HEAD"

    hook = select_hook(ref_path, hook_ref)
    if hook:
        click.echo(f"  Detected: {hook.name()}")
        image_name = hook.image_name()
    else:
        image_name = "base"

    pending_toolchain_build = None
    is_toolchain_image = image_name.startswith("lean-v")
    if not runtime.image_exists(image_name):
        if is_toolchain_image:
            # Toolchain-specific image doesn't exist yet — fall back to base lean
            # and build the toolchain image in the background for next time.
            version = image_name[len("lean-") :]
            click.echo(
                f"  Toolchain {version} image not cached, using lean image"
                f" (building {image_name} in background for next time)"
            )
            pending_toolchain_build = version
            image_name = "lean"
        if not runtime.image_exists(image_name):
            click.echo(f"Building {image_name} image (one-time setup, may take a few minutes)...")
            from .images.builder import build_image

            build_image(runtime, image_name)
            click.echo(f"  {image_name} image ready.")
    elif is_toolchain_image:
        version = image_name[len("lean-") :]
        click.echo(f"  Using cached toolchain image ({version})")

    if pending_toolchain_build:
        _background_build_lean_toolchain(pending_toolchain_build)

    return hook, image_name


def _background_build_lean_toolchain(version: str):
    """Fire off a background build of a toolchain-specific Lean image."""
    image_alias = f"lean-{version}"
    # Incus container names only allow alphanumeric + hyphens
    safe_alias = image_alias.replace(".", "-")
    # Skip if a build is already in progress (avoid spawning redundant processes)
    if is_build_locked(safe_alias):
        return
    click.echo(f"  Building {image_alias} image in background for next time...")
    _spawn_background_bubble(
        ["images", "build", image_alias],
        f"/tmp/bubble-{image_alias}-build.log",
    )
