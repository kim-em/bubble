#!/bin/bash
set -euo pipefail

# Install GitHub CLI
# Checksums are injected as environment variables by bubble:
#   GH_GPG_KEY_SHA256
export DEBIAN_FRONTEND=noninteractive

# Download and verify GPG key
curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
    -o /tmp/githubcli-archive-keyring.gpg
echo "${GH_GPG_KEY_SHA256}  /tmp/githubcli-archive-keyring.gpg" | sha256sum -c -
cp /tmp/githubcli-archive-keyring.gpg /usr/share/keyrings/githubcli-archive-keyring.gpg
rm /tmp/githubcli-archive-keyring.gpg

chmod go+r /usr/share/keyrings/githubcli-archive-keyring.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
    | tee /etc/apt/sources.list.d/github-cli.list > /dev/null

apt-get update -qq
apt-get install -y -qq gh < /dev/null

echo "GitHub CLI installed: $(gh --version 2>/dev/null | head -1)"
