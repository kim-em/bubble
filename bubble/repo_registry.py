"""Learned repo name registry for short name resolution."""

import json
from datetime import datetime, timezone
from pathlib import Path

from .config import REPOS_FILE


class RepoRegistry:
    """Maps short repo names to full owner/repo pairs.

    Repos are learned on first use and stored in ~/.bubble/repos.json.
    """

    def __init__(self, path: Path | None = None):
        self._path = path or REPOS_FILE
        self._repos: dict[str, dict] = {}  # short_name -> {"owner": ..., "repo": ..., ...}
        self._ambiguous: dict[str, list[str]] = {}  # short_name -> [owner/repo, ...]
        self._load()

    def _load(self):
        if self._path.exists():
            try:
                data = json.loads(self._path.read_text())
                self._repos = data.get("repos", {})
                self._ambiguous = data.get("ambiguous", {})
            except (json.JSONDecodeError, OSError):
                self._repos = {}
                self._ambiguous = {}

    def _save(self):
        self._path.parent.mkdir(parents=True, exist_ok=True)
        data = {"repos": self._repos, "ambiguous": self._ambiguous}
        self._path.write_text(json.dumps(data, indent=2) + "\n")

    def resolve(self, short_name: str) -> str | None:
        """Resolve a short name to owner/repo. Returns None if unknown or ambiguous."""
        lower = short_name.lower()
        if lower in self._ambiguous:
            return None
        entry = self._repos.get(lower)
        if entry:
            return f"{entry['owner']}/{entry['repo']}"
        return None

    def register(self, owner: str, repo: str):
        """Record a repo usage. Auto-learns short name mapping."""
        short = repo.lower()
        org_repo = f"{owner}/{repo}"
        now = datetime.now(timezone.utc).isoformat()

        # Check for ambiguity
        existing = self._repos.get(short)
        if existing:
            existing_org_repo = f"{existing['owner']}/{existing['repo']}"
            if existing_org_repo == org_repo:
                # Same repo, just update last_used
                existing["last_used"] = now
                self._save()
                return
            else:
                # Different repo with same short name — mark ambiguous
                self._ambiguous.setdefault(short, [existing_org_repo])
                if org_repo not in self._ambiguous[short]:
                    self._ambiguous[short].append(org_repo)
                del self._repos[short]
                self._save()
                return

        # Check if already in ambiguous list
        if short in self._ambiguous:
            if org_repo not in self._ambiguous[short]:
                self._ambiguous[short].append(org_repo)
            self._save()
            return

        # New unambiguous entry
        self._repos[short] = {
            "owner": owner,
            "repo": repo,
            "last_used": now,
        }
        self._save()

    def is_ambiguous(self, short_name: str) -> bool:
        return short_name.lower() in self._ambiguous

    def get_ambiguous_options(self, short_name: str) -> list[str]:
        return self._ambiguous.get(short_name.lower(), [])

    def list_all(self) -> dict[str, str]:
        """Return all known short_name -> owner/repo mappings."""
        return {k: f"{v['owner']}/{v['repo']}" for k, v in self._repos.items()}
