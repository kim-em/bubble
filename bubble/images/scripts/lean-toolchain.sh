#!/bin/bash
set -euo pipefail

# Install a specific Lean toolchain version (passed via LEAN_TOOLCHAIN env var).
# Used to build toolchain-specific images like lean-v4.16.0.

if [ -z "${LEAN_TOOLCHAIN:-}" ]; then
    echo "Error: LEAN_TOOLCHAIN not set" >&2
    exit 1
fi

echo "Installing Lean toolchain: leanprover/lean4:$LEAN_TOOLCHAIN"
su - user -c "export PATH=\"\$HOME/.elan/bin:\$PATH\" && elan toolchain install \"leanprover/lean4:$LEAN_TOOLCHAIN\""
echo "Lean toolchain $LEAN_TOOLCHAIN installed."
