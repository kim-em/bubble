#!/bin/bash
set -euo pipefail

# Skip if uv is already installed (idempotent)
# NOTE: Do not use `exit 0` here — this script may run as part of a
# combined tool script where exit would terminate the entire pipeline.
if [ -x /home/user/.local/bin/uv ]; then
    echo "uv already installed, skipping."
else

echo "BUBBLE_PROGRESS: Installing uv..."
curl -LsSf https://astral.sh/uv/install.sh | su - user -c 'bash'

# Add uv to PATH for all sessions
echo 'export PATH="$HOME/.local/bin:$PATH"' >> /home/user/.bashrc
echo 'export PATH="/home/user/.local/bin:$PATH"' >> /etc/profile.d/uv.sh

echo "uv tool setup complete."

fi  # end of uv install
