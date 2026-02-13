#!/bin/bash
set -euo pipefail

# Build on top of lean-base image.
# Clones batteries using shared git objects and builds it.

REPO="leanprover-community/batteries"
SHORT="batteries"
SHARED_GIT="/shared/git/batteries.git"

# Clone batteries using shared objects if available
if [ -d "$SHARED_GIT" ]; then
    su - lean -c "git clone --reference $SHARED_GIT https://github.com/$REPO.git /home/lean/$SHORT"
else
    su - lean -c "git clone https://github.com/$REPO.git /home/lean/$SHORT"
fi

# Build batteries
su - lean -c "cd /home/lean/$SHORT && ~/.elan/bin/lake build"

echo "lean-batteries setup complete."
