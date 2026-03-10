"""Tests for skill install/uninstall/status."""

from pathlib import Path
from unittest import mock

import pytest

from bubble import skill


@pytest.fixture
def tmp_claude_dir(tmp_path):
    """Set up a temporary ~/.claude directory."""
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    with mock.patch.object(skill, "CLAUDE_DIR", claude_dir), \
         mock.patch.object(skill, "SKILLS_DIR", claude_dir / "skills" / skill.SKILL_NAME), \
         mock.patch.object(skill, "INSTALLED_SKILL",
                           claude_dir / "skills" / skill.SKILL_NAME / skill.SKILL_FILENAME):
        yield claude_dir


@pytest.fixture
def tmp_no_claude(tmp_path):
    """Set up a temporary dir without ~/.claude."""
    fake_dir = tmp_path / ".claude"
    with mock.patch.object(skill, "CLAUDE_DIR", fake_dir), \
         mock.patch.object(skill, "SKILLS_DIR", fake_dir / "skills" / skill.SKILL_NAME), \
         mock.patch.object(skill, "INSTALLED_SKILL",
                           fake_dir / "skills" / skill.SKILL_NAME / skill.SKILL_FILENAME):
        yield fake_dir


def test_bundled_skill_content():
    """The bundled skill file should be readable."""
    content = skill._bundled_skill_content()
    assert "lean-bubbles" in content
    assert "bubble" in content


def test_claude_code_detected(tmp_claude_dir):
    assert skill.claude_code_detected() is True


def test_claude_code_not_detected(tmp_no_claude):
    assert skill.claude_code_detected() is False


def test_install_fresh(tmp_claude_dir):
    assert not skill.is_installed()
    msg = skill.install_skill()
    assert "Installed" in msg
    assert skill.is_installed()
    assert skill.is_up_to_date()


def test_install_no_claude(tmp_no_claude):
    msg = skill.install_skill()
    assert "not found" in msg or "not detected" in msg
    assert not skill.is_installed()


def test_uninstall(tmp_claude_dir):
    skill.install_skill()
    assert skill.is_installed()
    msg = skill.uninstall_skill()
    assert "Removed" in msg
    assert not skill.is_installed()


def test_uninstall_not_installed(tmp_claude_dir):
    msg = skill.uninstall_skill()
    assert "not installed" in msg


def test_is_up_to_date_after_modification(tmp_claude_dir):
    skill.install_skill()
    assert skill.is_up_to_date()
    # Modify the installed file
    skill.INSTALLED_SKILL.write_text("modified content")
    assert not skill.is_up_to_date()


def test_diff_skill(tmp_claude_dir):
    skill.install_skill()
    assert skill.diff_skill() == ""
    # Modify
    skill.INSTALLED_SKILL.write_text("modified content")
    d = skill.diff_skill()
    assert "---" in d
    assert "+++" in d


def test_diff_skill_not_installed(tmp_claude_dir):
    assert skill.diff_skill() == ""


def test_reinstall_updates(tmp_claude_dir):
    skill.install_skill()
    skill.INSTALLED_SKILL.write_text("old content")
    assert not skill.is_up_to_date()
    skill.install_skill()
    assert skill.is_up_to_date()
