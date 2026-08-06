#!/bin/bash
set -euo pipefail

# Install the pinned native Kiro CLI bundle. Version and checksums are injected
# from pins.json; never execute Kiro's moving installer during an image build.
ARCH=$(dpkg --print-architecture)
case "$ARCH" in
    amd64)
        KIRO_ARCHIVE="kirocli-x86_64-linux.tar.xz"
        KIRO_SHA256="$KIRO_CLI_SHA256_X64"
        ;;
    arm64)
        # Kiro's GNU arm64 build requires glibc 2.39. The official musl artifact
        # is portable across Bubble's supported Debian/Ubuntu base images.
        KIRO_ARCHIVE="kirocli-aarch64-linux-musl.tar.xz"
        KIRO_SHA256="$KIRO_CLI_SHA256_ARM64"
        ;;
    *)
        echo "Unsupported architecture: $ARCH" >&2
        exit 1
        ;;
esac

KIRO_URL="https://prod.download.cli.kiro.dev/stable/${KIRO_CLI_VERSION}/${KIRO_ARCHIVE}"
curl -fsSL "$KIRO_URL" -o /tmp/kiro-cli.tar.xz
echo "${KIRO_SHA256}  /tmp/kiro-cli.tar.xz" | sha256sum -c -
tar -xJf /tmp/kiro-cli.tar.xz -C /tmp
install -m 0755 /tmp/kirocli/bin/kiro-cli /usr/local/bin/kiro-cli
install -m 0755 /tmp/kirocli/bin/kiro-cli-chat /usr/local/bin/kiro-cli-chat
install -m 0755 /tmp/kirocli/bin/kiro-cli-term /usr/local/bin/kiro-cli-term
rm -rf /tmp/kirocli /tmp/kiro-cli.tar.xz

echo "Kiro CLI installed: $(kiro-cli --version 2>/dev/null || echo 'unknown version')"
