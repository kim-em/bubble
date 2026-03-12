"""Tests for image build locking to prevent race conditions (issue #67, #80)."""

import threading
import time

import pytest

from bubble.images.builder import (
    _ancestor_chain,
    _build_lock,
    build_image,
    build_lean_toolchain_image,
    is_build_locked,
)


def test_build_lock_prevents_concurrent_builds(mock_runtime, monkeypatch, tmp_data_dir):
    """A concurrent build_image call for the same image should skip if the first completes."""
    monkeypatch.setattr("bubble.tools._host_has_command", lambda cmd: False)
    monkeypatch.setattr("bubble.images.builder.get_vscode_commit", lambda: None)
    monkeypatch.setattr("bubble.images.builder.wait_for_container", lambda *a, **kw: None)

    from bubble.config import load_config, save_config

    config = load_config()
    config["tools"] = {"claude": "no", "codex": "no"}
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


def test_build_lean_toolchain_rejects_invalid_version(mock_runtime):
    """build_lean_toolchain_image rejects versions that don't match the expected pattern."""
    for bad in [
        "nightly-2024-01-01",
        "v4.16.0; rm -rf /",
        "../../../etc/passwd",
        "leanprover/lean4:v4.16.0",
        "v4",
        "v4.16",
        "",
        "hello",
    ]:
        with pytest.raises(ValueError, match="Invalid Lean toolchain version"):
            build_lean_toolchain_image(mock_runtime, bad)


def test_build_lean_toolchain_accepts_valid_version(mock_runtime, monkeypatch, tmp_data_dir):
    """build_lean_toolchain_image accepts stable and RC versions."""
    monkeypatch.setattr("bubble.images.builder.wait_for_container", lambda *a, **kw: None)
    mock_runtime._images.add("lean")

    build_lean_toolchain_image(mock_runtime, "v4.16.0")
    assert mock_runtime.image_exists("lean-v4.16.0")

    build_lean_toolchain_image(mock_runtime, "v4.16.0-rc2")
    assert mock_runtime.image_exists("lean-v4.16.0-rc2")


def test_build_lean_toolchain_lock(mock_runtime, monkeypatch, tmp_data_dir):
    """Lean toolchain builds also use build locks."""
    monkeypatch.setattr("bubble.images.builder.wait_for_container", lambda *a, **kw: None)

    mock_runtime._images.add("lean")

    # First build
    build_lean_toolchain_image(mock_runtime, "v4.16.0")
    assert mock_runtime.image_exists("lean-v4.16.0")

    # Second build should skip (image already exists)
    mock_runtime.calls.clear()
    build_lean_toolchain_image(mock_runtime, "v4.16.0")
    launch_calls = [c for c in mock_runtime.calls if c[0] == "launch"]
    assert len(launch_calls) == 0


def test_shared_lock_blocks_exclusive():
    """A shared lock on an image should block an exclusive lock on that image."""
    order = []

    def shared_holder(ready, done):
        with _build_lock("parent-img", shared=True):
            order.append("shared-acquired")
            ready.set()
            done.wait(timeout=5)
            order.append("shared-released")

    def exclusive_acquirer(ready):
        ready.wait(timeout=5)
        time.sleep(0.05)  # Ensure we try after the shared lock is held
        order.append("exclusive-waiting")
        with _build_lock("parent-img"):
            order.append("exclusive-acquired")

    ready = threading.Event()
    done = threading.Event()
    t1 = threading.Thread(target=shared_holder, args=(ready, done))
    t2 = threading.Thread(target=exclusive_acquirer, args=(ready,))

    t1.start()
    t2.start()
    # Give t2 time to start waiting for the exclusive lock
    time.sleep(0.15)
    # Release the shared lock
    done.set()
    t1.join(timeout=5)
    t2.join(timeout=5)

    # The exclusive lock must not be acquired until the shared lock is released
    assert order.index("shared-released") <= order.index("exclusive-acquired")


def test_exclusive_lock_blocks_shared():
    """An exclusive lock on an image should block a shared lock on that image."""
    order = []

    def exclusive_holder(ready, done):
        with _build_lock("parent-img"):
            order.append("exclusive-acquired")
            ready.set()
            done.wait(timeout=5)
            order.append("exclusive-released")

    def shared_acquirer(ready):
        ready.wait(timeout=5)
        time.sleep(0.05)
        order.append("shared-waiting")
        with _build_lock("parent-img", shared=True):
            order.append("shared-acquired")

    ready = threading.Event()
    done = threading.Event()
    t1 = threading.Thread(target=exclusive_holder, args=(ready, done))
    t2 = threading.Thread(target=shared_acquirer, args=(ready,))

    t1.start()
    t2.start()
    time.sleep(0.15)
    done.set()
    t1.join(timeout=5)
    t2.join(timeout=5)

    assert order.index("exclusive-released") <= order.index("shared-acquired")


def test_multiple_shared_locks_coexist():
    """Multiple shared locks on the same image should not block each other."""
    results = {}
    barrier = threading.Barrier(2, timeout=5)

    def shared_worker(name):
        with _build_lock("parent-img", shared=True):
            barrier.wait()  # Both must reach here concurrently
            results[name] = True

    t1 = threading.Thread(target=shared_worker, args=("a",))
    t2 = threading.Thread(target=shared_worker, args=("b",))
    t1.start()
    t2.start()
    t1.join(timeout=5)
    t2.join(timeout=5)

    assert results == {"a": True, "b": True}


def test_derived_build_holds_parent_lock(mock_runtime, monkeypatch, tmp_data_dir):
    """Building a derived image should hold a shared lock on the parent,
    blocking a concurrent parent rebuild until the derived build finishes."""
    monkeypatch.setattr("bubble.tools._host_has_command", lambda cmd: False)
    monkeypatch.setattr("bubble.images.builder.get_vscode_commit", lambda: None)

    from bubble.config import load_config, save_config

    config = load_config()
    config["tools"] = {"claude": "no", "codex": "no"}
    save_config(config)

    order = []
    derived_building = threading.Event()
    parent_lock_requested = threading.Event()

    # Instrument fcntl.flock to signal when the exclusive lock on base is
    # actually requested, replacing the racy sleep(0.1) that assumed the
    # parent thread would reach the lock within a fixed time window.
    import fcntl

    original_flock = fcntl.flock

    def signaling_flock(fd, operation):
        if (
            not (operation & fcntl.LOCK_SH)
            and hasattr(fd, "name")
            and fd.name.endswith("base.lock")
        ):
            parent_lock_requested.set()
        return original_flock(fd, operation)

    def slow_derived_wait(*a, **kw):
        """Simulate a slow derived build that holds the parent lock."""
        order.append("derived-building")
        derived_building.set()
        # Wait until the parent is actually blocked on the exclusive lock
        parent_lock_requested.wait(timeout=5)
        order.append("derived-done")

    def slow_parent_wait(*a, **kw):
        order.append("parent-building")

    mock_runtime._images = {"base"}  # base exists, lean does not

    def build_lean():
        monkeypatch.setattr("bubble.images.builder.wait_for_container", slow_derived_wait)
        build_image(mock_runtime, "lean")
        order.append("lean-published")

    def rebuild_base():
        derived_building.wait(timeout=5)
        monkeypatch.setattr("bubble.images.builder.fcntl.flock", signaling_flock)
        monkeypatch.setattr("bubble.images.builder.wait_for_container", slow_parent_wait)
        # Delete old base to force rebuild
        mock_runtime._images.discard("base")
        build_image(mock_runtime, "base")
        order.append("base-published")

    t1 = threading.Thread(target=build_lean)
    t2 = threading.Thread(target=rebuild_base)
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)

    # The derived build (lean) must complete before the parent rebuild (base)
    # can proceed, because the derived build holds a shared lock on base.
    assert order.index("lean-published") < order.index("parent-building")


def test_ancestor_chain():
    """_ancestor_chain returns the full chain of ancestors, root first."""
    # base has no ancestors (its parent is images:ubuntu/24.04)
    assert _ancestor_chain("base") == []
    # lean's parent is base
    assert _ancestor_chain("lean") == ["base"]


def test_ancestor_chain_deep(monkeypatch):
    """_ancestor_chain works for deeper hierarchies (3+ levels)."""
    from bubble.images import builder

    # Temporarily add a grandchild image to test the full chain
    original_images = builder.IMAGES.copy()
    monkeypatch.setattr(
        builder,
        "IMAGES",
        {
            **original_images,
            "lean-extra": {"script": "lean.sh", "parent": "lean"},
        },
    )
    assert _ancestor_chain("lean-extra") == ["base", "lean"]


def test_grandchild_build_holds_ancestor_locks(mock_runtime, monkeypatch, tmp_data_dir):
    """Building a grandchild should block a grandparent (base) rebuild.

    This tests the full ancestor chain locking: a grandchild holds shared
    locks on both its parent and grandparent, so a concurrent grandparent
    rebuild must wait until the grandchild build completes.
    """
    from bubble.images import builder

    monkeypatch.setattr("bubble.tools._host_has_command", lambda cmd: False)
    monkeypatch.setattr("bubble.images.builder.get_vscode_commit", lambda: None)

    from bubble.config import load_config, save_config

    config = load_config()
    config["tools"] = {"claude": "no", "codex": "no"}
    save_config(config)

    # Temporarily add a grandchild image for this test
    monkeypatch.setattr(
        builder,
        "IMAGES",
        {
            **builder.IMAGES,
            "lean-extra": {"script": "lean.sh", "parent": "lean"},
        },
    )

    order = []
    derived_building = threading.Event()
    parent_started = threading.Event()

    def slow_derived_wait(*a, **kw):
        order.append("grandchild-building")
        derived_building.set()
        parent_started.wait(timeout=5)
        time.sleep(0.1)
        order.append("grandchild-done")

    def slow_parent_wait(*a, **kw):
        order.append("grandparent-building")

    # base and lean exist, lean-extra does not
    mock_runtime._images = {"base", "lean"}

    def build_grandchild():
        monkeypatch.setattr("bubble.images.builder.wait_for_container", slow_derived_wait)
        build_image(mock_runtime, "lean-extra")
        order.append("lean-extra-published")

    def rebuild_base():
        derived_building.wait(timeout=5)
        parent_started.set()
        monkeypatch.setattr("bubble.images.builder.wait_for_container", slow_parent_wait)
        mock_runtime._images.discard("base")
        build_image(mock_runtime, "base")
        order.append("base-published")

    t1 = threading.Thread(target=build_grandchild)
    t2 = threading.Thread(target=rebuild_base)
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)

    # The grandchild build must complete before the grandparent rebuild
    assert order.index("lean-extra-published") < order.index("grandparent-building")
