"""Tests for image build locking to prevent race conditions (issue #67)."""

import threading

from bubble.images.builder import _build_lock, build_image, is_build_locked


def test_build_lock_prevents_concurrent_builds(mock_runtime, monkeypatch, tmp_data_dir):
    """A concurrent build_image call for the same image should skip if the first completes."""
    monkeypatch.setattr("bubble.tools._host_has_command", lambda cmd: False)
    monkeypatch.setattr("bubble.images.builder.get_vscode_commit", lambda: None)
    monkeypatch.setattr("bubble.images.builder._wait_for_container", lambda *a, **kw: None)

    from bubble.config import load_config, save_config

    config = load_config()
    config["tools"] = {"claude": "no", "codex": "no", "gh": "no"}
    save_config(config)

    mock_runtime._images.discard("base")

    # First build should proceed normally
    build_image(mock_runtime, "base")
    assert "base" in mock_runtime._images

    # Second build should skip (image already exists after lock acquired)
    mock_runtime.calls.clear()
    build_image(mock_runtime, "base")
    # No launch call — the build was skipped
    launch_calls = [c for c in mock_runtime.calls if c[0] == "launch"]
    assert len(launch_calls) == 0


def test_build_lock_is_exclusive():
    """Two threads trying to acquire the same lock should serialize."""
    order = []

    def worker(name, delay_event):
        with _build_lock("test-exclusive"):
            order.append(f"{name}-start")
            delay_event.wait(timeout=2)
            order.append(f"{name}-end")

    event = threading.Event()
    t1 = threading.Thread(target=worker, args=("first", event))
    t2 = threading.Thread(target=worker, args=("second", threading.Event()))

    t1.start()
    # Give t1 time to acquire the lock
    import time

    time.sleep(0.05)
    t2.start()
    # Let t1 finish
    event.set()
    t1.join(timeout=5)
    t2.join(timeout=5)

    # first must complete before second starts
    assert order.index("first-end") < order.index("second-start")


def test_build_lock_different_images_dont_block():
    """Locks for different image names should not block each other."""
    results = {}

    def worker(image_name):
        with _build_lock(image_name):
            results[image_name] = True

    t1 = threading.Thread(target=worker, args=("image-a",))
    t2 = threading.Thread(target=worker, args=("image-b",))
    t1.start()
    t2.start()
    t1.join(timeout=5)
    t2.join(timeout=5)

    assert results == {"image-a": True, "image-b": True}


def test_is_build_locked_false_when_free():
    """is_build_locked returns False when no build holds the lock."""
    assert not is_build_locked("no-such-build")


def test_is_build_locked_true_when_held():
    """is_build_locked returns True when another thread holds the lock."""
    ready = threading.Event()
    done = threading.Event()

    def holder():
        with _build_lock("held-image"):
            ready.set()
            done.wait(timeout=5)

    t = threading.Thread(target=holder)
    t.start()
    ready.wait(timeout=5)

    assert is_build_locked("held-image")
    # Different image should not be locked
    assert not is_build_locked("other-image")

    done.set()
    t.join(timeout=5)


def test_build_lean_toolchain_lock(mock_runtime, monkeypatch, tmp_data_dir):
    """Lean toolchain builds also use build locks."""
    monkeypatch.setattr("bubble.images.builder._wait_for_container", lambda *a, **kw: None)

    from bubble.images.builder import build_lean_toolchain_image

    mock_runtime._images.add("lean")

    # First build
    build_lean_toolchain_image(mock_runtime, "v4.16.0")
    assert mock_runtime.image_exists("lean-v4.16.0")

    # Second build should skip (image already exists)
    mock_runtime.calls.clear()
    build_lean_toolchain_image(mock_runtime, "v4.16.0")
    launch_calls = [c for c in mock_runtime.calls if c[0] == "launch"]
    assert len(launch_calls) == 0
