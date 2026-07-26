"""Image detection, building, and background rebuild management."""

import shutil
import subprocess
import sys

import click

from .hooks import select_hook
from .images.builder import desired_image_alias, is_build_locked
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
    """Compatibility no-op; build-key selection happens after target detection."""


def maybe_rebuild_tools(runtime: ContainerRuntime, notices=None):
    """Compatibility no-op; the selected image is ensured by detection."""


def maybe_rebuild_customize(notices=None):
    """Compatibility no-op; customization contents are part of the build key."""


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
    logical_image_name = image_name
    is_toolchain_image = logical_image_name.startswith("lean-v")
    physical_image_name = desired_image_alias(runtime, logical_image_name)
    if not runtime.image_exists(physical_image_name):
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
                    built_alias = build_lean_toolchain_image(runtime, version)
                    if isinstance(built_alias, str):
                        physical_image_name = built_alias
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
                logical_image_name = "lean"
                physical_image_name = desired_image_alias(runtime, logical_image_name)
        if not is_toolchain_image and not runtime.image_exists(physical_image_name):
            step(f"Building {logical_image_name} image (one-time setup, may take a few minutes)...")
            from .images.builder import build_image

            physical_image_name = build_image(runtime, logical_image_name, quiet=True)
            detail(f"{logical_image_name} image ready.")
    elif is_toolchain_image:
        version = image_name[len("lean-") :]
        detail(f"Using cached toolchain image ({version})")

    if pending_toolchain_build:
        _background_build_lean_toolchain(runtime, pending_toolchain_build)

    return hook, physical_image_name


def _background_build_lean_toolchain(runtime: ContainerRuntime, version: str):
    """Fire off a background build of a toolchain-specific Lean image."""
    image_alias = f"lean-{version}"
    physical_alias = desired_image_alias(runtime, image_alias)
    # Skip if a build is already in progress (avoid spawning redundant processes)
    if is_build_locked(runtime.qualify(physical_alias)):
        return
    detail(f"Building {image_alias} image in background for next time...")
    _spawn_background_bubble(
        ["images", "build", image_alias],
        f"/tmp/bubble-{image_alias}-build.log",
    )
