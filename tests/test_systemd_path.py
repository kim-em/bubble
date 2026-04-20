"""Tests for systemd PATH environment injection."""

from unittest.mock import patch

from bubble.automation import _systemd_path_env


def test_systemd_path_env_uses_current_path():
    """PATH from the current environment is captured."""
    with patch.dict("os.environ", {"PATH": "/usr/local/bin:/usr/bin:/bin"}):
        result = _systemd_path_env()
        assert result == "Environment=PATH=/usr/local/bin:/usr/bin:/bin"


def test_systemd_path_env_escapes_percent():
    """Percent signs are escaped for systemd specifier syntax."""
    with patch.dict("os.environ", {"PATH": "/home/user/%n/bin:/usr/bin"}):
        result = _systemd_path_env()
        assert result == "Environment=PATH=/home/user/%%n/bin:/usr/bin"


def test_systemd_path_env_fallback():
    """Falls back to sensible default when PATH is unset."""
    with patch.dict("os.environ", {}, clear=True):
        result = _systemd_path_env()
        assert result == "Environment=PATH=/usr/local/bin:/usr/bin:/bin"
