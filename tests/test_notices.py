"""Tests for the startup notice separator system."""

import click

from bubble.notices import SEPARATOR, Notices, maybe_print_welcome


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


class TestWelcomeBanner:
    """Tests for the first-run welcome banner."""

    def test_welcome_banner_on_first_run(self, tmp_data_dir, monkeypatch, capsys):
        """Banner prints when first_run is True and stderr is a TTY."""
        monkeypatch.setattr("sys.stderr.isatty", lambda: True)

        ctx = click.Context(click.Command("test"), obj={"first_run": True})
        with ctx:
            maybe_print_welcome()

        captured = capsys.readouterr()
        assert "bubble v" in captured.err
        assert "Config created at" in captured.err
        assert "bubble -h" in captured.err
        assert "bubble doctor" in captured.err

    def test_welcome_banner_not_on_subsequent_run(self, tmp_data_dir, monkeypatch, capsys):
        """Banner does not print when first_run is False."""
        monkeypatch.setattr("sys.stderr.isatty", lambda: True)

        ctx = click.Context(click.Command("test"), obj={"first_run": False})
        with ctx:
            maybe_print_welcome()

        captured = capsys.readouterr()
        assert captured.err == ""

    def test_welcome_banner_suppressed_non_tty(self, tmp_data_dir, monkeypatch, capsys):
        """Banner does not print when stderr is not a TTY."""
        monkeypatch.setattr("sys.stderr.isatty", lambda: False)

        ctx = click.Context(click.Command("test"), obj={"first_run": True})
        with ctx:
            maybe_print_welcome()

        captured = capsys.readouterr()
        assert captured.err == ""

    def test_welcome_banner_only_prints_once(self, tmp_data_dir, monkeypatch, capsys):
        """Banner clears the first_run flag so it only prints once per invocation."""
        monkeypatch.setattr("sys.stderr.isatty", lambda: True)

        ctx = click.Context(click.Command("test"), obj={"first_run": True})
        with ctx:
            maybe_print_welcome()
            capsys.readouterr()  # discard first output
            maybe_print_welcome()

        captured = capsys.readouterr()
        assert captured.err == ""

    def test_welcome_banner_uses_notices(self, tmp_data_dir, monkeypatch, capsys):
        """Banner integrates with the Notices separator system."""
        monkeypatch.setattr("sys.stderr.isatty", lambda: True)

        ctx = click.Context(click.Command("test"), obj={"first_run": True})
        notices = Notices()
        with ctx:
            # Simulate a prior notice group
            notices.begin()
            capsys.readouterr()
            maybe_print_welcome(notices=notices)

        captured = capsys.readouterr()
        # Should have a separator before the banner (from notices.begin() inside)
        assert SEPARATOR in captured.err
        assert "bubble v" in captured.err
