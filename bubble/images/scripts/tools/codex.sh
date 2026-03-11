#!/bin/bash
set -euo pipefail

# Install OpenAI Codex CLI (requires Node.js)
# Versions and checksums are injected as environment variables by bubble:
#   NODE_VERSION, NODE_SHA256_X64, NODE_SHA256_ARM64, CODEX_VERSION
export DEBIAN_FRONTEND=noninteractive

# Install Node.js from official tarball if not present
if ! command -v node &>/dev/null || ! command -v npm &>/dev/null; then
    ARCH=$(dpkg --print-architecture)
    case "$ARCH" in
        amd64) NODE_SHA256="$NODE_SHA256_X64" ; NODE_ARCH="x64" ;;
        arm64) NODE_SHA256="$NODE_SHA256_ARM64" ; NODE_ARCH="arm64" ;;
        *) echo "Unsupported architecture: $ARCH" >&2; exit 1 ;;
    esac
    curl -fsSL "https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-${NODE_ARCH}.tar.xz" \
        -o /tmp/node.tar.xz
    echo "${NODE_SHA256}  /tmp/node.tar.xz" | sha256sum -c -
    tar -xJf /tmp/node.tar.xz -C /usr/local --strip-components=1
    rm /tmp/node.tar.xz
fi

npm install -g "@openai/codex@${CODEX_VERSION}"

echo "Codex installed: $(codex --version 2>/dev/null || echo 'unknown version')"
