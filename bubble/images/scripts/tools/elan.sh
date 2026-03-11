#!/bin/bash
set -euo pipefail

export DEBIAN_FRONTEND=noninteractive

# Skip if elan is already installed (idempotent)
if [ -x /home/user/.elan/bin/elan ]; then
    echo "elan already installed, skipping."
    exit 0
fi

# Install dependencies for leantar download
apt-get update -qq && apt-get install -y -qq python3 unzip < /dev/null

# Install elan as user
su - user -c 'curl -sSf https://raw.githubusercontent.com/leanprover/elan/master/elan-init.sh | bash -s -- -y --default-toolchain none'

# Add elan to PATH for all sessions
echo 'export PATH="$HOME/.elan/bin:$PATH"' >> /home/user/.bashrc
echo 'export PATH="/home/user/.elan/bin:$PATH"' >> /etc/profile.d/elan.sh

# Pre-install leantar (used by lake exe cache get for mathlib)
echo "Installing leantar..."
ARCH=$(uname -m)
[ "$ARCH" = "arm64" ] && ARCH="aarch64"
LEANTAR_VERSION=$(curl -sSf https://api.github.com/repos/digama0/leangz/releases/latest \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['tag_name'].lstrip('v'))") \
  || true
if [ -z "$LEANTAR_VERSION" ]; then
  echo "Warning: could not determine leantar version (GitHub API may be rate-limited). Skipping leantar install."
else
  LEANTAR_URL="https://github.com/digama0/leangz/releases/download/v${LEANTAR_VERSION}/leantar-v${LEANTAR_VERSION}-${ARCH}-unknown-linux-musl.tar.gz"
  CACHE_DIR="/home/user/.cache/mathlib"
  mkdir -p "$CACHE_DIR"
  curl -sSfL "$LEANTAR_URL" -o /tmp/leantar.tar.gz
  tar -xf /tmp/leantar.tar.gz -C "$CACHE_DIR" --strip-components=1
  mv "$CACHE_DIR/leantar" "$CACHE_DIR/leantar-${LEANTAR_VERSION}"
  rm -f /tmp/leantar.tar.gz
  chown -R user:user /home/user/.cache
  echo "  leantar ${LEANTAR_VERSION} installed to ${CACHE_DIR}/leantar-${LEANTAR_VERSION}"
fi

# Clean up
apt-get clean
rm -rf /var/lib/apt/lists/*

echo "elan tool setup complete."
