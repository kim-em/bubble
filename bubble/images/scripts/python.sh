#!/bin/bash
set -euo pipefail

export DEBIAN_FRONTEND=noninteractive

# Install uv if not already present (may have been installed via tools registry)
if [ -x /home/user/.local/bin/uv ]; then
    echo "uv already installed, skipping."
else
    echo "BUBBLE_PROGRESS: Installing uv..."
    curl -LsSf https://astral.sh/uv/install.sh | su - user -c 'bash'

    # Add uv to PATH for all sessions
    echo 'export PATH="$HOME/.local/bin:$PATH"' >> /home/user/.bashrc
    echo 'export PATH="/home/user/.local/bin:$PATH"' >> /etc/profile.d/uv.sh
fi

# Install ruff via uv
echo "BUBBLE_PROGRESS: Installing ruff..."
su - user -c 'export PATH="$HOME/.local/bin:$PATH" && uv tool install ruff'

echo "Python image setup complete."
