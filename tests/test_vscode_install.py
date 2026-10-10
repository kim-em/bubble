"""The VS Code tool script installs its own dependencies, and fails fast when it cannot.

Regression for a fresh base-image build where the extension helper died with
``FileNotFoundError: 'unzip'``: ``vscode.sh`` ran the helper without ever ensuring ``unzip`` was
installed, relying on the optional ``elan`` tool (priority 10, before VS Code's 90) to have done it.
These tests run the script's real dependency preamble in a fresh bash with a fake ``apt-get`` on an
otherwise empty PATH, then drive the real extension helper (extracted from the script's heredoc)
against a canned marketplace answer and a local VSIX, with the only ``unzip`` being the one the fake
package manager "installed" because the preamble asked for it. Nothing here touches the network, the
real package manager, or ``/home/user``; the whole ``vscode.sh`` (which writes there as root) is
not executed on the host.
"""

import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from bubble.tools import resolve_tools

REPO = Path(__file__).resolve().parent.parent
VSCODE_SH = REPO / "bubble" / "images" / "scripts" / "tools" / "vscode.sh"
PREAMBLE_BOUNDARY = "# Pre-install VS Code Server"
HELPER_START, HELPER_END = "<< 'PYEOF'\n", "\nPYEOF\n"
SENTINEL = "PREAMBLE-CONTINUED"

FAKE_APT_GET = """#!/bin/bash
# A stand-in package manager: records its argv, exits with the status configured for its
# subcommand, and on a successful `install` that names unzip exposes the real unzip on PATH.
printf '%s\\n' "$*" >> "$FAKE_APT_LOG"
case "$1" in
  update)  exit "${FAKE_APT_UPDATE_RC:-0}" ;;
  install)
    rc="${FAKE_APT_INSTALL_RC:-0}"
    if [ "$rc" = 0 ]; then
      for arg in "$@"; do
        if [ "$arg" = unzip ] && [ -n "${FAKE_APT_REAL_UNZIP:-}" ]; then
          ln -sf "$FAKE_APT_REAL_UNZIP" "${0%/*}/unzip"
        fi
      done
    fi
    exit "$rc" ;;
  *) exit 99 ;;
esac
"""


def preamble(script_text: str) -> str:
    """The script's dependency setup: everything before the VS Code Server section."""
    idx = script_text.find(PREAMBLE_BOUNDARY)
    assert idx > 0, f"boundary {PREAMBLE_BOUNDARY!r} not found in vscode.sh"
    return script_text[:idx]


def helper_source(script_text: str) -> str:
    """The extension-installer Python program the script writes to a temp file."""
    start = script_text.index(HELPER_START) + len(HELPER_START)
    end = script_text.index(HELPER_END, start)
    return script_text[start:end] + "\n"


def make_bin(tmp_path: Path) -> Path:
    """A private executable directory holding only the fake apt-get (no unzip yet)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "apt-get"
    fake.write_text(FAKE_APT_GET)
    fake.chmod(0o755)
    # The only other executable the fake needs; bash itself is launched by absolute path.
    os.symlink(shutil.which("ln"), bin_dir / "ln")
    return bin_dir


def run_preamble(
    script_text: str, tmp_path: Path, *, update_rc: int = 0, install_rc: int = 0, expose_unzip=None
):
    """Run the checked-in preamble plus a continuation sentinel in a fresh bash on a PATH that
    holds only the fake apt-get. Returns (completed process, apt-get argv log lines)."""
    bin_dir = tmp_path / "bin"
    if not bin_dir.exists():
        make_bin(tmp_path)
    log = tmp_path / "apt.log"
    log.write_text("")
    harness = tmp_path / "preamble.sh"
    harness.write_text(preamble(script_text) + f"\necho {SENTINEL}\n")
    env = {
        "PATH": str(bin_dir),
        "FAKE_APT_LOG": str(log),
        "FAKE_APT_UPDATE_RC": str(update_rc),
        "FAKE_APT_INSTALL_RC": str(install_rc),
        "FAKE_APT_REAL_UNZIP": str(expose_unzip or ""),
        "HOME": str(tmp_path),
    }
    proc = subprocess.run(
        [shutil.which("bash"), str(harness)],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        stdin=subprocess.DEVNULL,
    )
    return proc, [line for line in log.read_text().splitlines() if line]


SCRIPT = VSCODE_SH.read_text()


class TestDependencyPreambleFailsFast:
    def test_update_failure_stops_setup(self, tmp_path):
        proc, calls = run_preamble(SCRIPT, tmp_path, update_rc=100)
        assert proc.returncode == 100, proc.stderr
        assert calls == ["update -qq"], calls  # install never attempted
        assert SENTINEL not in proc.stdout

    def test_install_failure_stops_setup(self, tmp_path):
        proc, calls = run_preamble(SCRIPT, tmp_path, install_rc=100)
        assert proc.returncode == 100, proc.stderr
        assert [c.split()[0] for c in calls] == ["update", "install"]
        assert SENTINEL not in proc.stdout

    def test_success_installs_both_dependencies_then_continues(self, tmp_path):
        proc, calls = run_preamble(SCRIPT, tmp_path)
        assert proc.returncode == 0, proc.stderr
        assert [c.split()[0] for c in calls] == ["update", "install"]
        install_args = calls[1].split()
        assert "python3" in install_args and "unzip" in install_args, install_args
        assert SENTINEL in proc.stdout


def _canned_marketplace(ext_id: str, version: str, vsix_url: str) -> dict:
    return {
        "results": [
            {
                "extensions": [
                    {
                        "versions": [
                            {
                                "version": version,
                                "targetPlatform": "",
                                "files": [
                                    {
                                        "assetType": "Microsoft.VisualStudio.Services.VSIXPackage",
                                        "source": vsix_url,
                                    }
                                ],
                            }
                        ]
                    }
                ]
            }
        ]
    }


DRIVER = '''
"""Drive the real extension helper offline: canned marketplace answer, local VSIX, fixed arch."""
import json, runpy, shutil, subprocess, sys, urllib.request
from pathlib import Path

helper, ext_id, vsix, marketplace, calls_log = sys.argv[1:6]
calls = []
real_check_output = subprocess.check_output


def check_output(argv, *a, **k):
    if argv[:2] == ["dpkg", "--print-architecture"]:
        return b"amd64\\n"
    return real_check_output(argv, *a, **k)


class Resp:
    def __init__(self, data):
        self.data = data
    def read(self):
        return self.data
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False


def urlopen(req, timeout=None):
    calls.append("marketplace")
    return Resp(Path(marketplace).read_bytes())


def urlretrieve(url, filename):
    calls.append("download:" + url)
    shutil.copy(vsix, filename)


subprocess.check_output = check_output
urllib.request.urlopen = urlopen
urllib.request.urlretrieve = urlretrieve
sys.argv = [helper, ext_id]
try:
    runpy.run_path(helper, run_name="__main__")
finally:
    Path(calls_log).write_text(json.dumps(calls))
'''


def run_helper(
    script_text: str, tmp_path: Path, bin_dir: Path, ext_id="leanprover.lean4", version="0.0.9"
):
    """Run the real helper (EXTENSIONS_DIR redirected under tmp_path); PATH holds only bin_dir."""
    ext_root = tmp_path / "extensions"
    src = helper_source(script_text)
    assert 'EXTENSIONS_DIR = "/home/user/.vscode-server/extensions"' in src
    src = src.replace(
        'EXTENSIONS_DIR = "/home/user/.vscode-server/extensions"', f'EXTENSIONS_DIR = "{ext_root}"'
    )
    helper = tmp_path / "ext-helper.py"
    helper.write_text(src)
    vsix = tmp_path / "fixture.vsix"
    with zipfile.ZipFile(vsix, "w") as z:
        z.writestr("extension/package.json", json.dumps({"name": "lean4", "version": version}))
        z.writestr("extension/fixture-marker.txt", "extracted by the real helper\n")
        z.writestr("[Content_Types].xml", "<Types/>")  # a real VSIX has more than the extension dir
    marketplace = tmp_path / "marketplace.json"
    marketplace.write_text(
        json.dumps(_canned_marketplace(ext_id, version, "https://example.invalid/x.vsix"))
    )
    driver = tmp_path / "driver.py"
    driver.write_text(DRIVER)
    calls_log = tmp_path / "calls.json"
    proc = subprocess.run(
        [
            sys.executable,
            str(driver),
            str(helper),
            ext_id,
            str(vsix),
            str(marketplace),
            str(calls_log),
        ],
        env={"PATH": str(bin_dir), "HOME": str(tmp_path)},
        capture_output=True,
        text=True,
        timeout=60,
        stdin=subprocess.DEVNULL,
    )
    calls = json.loads(calls_log.read_text()) if calls_log.exists() else []
    return proc, calls, ext_root


@pytest.mark.skipif(
    shutil.which("unzip") is None, reason="needs a real unzip to expose through the fake installer"
)
class TestExtensionInstallNeedsUnzipFromTheScript:
    """Claude and VS Code enabled, ``elan`` disabled: nothing but ``vscode.sh`` itself provides
    ``unzip``, and the helper must still extract a VSIX."""

    def test_tool_configuration_is_the_reported_one(self, monkeypatch):
        monkeypatch.setattr("bubble.tools._host_has_command", lambda cmd: False)
        enabled = resolve_tools({"editor": "vscode", "tools": {"claude": "yes", "elan": "no"}})
        assert "claude" in enabled and "vscode" in enabled and "elan" not in enabled

    def test_helper_extracts_with_the_unzip_the_preamble_installed(self, tmp_path):
        real_unzip = shutil.which("unzip")
        bin_dir = make_bin(tmp_path)
        assert not (bin_dir / "unzip").exists()  # no extractor until the script asks for one
        proc, calls = run_preamble(SCRIPT, tmp_path, expose_unzip=real_unzip)
        assert proc.returncode == 0, proc.stderr
        assert "python3" in calls[1].split()
        assert (bin_dir / "unzip").exists(), "the preamble did not request unzip"
        proc, calls, ext_root = run_helper(SCRIPT, tmp_path, bin_dir)
        assert proc.returncode == 0, f"stdout: {proc.stdout}\nstderr: {proc.stderr}"
        assert calls[0] == "marketplace" and calls[1].startswith("download:"), calls
        ext_dir = ext_root / "leanprover.lean4-0.0.9"
        assert (ext_dir / "fixture-marker.txt").read_text().startswith("extracted"), list(
            ext_root.rglob("*")
        )
        assert (ext_dir / "package.json").is_file()
        assert not (ext_dir / "extension").exists()  # nested extension/ contents were moved up
        manifest = json.loads((ext_root / "extensions.json").read_text())
        assert [(e["identifier"]["id"], e["version"]) for e in manifest] == [
            ("leanprover.lean4", "0.0.9")
        ]
        assert manifest[0]["relativeLocation"] == "leanprover.lean4-0.0.9"

    def test_helper_fails_without_unzip(self, tmp_path):
        """The bug this PR fixes, reproduced: the same helper with no unzip on PATH cannot install."""
        bin_dir = make_bin(tmp_path)
        proc, calls, ext_root = run_helper(SCRIPT, tmp_path, bin_dir)
        assert proc.returncode != 0
        assert "unzip" in proc.stderr and "FileNotFoundError" in proc.stderr, proc.stderr[-400:]
        assert calls[:2] == [
            "marketplace",
            "download:https://example.invalid/x.vsix",
        ]  # it got as far as extraction
        assert not (ext_root / "extensions.json").exists()
