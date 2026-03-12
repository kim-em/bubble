"""Tests for the startup notice separator system."""

from bubble.notices import SEPARATOR, Notices


def test_notices_first_begin_no_separator(capsys):
    n = Notices()
    n.begin()
    captured = capsys.readouterr()
    assert captured.err == ""
    assert n.has_output is True


def test_notices_second_begin_prints_separator(capsys):
    n = Notices()
    n.begin()
    capsys.readouterr()  # discard first
    n.begin()
    captured = capsys.readouterr()
    assert SEPARATOR in captured.err


def test_notices_finish_after_output(capsys):
    n = Notices()
    n.begin()
    capsys.readouterr()
    n.finish()
    captured = capsys.readouterr()
    assert SEPARATOR in captured.err


def test_notices_finish_no_output(capsys):
    n = Notices()
    n.finish()
    captured = capsys.readouterr()
    assert captured.err == ""


def test_notices_has_output_initially_false():
    n = Notices()
    assert n.has_output is False


def test_notices_multiple_groups(capsys):
    n = Notices()
    n.begin()
    n.begin()
    n.begin()
    captured = capsys.readouterr()
    # Two separators (between groups 1-2 and 2-3)
    assert captured.err.count(SEPARATOR) == 2
