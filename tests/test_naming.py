"""Tests for container name generation."""

from datetime import date

from bubble.naming import deduplicate_name, generate_name


def test_generate_name_pr():
    assert generate_name("mathlib4", "pr", "12345") == "mathlib4-pr-12345"


def test_generate_name_branch():
    assert generate_name("batteries", "branch", "fix-grind") == "batteries-branch-fix-grind"


def test_generate_name_main_uses_date(monkeypatch):
    monkeypatch.setattr("bubble.naming.date", type("D", (), {
        "today": staticmethod(lambda: date(2026, 3, 14)),
    })())
    assert generate_name("lean4", "main", "") == "lean4-main-20260314"


def test_generate_name_sanitizes_uppercase():
    name = generate_name("MyRepo", "branch", "FeatureBranch")
    assert name == name.lower()
    assert "myrepo" in name


def test_generate_name_sanitizes_special_chars():
    name = generate_name("repo", "branch", "feat/add_thing")
    assert "/" not in name
    assert "_" not in name
    assert "feat-add-thing" in name


def test_generate_name_digit_prefix():
    name = generate_name("123repo", "pr", "1")
    assert name.startswith("b-")


def test_deduplicate_no_collision():
    assert deduplicate_name("test", set()) == "test"


def test_deduplicate_single_collision():
    assert deduplicate_name("test", {"test"}) == "test-2"


def test_deduplicate_multiple_collisions():
    existing = {"test", "test-2", "test-3"}
    assert deduplicate_name("test", existing) == "test-4"
