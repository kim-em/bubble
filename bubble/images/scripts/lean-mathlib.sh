#!/bin/bash
set -euo pipefail

# Build on top of lean-base image.
# Clones mathlib4 using shared git objects and fetches the .olean cache.

REPO="leanprover-community/mathlib4"
SHORT="mathlib4"
SHARED_GIT="/shared/git/mathlib4.git"

# Clone mathlib4 using shared objects if available
if [ -d "$SHARED_GIT" ]; then
    su - lean -c "git clone --reference $SHARED_GIT https://github.com/$REPO.git /home/lean/$SHORT"
else
    su - lean -c "git clone https://github.com/$REPO.git /home/lean/$SHORT"
fi

# Fetch the .olean cache
su - lean -c "cd /home/lean/$SHORT && ~/.elan/bin/lake exe cache get"

echo "lean-mathlib setup complete."
