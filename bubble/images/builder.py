"""Base image construction."""

import time
from pathlib import Path

from ..config import GIT_DIR
from ..runtime.base import ContainerRuntime

SCRIPTS_DIR = Path(__file__).parent / "scripts"

# Images that build on top of lean-base
DERIVED_IMAGES = {
    "lean-mathlib": "lean-mathlib.sh",
    "lean-batteries": "lean-batteries.sh",
    "lean-lean4": "lean-lean4.sh",
}


def _wait_for_container(runtime: ContainerRuntime, name: str, timeout: int = 60):
    """Wait for a container to be ready, including DNS."""
    for _ in range(timeout):
        try:
            runtime.exec(name, ["true"])
            try:
                runtime.exec(name, ["getent", "hosts", "github.com"])
                return
            except Exception:
                time.sleep(1)
        except Exception:
            time.sleep(1)
    raise RuntimeError(f"Container '{name}' not ready after {timeout}s")


def build_lean_base(runtime: ContainerRuntime):
    """Build the lean-base image: Ubuntu 24.04 + elan + openssh-server."""
    build_name = "lean-base-builder"

    print("Building lean-base image...")

    # Launch from stock Ubuntu
    runtime.launch(build_name, "images:ubuntu/24.04")
    _wait_for_container(runtime, build_name)

    # Run setup script
    script = (SCRIPTS_DIR / "lean-base.sh").read_text()
    runtime.exec(build_name, ["bash", "-c", script])

    # Publish as image
    runtime.stop(build_name)
    runtime.publish(build_name, "lean-base")
    runtime.delete(build_name)

    print("lean-base image built successfully.")


def build_derived_image(runtime: ContainerRuntime, image_name: str):
    """Build a derived image on top of lean-base."""
    if image_name not in DERIVED_IMAGES:
        raise ValueError(f"Unknown image: {image_name}. Available: {', '.join(DERIVED_IMAGES)}")

    # Ensure lean-base exists
    if not runtime.image_exists("lean-base"):
        build_lean_base(runtime)

    build_name = f"{image_name}-builder"
    script_file = DERIVED_IMAGES[image_name]

    print(f"Building {image_name} image...")

    # Launch from lean-base
    runtime.launch(build_name, "lean-base")
    _wait_for_container(runtime, build_name)

    # Mount shared git store if available
    if GIT_DIR.exists():
        runtime.add_disk(build_name, "shared-git", str(GIT_DIR), "/shared/git", readonly=True)

    # Run setup script
    script = (SCRIPTS_DIR / script_file).read_text()
    runtime.exec(build_name, ["bash", "-c", script])

    # Publish as image
    runtime.stop(build_name)
    runtime.publish(build_name, image_name)
    runtime.delete(build_name)

    print(f"{image_name} image built successfully.")


def build_image(runtime: ContainerRuntime, image_name: str):
    """Build any known image by name."""
    if image_name == "lean-base":
        build_lean_base(runtime)
    elif image_name in DERIVED_IMAGES:
        build_derived_image(runtime, image_name)
    else:
        all_images = ["lean-base"] + list(DERIVED_IMAGES.keys())
        raise ValueError(f"Unknown image: {image_name}. Available: {', '.join(all_images)}")
