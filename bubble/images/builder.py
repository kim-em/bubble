"""Container image construction."""

import time
from pathlib import Path

from ..runtime.base import ContainerRuntime

SCRIPTS_DIR = Path(__file__).parent / "scripts"

# Image hierarchy: name -> {"script": "...", "parent": "..."}
# Parent can be another image name (built recursively) or an Incus remote image.
IMAGES = {
    "base": {"script": "base.sh", "parent": "images:ubuntu/24.04"},
    "lean": {"script": "lean.sh", "parent": "base"},
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


def build_image(runtime: ContainerRuntime, image_name: str):
    """Build any known image by name. Builds parent images recursively if needed."""
    if image_name not in IMAGES:
        available = ", ".join(IMAGES.keys())
        raise ValueError(f"Unknown image: {image_name}. Available: {available}")

    spec = IMAGES[image_name]
    parent = spec["parent"]

    # Ensure parent image exists (recursive for our own images)
    if parent in IMAGES and not runtime.image_exists(parent):
        build_image(runtime, parent)

    build_name = f"{image_name}-builder"
    print(f"Building {image_name} image...")

    # Launch from parent
    runtime.launch(build_name, parent)
    _wait_for_container(runtime, build_name)

    # Run setup script
    script = (SCRIPTS_DIR / spec["script"]).read_text()
    runtime.exec(build_name, ["bash", "-c", script])

    # Publish as image
    runtime.stop(build_name)
    runtime.publish(build_name, image_name)
    runtime.delete(build_name)

    print(f"{image_name} image built successfully.")
