#!/bin/bash
set -euo pipefail

# Install Mistral Vibe (mistralai/mistral-vibe): a CLI coding agent, and the
# home of the Leanstral Lean 4 agent (activate per-user with `/leanstall`, then
# `vibe --agent lean`). Unlike the npm-based agents, vibe is Python and is
# installed via uv. The version is injected by bubble as VIBE_VERSION.
export DEBIAN_FRONTEND=noninteractive

# Skip if vibe is already installed (idempotent).
# NOTE: Do not use `exit 0` here — this script may run as part of a combined
# tool script where exit would terminate the entire pipeline.
if [ -x /home/user/.local/bin/vibe ]; then
    echo "vibe already installed, skipping."
else

# vibe requires Python >= 3.12 (present on Ubuntu 24.04) for uv's tool venv.
apt-get update -qq && apt-get install -y -qq python3 < /dev/null
apt-get clean
rm -rf /var/lib/apt/lists/*

# Bootstrap uv if it isn't already present. Mirrors tools/uv.sh so vibe works
# whether or not the standalone `uv` tool is enabled.
if [ ! -x /home/user/.local/bin/uv ]; then
    curl -LsSf https://astral.sh/uv/install.sh | su - user -c 'bash'
fi

# Install the pinned mistral-vibe release as the user.
su - user -c "/home/user/.local/bin/uv tool install 'mistral-vibe==${VIBE_VERSION}'"

# Ensure ~/.local/bin (uv's tool bin dir) is on PATH for all sessions. Guard
# against duplicating the entry the uv tool may already have written.
if ! grep -q '/.local/bin' /home/user/.bashrc 2>/dev/null; then
    echo 'export PATH="$HOME/.local/bin:$PATH"' >> /home/user/.bashrc
fi
if [ ! -f /etc/profile.d/uv.sh ]; then
    echo 'export PATH="/home/user/.local/bin:$PATH"' >> /etc/profile.d/vibe.sh
fi

echo "vibe installed: $(su - user -c '/home/user/.local/bin/vibe --version' 2>/dev/null || echo 'unknown version')"

fi  # end of vibe install
