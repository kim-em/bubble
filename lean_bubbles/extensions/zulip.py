"""Zulip sandboxed access extension.

Provides sandboxed Zulip access for containers:
- Read operations use Kim's credentials (full access)
- Write operations use an AI account (attributed to the AI)

Also supports thread attachment mode where a bubble monitors
a Zulip thread and pipes messages to Claude.
"""

import textwrap
from pathlib import Path

from ..runtime.base import ContainerRuntime


def setup_zulip_sandbox(runtime: ContainerRuntime, container: str,
                         read_config: str = "~/.zuliprc",
                         write_config: str = "~/.zuliprc-ai"):
    """Set up sandboxed Zulip access in a container.

    Mounts two zuliprc files and installs a wrapper script that
    routes read operations through Kim's account and write operations
    through the AI account.
    """
    read_path = Path(read_config).expanduser()
    write_path = Path(write_config).expanduser()

    if not read_path.exists():
        raise FileNotFoundError(f"Zulip read config not found: {read_path}")
    if not write_path.exists():
        raise FileNotFoundError(f"Zulip write config not found: {write_path}")

    # Push config files into container
    import subprocess
    subprocess.run(
        ["incus", "file", "push", str(read_path),
         f"{container}/etc/zulip/read.zuliprc"],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["incus", "file", "push", str(write_path),
         f"{container}/etc/zulip/write.zuliprc"],
        check=True, capture_output=True,
    )

    # Set permissions
    runtime.exec(container, ["bash", "-c", textwrap.dedent("""\
        mkdir -p /etc/zulip
        chmod 600 /etc/zulip/read.zuliprc /etc/zulip/write.zuliprc
        chown lean:lean /etc/zulip/read.zuliprc /etc/zulip/write.zuliprc
    """)])

    # Install the wrapper script
    wrapper = _build_wrapper_script()
    runtime.exec(container, ["bash", "-c", f"cat > /usr/local/bin/zulip-sandbox << 'SCRIPT'\n{wrapper}\nSCRIPT\nchmod +x /usr/local/bin/zulip-sandbox"])

    # Install zulip CLI in container if not present
    try:
        runtime.exec(container, ["su", "-", "lean", "-c", "which zulip-send"])
    except Exception:
        runtime.exec(container, [
            "su", "-", "lean", "-c",
            "pip3 install --user zulip 2>/dev/null || pip install --user zulip",
        ])


def _build_wrapper_script() -> str:
    """Build the zulip-sandbox wrapper script."""
    return textwrap.dedent("""\
        #!/bin/bash
        # zulip-sandbox: Routes Zulip operations through appropriate accounts.
        # Read operations use Kim's credentials, write operations use AI account.

        COMMAND="$1"
        shift

        # Read-only operations use Kim's account
        READ_OPS="get-messages get-stream-topics get-subscribers list-members get-users get-realm get-stream-id"
        # Write operations use AI account
        WRITE_OPS="send-message update-message add-reaction send-reply"

        CONFIG="/etc/zulip/read.zuliprc"

        for op in $WRITE_OPS; do
            if [ "$COMMAND" = "$op" ]; then
                CONFIG="/etc/zulip/write.zuliprc"
                break
            fi
        done

        exec zulip "$COMMAND" --config-file "$CONFIG" "$@"
    """)


def parse_zulip_thread_url(url: str) -> dict:
    """Parse a Zulip thread URL into stream and topic.

    Handles URLs like:
    https://leanprover.zulipchat.com/#narrow/stream/287929-mathlib4/topic/some.20topic
    """
    import re
    import urllib.parse

    m = re.search(r"#narrow/stream/(\d+)-([^/]+)/topic/(.+?)(?:\?|$)", url)
    if not m:
        raise ValueError(f"Cannot parse Zulip thread URL: {url}")

    stream_id = int(m.group(1))
    stream_name = urllib.parse.unquote(m.group(2).replace(".", " ").replace("-", " "))
    topic = urllib.parse.unquote(m.group(3).replace(".", " "))

    return {
        "stream_id": stream_id,
        "stream_name": stream_name,
        "topic": topic,
        "url": url,
    }
