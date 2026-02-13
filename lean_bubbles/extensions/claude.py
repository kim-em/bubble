"""Claude Code session persistence extension.

Manages extraction and injection of Claude Code .jsonl session files
for archive/reconstitute workflows. Also handles starting Claude
inside containers with proper configuration.
"""

import json
import subprocess
from pathlib import Path

from ..config import SESSIONS_DIR
from ..runtime.base import ContainerRuntime


def extract_sessions(runtime: ContainerRuntime, container: str,
                      bubble_name: str) -> Path | None:
    """Extract Claude Code session files from a container.

    Returns the session directory path, or None if no sessions found.
    """
    session_dir = SESSIONS_DIR / bubble_name
    session_dir.mkdir(parents=True, exist_ok=True)

    # Find all Claude-related files
    try:
        files = runtime.exec(container, [
            "bash", "-c",
            "find /home/lean/.claude -type f \\( -name '*.jsonl' -o -name '*.json' \\) 2>/dev/null || true",
        ]).strip()
    except Exception:
        return None

    if not files:
        return None

    pulled = 0
    for filepath in files.splitlines():
        filepath = filepath.strip()
        if not filepath:
            continue

        # Preserve relative path structure under .claude/
        rel = filepath.replace("/home/lean/.claude/", "")
        dest = session_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)

        try:
            subprocess.run(
                ["incus", "file", "pull", f"{container}{filepath}", str(dest)],
                check=True, capture_output=True,
            )
            pulled += 1
        except Exception:
            pass

    if pulled == 0:
        session_dir.rmdir()
        return None

    # Save metadata
    metadata = {
        "container": container,
        "files_extracted": pulled,
    }
    (session_dir / "metadata.json").write_text(json.dumps(metadata, indent=2) + "\n")

    return session_dir


def inject_sessions(runtime: ContainerRuntime, container: str,
                     bubble_name: str) -> bool:
    """Inject saved Claude Code session files into a container.

    Returns True if sessions were injected.
    """
    session_dir = SESSIONS_DIR / bubble_name
    if not session_dir.exists():
        return False

    # Create .claude directory in container
    runtime.exec(container, [
        "su", "-", "lean", "-c",
        "mkdir -p ~/.claude",
    ])

    injected = 0
    for src_file in session_dir.rglob("*"):
        if src_file.is_dir() or src_file.name == "metadata.json":
            continue

        # Compute destination path
        rel = src_file.relative_to(session_dir)
        dest_path = f"/home/lean/.claude/{rel}"

        # Ensure parent directory exists
        dest_dir = str(Path(dest_path).parent)
        runtime.exec(container, [
            "su", "-", "lean", "-c",
            f"mkdir -p {dest_dir}",
        ])

        try:
            subprocess.run(
                ["incus", "file", "push", str(src_file),
                 f"{container}{dest_path}"],
                check=True, capture_output=True,
            )
            # Fix ownership
            runtime.exec(container, [
                "chown", "lean:lean", dest_path,
            ])
            injected += 1
        except Exception:
            pass

    return injected > 0


def find_session_id(bubble_name: str) -> str | None:
    """Find the Claude Code session ID for a bubble from saved sessions."""
    session_dir = SESSIONS_DIR / bubble_name

    # Look for sessions-index.json first
    index_candidates = list(session_dir.rglob("sessions-index.json"))
    if index_candidates:
        try:
            index = json.loads(index_candidates[0].read_text())
            # Return the most recent session ID
            if isinstance(index, list) and index:
                return index[-1].get("sessionId") or index[-1].get("id")
            if isinstance(index, dict):
                sessions = index.get("sessions", [])
                if sessions:
                    return sessions[-1].get("sessionId") or sessions[-1].get("id")
        except (json.JSONDecodeError, KeyError, IndexError):
            pass

    # Fall back to finding the most recent .jsonl file
    jsonl_files = sorted(session_dir.rglob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    if jsonl_files:
        # Session ID is typically the filename without extension
        return jsonl_files[0].stem

    return None


def start_claude_in_container(runtime: ContainerRuntime, container: str,
                                project_dir: str, session_id: str | None = None,
                                unset_api_key: bool = True):
    """Start Claude Code inside a container.

    This launches Claude Code interactively, optionally resuming a previous session.
    """
    cmd_parts = ["claude"]

    if session_id:
        cmd_parts.extend(["--resume", session_id])

    env_prefix = ""
    if unset_api_key:
        env_prefix = "unset ANTHROPIC_API_KEY; "

    cmd = f"{env_prefix}cd {project_dir} && {' '.join(cmd_parts)}"

    # This needs to be interactive, so we use subprocess directly
    subprocess.run(
        ["incus", "exec", container, "--", "su", "-", "lean", "-c", cmd],
    )
