#!/bin/bash
set -euo pipefail

# Build on top of lean-base image.
# Clones lean4 and sets up stage0/stage1 elan toolchain overrides.

REPO="leanprover/lean4"
SHORT="lean4"
SHARED_GIT="/shared/git/lean4.git"

# Install additional build dependencies for lean4
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq cmake ninja-build python3 < /dev/null
apt-get clean
rm -rf /var/lib/apt/lists/*

# Clone lean4 using shared objects if available
if [ -d "$SHARED_GIT" ]; then
    su - lean -c "git clone --reference $SHARED_GIT https://github.com/$REPO.git /home/lean/$SHORT"
else
    su - lean -c "git clone https://github.com/$REPO.git /home/lean/$SHORT"
fi

# Set up elan overrides for stage0 and stage1
# stage0 uses the lean-toolchain file's toolchain
# The build directory gets its own override pointing to the built stage1
su - lean -c "cd /home/lean/$SHORT && ~/.elan/bin/elan override set \$(cat lean-toolchain)"

echo "lean-lean4 setup complete."
