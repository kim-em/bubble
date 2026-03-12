"""Tests for shell tab completion."""


from click.testing import CliRunner

from bubble.cli import main


def test_completion_zsh():
    runner = CliRunner()
    result = runner.invoke(main, ["completion", "zsh"])
    assert result.exit_code == 0
    assert "#compdef bubble" in result.output
    assert "_BUBBLE_COMPLETE" in result.output


def test_completion_bash():
    runner = CliRunner()
    result = runner.invoke(main, ["completion", "bash"])
    assert result.exit_code == 0
    assert "_bubble_completion" in result.output
    assert "_BUBBLE_COMPLETE" in result.output


def test_completion_fish():
    runner = CliRunner()
    result = runner.invoke(main, ["completion", "fish"])
    assert result.exit_code == 0
    assert "_bubble_completion" in result.output
    assert "complete" in result.output


def test_completion_invalid_shell():
    runner = CliRunner()
    result = runner.invoke(main, ["completion", "powershell"])
    assert result.exit_code != 0


def test_completion_install_zsh(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "bubble.commands.completion._INSTALL_PATHS",
        {
            "bash": {"linux": str(tmp_path / "bash" / "bubble")},
            "zsh": {"all": str(tmp_path / "zsh" / "_bubble")},
            "fish": {"all": str(tmp_path / "fish" / "bubble.fish")},
        },
    )
    runner = CliRunner()
    result = runner.invoke(main, ["completion", "zsh", "--install"])
    assert result.exit_code == 0
    assert "Completion script written to" in result.output
    assert "fpath" in result.output

    script_path = tmp_path / "zsh" / "_bubble"
    assert script_path.exists()
    assert "#compdef bubble" in script_path.read_text()


def test_completion_install_fish(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "bubble.commands.completion._INSTALL_PATHS",
        {
            "bash": {"linux": str(tmp_path / "bash" / "bubble")},
            "zsh": {"all": str(tmp_path / "zsh" / "_bubble")},
            "fish": {"all": str(tmp_path / "fish" / "bubble.fish")},
        },
    )
    runner = CliRunner()
    result = runner.invoke(main, ["completion", "fish", "--install"])
    assert result.exit_code == 0
    assert "Completion script written to" in result.output
    assert "Fish will pick this up automatically" in result.output

    script_path = tmp_path / "fish" / "bubble.fish"
    assert script_path.exists()


def test_completion_help():
    runner = CliRunner()
    result = runner.invoke(main, ["completion", "--help"])
    assert result.exit_code == 0
    assert "eval" in result.output
    assert "--install" in result.output
