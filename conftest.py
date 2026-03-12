"""Root conftest: ensure the local worktree's bubble package is importable.

uv's editable install finder hardcodes the package path from whichever
worktree created the venv.  When pytest runs from a different worktree it
picks up the main venv's finder and imports the *wrong* copy of bubble.

Prepending the project root to sys.path before any bubble imports fixes this
because a plain sys.path entry beats the editable-install meta path finder.
"""

import pathlib
import sys

_project_root = str(pathlib.Path(__file__).resolve().parent)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

# If bubble was already imported from the wrong location, force a reimport
# from the correct path.
_to_remove = [key for key in sys.modules if key == "bubble" or key.startswith("bubble.")]
for key in _to_remove:
    del sys.modules[key]
