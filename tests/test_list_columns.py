"""Tests for dynamic column width helpers in list_cmd."""

from bubble.commands.list_cmd import _display_width, _pad, _truncate


class TestDisplayWidth:
    def test_ascii(self):
        assert _display_width("hello") == 5

    def test_empty(self):
        assert _display_width("") == 0

    def test_wide_cjk(self):
        # CJK characters are fullwidth (2 columns each)
        assert _display_width("\u4e16\u754c") == 4  # 世界

    def test_mixed(self):
        assert _display_width("hi\u4e16") == 4  # 2 + 2

    def test_combining_marks(self):
        # e + combining acute accent renders as 1 cell
        assert _display_width("e\u0301") == 1

    def test_zwj_sequence(self):
        # ZWJ characters should be zero-width
        assert _display_width("\u200d") == 0

    def test_variation_selectors(self):
        # Variation selectors (U+FE0E, U+FE0F) are zero-width
        assert _display_width("\u2603\ufe0f") == 1  # snowman + VS16


class TestTruncate:
    def test_short_string_unchanged(self):
        assert _truncate("abc", 10) == "abc"

    def test_exact_fit(self):
        assert _truncate("abcde", 5) == "abcde"

    def test_truncated_with_ellipsis(self):
        result = _truncate("abcdefghij", 8)
        assert result == "abcde..."
        assert _display_width(result) == 8

    def test_wide_chars_truncated(self):
        # 3 wide chars = 6 columns; truncate to 5 should keep 1 wide + "..."
        result = _truncate("\u4e16\u754c\u4eba", 5)
        assert _display_width(result) <= 5
        assert result.endswith("...")

    def test_max_width_3(self):
        result = _truncate("abcdef", 3)
        assert result == "..."
        assert _display_width(result) == 3

    def test_max_width_2(self):
        result = _truncate("abcdef", 2)
        assert result == ".."
        assert _display_width(result) == 2

    def test_max_width_1(self):
        result = _truncate("abcdef", 1)
        assert result == "."

    def test_max_width_0(self):
        result = _truncate("abcdef", 0)
        assert result == ""


class TestPad:
    def test_pad_ascii(self):
        result = _pad("hi", 5)
        assert result == "hi   "
        assert len(result) == 5

    def test_pad_exact(self):
        assert _pad("hello", 5) == "hello"

    def test_pad_wide_chars(self):
        # "世" is 2 columns, so padding to 5 needs 3 spaces
        result = _pad("\u4e16", 5)
        assert result == "\u4e16   "
        assert _display_width(result) == 5
