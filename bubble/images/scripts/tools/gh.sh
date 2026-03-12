#!/bin/bash
set -euo pipefail

# Install GitHub CLI and set up bubble-managed gh configuration.
# gh is configured to route all HTTP traffic through the auth proxy
# via http_unix_socket, so the host's GitHub token never enters the
# container.
export DEBIAN_FRONTEND=noninteractive

# Install gh from official apt repository
curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
    | dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg 2>/dev/null
chmod go+r /usr/share/keyrings/githubcli-archive-keyring.gpg

echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
    | tee /etc/apt/sources.list.d/github-cli.list > /dev/null

apt-get update -qq
apt-get install -y -qq gh

# Create bubble-managed gh config directory.
# GH_CONFIG_DIR is set at container creation time (not image build time)
# because the auth token is per-container.
mkdir -p /etc/bubble/gh

# Pre-populate config.yml with http_unix_socket pointing to the
# auth proxy socket exposed by Incus proxy device.
cat > /etc/bubble/gh/config.yml <<'GHCONF'
http_unix_socket: /bubble/gh-proxy.sock
GHCONF

echo "gh installed: $(gh --version 2>/dev/null | head -1 || echo 'unknown version')"
