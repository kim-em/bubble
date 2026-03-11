#!/bin/bash
set -euo pipefail

# Install GitHub CLI
export DEBIAN_FRONTEND=noninteractive

curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
    | dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg
chmod go+r /usr/share/keyrings/githubcli-archive-keyring.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
    | tee /etc/apt/sources.list.d/github-cli.list > /dev/null

apt-get update -qq
apt-get install -y -qq gh < /dev/null

echo "GitHub CLI installed: $(gh --version 2>/dev/null | head -1)"
