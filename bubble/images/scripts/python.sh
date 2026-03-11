#!/bin/bash
set -euo pipefail

export DEBIAN_FRONTEND=noninteractive

# Install uv (Python package manager from Astral)
echo "Installing uv..."
curl -LsSf https://astral.sh/uv/install.sh | su - user -c 'bash'

# Add uv to PATH for all sessions
echo 'export PATH="$HOME/.local/bin:$PATH"' >> /home/user/.bashrc
echo 'export PATH="/home/user/.local/bin:$PATH"' >> /etc/profile.d/uv.sh

# Install ruff via uv
echo "Installing ruff..."
su - user -c 'export PATH="$HOME/.local/bin:$PATH" && uv tool install ruff'

echo "Python image setup complete."
