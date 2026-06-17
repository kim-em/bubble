"""Image detection, building, and background rebuild management."""

import shutil
import subprocess
import sys

import click

from .config import load_config
from .hooks import select_hook
from .images.builder import VSCODE_COMMIT_FILE, get_vscode_commit, is_build_locked
from .output import detail, step
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
    if is_build_locked("base"):
        return
    _spawn_background_bubble(
        ["images", "build", "base", "--force"],
        "/tmp/bubble-vscode-rebuild.log",
    )


def maybe_rebuild_tools(runtime: ContainerRuntime, notices=None):
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

    def _tools_still_stale():
        return not (
            TOOLS_HASH_FILE.exists() and TOOLS_HASH_FILE.read_text().strip() == current_hash
        )

    if notices:
        notices.begin()
    step("Base image configuration changed, rebuilding base image...")
    build_image(runtime, "base", force=True, still_needed=_tools_still_stale, quiet=True)


def maybe_rebuild_customize(notices=None):
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

    if notices:
        notices.begin()
    if current is None:
        step("Customization script removed, rebuilding base image in background...")
    elif stored is None:
        step("Customization script detected, rebuilding base image in background...")
    else:
        step("Customization script changed, rebuilding base image in background...")

    _spawn_background_bubble(
        ["images", "build", "base", "--force"],
        "/tmp/bubble-customize-rebuild.log",
    )


def detect_and_build_image(runtime, ref_path, t, restricted_network: bool = True):
    """Detect language hook and ensure image exists. Returns (hook, image_name).

    ``restricted_network`` is True when the container will run under the
    network allowlist (the default). It governs the missing-toolchain-image
    fallback: under the allowlist, elan cannot download a toolchain inside the
    container, so a missing ``lean-vX.Y.Z`` image is built synchronously rather
    than falling back to the plain ``lean`` image.
    """
    if t.kind == "pr":
        hook_ref = f"refs/pull/{t.ref}/head"
    elif t.kind in ("branch", "commit"):
        hook_ref = t.ref
    else:
        # "repo" and "issue" use the default branch
        hook_ref = "HEAD"

    hook = select_hook(ref_path, hook_ref)
    if hook:
        detail(f"Detected: {hook.name()}")
        image_name = hook.image_name()
    else:
        image_name = "base"

    pending_toolchain_build = None
    is_toolchain_image = image_name.startswith("lean-v")
    if not runtime.image_exists(image_name):
        if is_toolchain_image:
            version = image_name[len("lean-") :]
            if restricted_network:
                # Under the network allowlist, falling back to the plain `lean`
                # image would force elan to download this toolchain inside the
                # container, where the repo-scoped auth proxy blocks GitHub
                # release assets and every lake/elan call hangs ~300s. Build the
                # toolchain image now instead — image builds run with open
                # network, so the toolchain is fetched and baked in here.
                step(f"Building {image_name} image (one-time setup, may take a few minutes)...")
                from .images.builder import build_lean_toolchain_image

                try:
                    build_lean_toolchain_image(runtime, version)
                    detail(f"{image_name} image ready.")
                except Exception as e:
                    # Fail fast: falling back to the plain `lean` image here
                    # would put us right back in the blocked-download hang this
                    # build was meant to avoid. Surface an actionable error
                    # instead.
                    raise click.ClickException(
                        f"Could not build the {image_name} toolchain image ({e}).\n"
                        f"Without it, elan would try to download {version} inside"
                        " the container, which the network allowlist blocks.\n"
                        "Retry once the network recovers, or rerun with"
                        " --no-network to allow the in-container download."
                    ) from e
            else:
                # No network restriction — elan can download the toolchain in
                # the container, so use the plain lean image immediately and
                # build the toolchain image in the background for next time.
                detail(
                    f"Toolchain {version} image not cached, using lean image"
                    f" (building {image_name} in background for next time)"
                )
                pending_toolchain_build = version
                image_name = "lean"
        if not runtime.image_exists(image_name):
            step(f"Building {image_name} image (one-time setup, may take a few minutes)...")
            from .images.builder import build_image

            build_image(runtime, image_name, quiet=True)
            detail(f"{image_name} image ready.")
    elif is_toolchain_image:
        version = image_name[len("lean-") :]
        detail(f"Using cached toolchain image ({version})")

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
    detail(f"Building {image_alias} image in background for next time...")
    _spawn_background_bubble(
        ["images", "build", image_alias],
        f"/tmp/bubble-{image_alias}-build.log",
    )
