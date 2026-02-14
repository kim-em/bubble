"""Tests for lake cache — safe tar extraction is security-critical."""

import io
import tarfile

import pytest

from lean_bubbles.lake_cache import _safe_extract_tar, cache_key, cache_path


def _make_tar(tmp_path, members):
    """Create a tar file with specified members.

    members: list of dicts with keys: name, content (bytes), type ('file', 'symlink', 'hardlink'),
             linkname (for sym/hardlinks)
    """
    tar_path = tmp_path / "test.tar"
    with tarfile.open(tar_path, "w") as tf:
        for m in members:
            info = tarfile.TarInfo(name=m["name"])
            if m.get("type") == "symlink":
                info.type = tarfile.SYMTYPE
                info.linkname = m.get("linkname", "/etc/passwd")
                tf.addfile(info)
            elif m.get("type") == "hardlink":
                info.type = tarfile.LNKTYPE
                info.linkname = m.get("linkname", "/etc/passwd")
                tf.addfile(info)
            elif m.get("type") == "chrdev":
                info.type = tarfile.CHRTYPE
                tf.addfile(info)
            elif m.get("type") == "blkdev":
                info.type = tarfile.BLKTYPE
                tf.addfile(info)
            elif m.get("type") == "fifo":
                info.type = tarfile.FIFOTYPE
                tf.addfile(info)
            else:
                content = m.get("content", b"test content")
                info.size = len(content)
                tf.addfile(info, io.BytesIO(content))
    return tar_path


class TestSafeExtractTar:
    """Verify tar extraction rejects dangerous archives."""

    def test_valid_tar_extracts(self, tmp_path):
        tar_path = _make_tar(
            tmp_path,
            [
                {"name": "file.txt", "content": b"hello"},
                {"name": "subdir/nested.txt", "content": b"world"},
            ],
        )
        dest = tmp_path / "output"
        dest.mkdir()
        _safe_extract_tar(tar_path, dest)
        assert (dest / "file.txt").read_text() == "hello"
        assert (dest / "subdir" / "nested.txt").read_text() == "world"

    def test_absolute_path_rejected(self, tmp_path):
        tar_path = _make_tar(
            tmp_path,
            [
                {"name": "/etc/passwd", "content": b"pwned"},
            ],
        )
        dest = tmp_path / "output"
        dest.mkdir()
        with pytest.raises(ValueError, match="absolute path"):
            _safe_extract_tar(tar_path, dest)

    def test_path_traversal_rejected(self, tmp_path):
        tar_path = _make_tar(
            tmp_path,
            [
                {"name": "../../../etc/passwd", "content": b"pwned"},
            ],
        )
        dest = tmp_path / "output"
        dest.mkdir()
        with pytest.raises(ValueError, match="path traversal"):
            _safe_extract_tar(tar_path, dest)

    def test_dotdot_in_middle_rejected(self, tmp_path):
        tar_path = _make_tar(
            tmp_path,
            [
                {"name": "subdir/../../escape.txt", "content": b"pwned"},
            ],
        )
        dest = tmp_path / "output"
        dest.mkdir()
        with pytest.raises(ValueError, match="path traversal|escapes dest"):
            _safe_extract_tar(tar_path, dest)

    def test_symlink_rejected(self, tmp_path):
        tar_path = _make_tar(
            tmp_path,
            [
                {"name": "evil-link", "type": "symlink", "linkname": "/etc/passwd"},
            ],
        )
        dest = tmp_path / "output"
        dest.mkdir()
        with pytest.raises(ValueError, match="symlink|hardlink"):
            _safe_extract_tar(tar_path, dest)

    def test_hardlink_rejected(self, tmp_path):
        tar_path = _make_tar(
            tmp_path,
            [
                {"name": "evil-link", "type": "hardlink", "linkname": "/etc/passwd"},
            ],
        )
        dest = tmp_path / "output"
        dest.mkdir()
        with pytest.raises(ValueError, match="symlink|hardlink"):
            _safe_extract_tar(tar_path, dest)

    def test_device_node_rejected(self, tmp_path):
        tar_path = _make_tar(
            tmp_path,
            [{"name": "evil-dev", "type": "chrdev"}],
        )
        dest = tmp_path / "output"
        dest.mkdir()
        with pytest.raises(ValueError, match="device/fifo"):
            _safe_extract_tar(tar_path, dest)

    def test_block_device_rejected(self, tmp_path):
        tar_path = _make_tar(
            tmp_path,
            [{"name": "evil-blk", "type": "blkdev"}],
        )
        dest = tmp_path / "output"
        dest.mkdir()
        with pytest.raises(ValueError, match="device/fifo"):
            _safe_extract_tar(tar_path, dest)

    def test_fifo_rejected(self, tmp_path):
        tar_path = _make_tar(
            tmp_path,
            [{"name": "evil-fifo", "type": "fifo"}],
        )
        dest = tmp_path / "output"
        dest.mkdir()
        with pytest.raises(ValueError, match="device/fifo"):
            _safe_extract_tar(tar_path, dest)


class TestCacheKey:
    def test_basic(self):
        key = cache_key("mathlib4", "leanprover/lean4:v4.5.0")
        assert "/" not in key
        assert ":" not in key
        assert "mathlib4" in key

    def test_sanitizes_slashes(self):
        key = cache_key("repo", "some/path/toolchain")
        assert "/" not in key

    def test_sanitizes_colons(self):
        key = cache_key("repo", "leanprover:lean4")
        assert ":" not in key


class TestCachePath:
    def test_returns_path(self, tmp_data_dir):
        p = cache_path("mathlib4", "v4.5.0")
        assert "mathlib4" in str(p)
        assert "v4.5.0" in str(p)
