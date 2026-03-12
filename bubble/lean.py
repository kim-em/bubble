"""Shared Lean version constants."""

import re

# Matches stable releases (v4.16.0) and release candidates (v4.16.0-rc2).
# Used by hooks, image builder, and CLI commands — update once here.
LEAN_VERSION_RE = re.compile(r"^v\d+\.\d+\.\d+(-rc\d+)?$")
