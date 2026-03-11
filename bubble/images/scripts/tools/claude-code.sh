#!/bin/bash
set -euo pipefail

# Install Claude Code (requires Node.js)
export DEBIAN_FRONTEND=noninteractive

# Install Node.js via NodeSource if not present
if ! command -v node &>/dev/null; then
    curl -fsSL https://deb.nodesource.com/setup_22.x | bash -
    apt-get install -y -qq nodejs < /dev/null
fi

npm install -g @anthropic-ai/claude-code

echo "Claude Code installed: $(claude --version 2>/dev/null || echo 'unknown version')"
