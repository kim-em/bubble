# bubble Behavioral Specification

This document specifies the observable behavior of `bubble`, a CLI tool that
creates containerized development environments from GitHub repositories. The
spec is **prescriptive about observable behavior** (what the user sees, what
files are created, what external commands are run) but **relaxed about
implementation details** (language, data structures, concurrency model).

Two implementations that produce the same behavior from the user's perspective
are both correct.

The spec is organized into stages, each building on the last. Each stage
produces a usable tool.

---

## Data directory

All persistent state lives under `~/.bubble/` (overridable via
`BUBBLE_HOME` environment variable). The implementation MUST create this
directory and its children on demand.

---

## Stage 1 — Local containers from GitHub URLs

The minimum viable product: parse a GitHub URL, clone the repo into an Incus
container, and open it in an editor.

### 1.1 CLI surface

```
bubble TARGET [OPTIONS]
bubble COMMAND [ARGS]...
```

The first positional argument is auto-routed to the implicit `open` command
unless it matches a known subcommand name. This means `bubble <url>` is
equivalent to `bubble open <url>`.

**Open command flags (Stage 1):**

| Flag | Type | Default | Description |
|------|------|---------|-------------|
| `--editor` | `vscode\|emacs\|neovim\|shell` | config or `vscode` | Editor to use |
| `--shell` | flag | | Shortcut for `--editor shell` |
| `--emacs` | flag | | Shortcut for `--editor emacs` |
| `--neovim` | flag | | Shortcut for `--editor neovim` |
| `--no-interactive` | flag | | Create but don't attach |
| `--machine-readable` | flag (hidden) | | Output JSON for orchestration |
| `--network/--no-network` | flag | `--network` | Apply network allowlist |
| `--name` | string | | Custom container name |
| `--command` | string | | Run command via SSH (implies shell) |
| `--native` | flag | | Non-containerized workspace |
| `--path` | flag | | Force interpretation as local path |
| `-b`, `--new-branch` | string | | Create a new branch |
| `--base` | string | | Base branch for `-b` |
| `--mount` | string (repeatable) | | Mount host dir into container |
| `--claude-config/--no-claude-config` | flag | enabled | Mount ~/.claude config read-only |
| `--claude-credentials/--no-claude-credentials` | flag | enabled | Mount Claude credentials |
| `--codex-credentials/--no-codex-credentials` | flag | enabled | Mount Codex credentials |
| `--ssh HOST` | string | | Run on remote host |
| `--cloud` | flag | | Run on Hetzner Cloud server |
| `--local` | flag | | Force local execution |
| `--no-clone` | flag (hidden) | | Fail if bare repo missing (relay) |
| `-h`, `--help` | flag | | Show help |
| `--version` | flag | | Show version |

Note: Many of these flags correspond to features defined in later stages. An
implementation MAY defer adding these flags until the relevant stage is
implemented. They are listed here for completeness since the `open` command is
the central entry point through which most features are accessed.

### 1.2 Target parsing

A target string is parsed into `(owner, repo, kind, ref)` where `kind` is one
of: `pr`, `issue`, `branch`, `commit`, `repo`.

**GitHub URLs:**

| Input | Parsed as |
|-------|-----------|
| `https://github.com/owner/repo/pull/123` | kind=pr, ref=123 |
| `https://github.com/owner/repo/issues/456` | kind=issue, ref=456 |
| `https://github.com/owner/repo/tree/branch-name` | kind=branch, ref=branch-name |
| `https://github.com/owner/repo/commit/abc123` | kind=commit, ref=abc123 |
| `https://github.com/owner/repo` | kind=repo, ref="" |
| `github.com/owner/repo/pull/123` | Same (scheme optional) |
| `owner/repo/pull/123` | Same (host optional) |
| `owner/repo` | kind=repo |

URL fragments (`#issuecomment-...`) and query strings (`?query=...`) are
stripped before parsing. Trailing slashes are stripped.

**Error cases:**
- Empty target after stripping → error
- Non-numeric PR/issue number → error
- Unparseable format → error with suggestion to use `owner/repo` format

### 1.3 Git store (bare repo management)

**Location:** `~/.bubble/git/`

On first use of a repository, create a bare mirror:

```
git clone --bare https://github.com/owner/repo.git ~/.bubble/git/repo.git
```

Then configure fetch refs:

```
git -C ~/.bubble/git/repo.git config remote.origin.fetch '+refs/heads/*:refs/heads/*'
git -C ~/.bubble/git/repo.git config --add remote.origin.fetch '+refs/tags/*:refs/tags/*'
git -C ~/.bubble/git/repo.git config --add remote.origin.fetch '+refs/pull/*/head:refs/pull/*/head'
```

The bare repo path is derived from the repo name only (not the owner):
`~/.bubble/git/<repo>.git`.

**PR ref fetching:** When the target is a PR, fetch the specific ref:

```
git -C ~/.bubble/git/repo.git fetch origin refs/pull/123/head:refs/pull/123/head
```

**Concurrency:** Bare repo operations (init, fetch) MUST use per-repo file
locking (`~/.bubble/git/<repo>.git.lock`) to prevent concurrent git operations
from corrupting the repo.

**Idempotency:** `init_bare_repo` is idempotent — if the bare repo exists,
return immediately.

### 1.4 Container naming

Names follow the pattern `<repo>-<source>-<id>`:

| Target | Name |
|--------|------|
| PR #12345 of mathlib4 | `mathlib4-pr-12345` |
| Branch `fix-grind` of batteries | `batteries-branch-fix-grind` |
| Issue #456 of lean4 | `lean4-issue-456` |
| Commit abc123def012 of lean4 | `lean4-commit-abc123def012` |
| Default branch of lean4 | `lean4-main-20260312` (today's date) |

**Sanitization rules:**
- Lowercase all characters
- Replace non-alphanumeric characters (except hyphens) with hyphens
- Collapse multiple consecutive hyphens
- Strip leading/trailing hyphens
- If name starts with a digit, prepend `b-`

**Deduplication:** If a name already exists among running containers, append
`-2`, `-3`, etc. up to `-999`.

### 1.5 Image building

Images are built by launching a container from a parent image, running a setup
script inside it, stopping it, and publishing it as a new image.

**Image hierarchy:**

| Image | Parent | Script |
|-------|--------|--------|
| `base` | `images:ubuntu/24.04` (Incus remote) | `base.sh` |

**Base image contents:**
- Ubuntu 24.04 with: `git`, `curl`, `build-essential`, `cmake`, `openssh-server`, `ca-certificates`, `netcat-openbsd`, `iptables`
- User `user` (no sudo, password locked, shell `/bin/bash`)
- SSH configured: key-based auth only, root login disabled
- Mount points: `/shared/git/` (for bare repos), `/bubble/` (for relay socket)
- Relay client stub at `/usr/local/bin/bubble`
- Shell hook in `/home/user/.profile` for auto-build marker consumption

**Builder container naming:** `<image>-builder` (e.g., `base-builder`). Cleaned
up after build completes. On failure, leftover builders are force-deleted on next
build attempt.

**Build locking:** Image builds use per-image file locks
(`/tmp/bubble-build-locks/<image>.lock`) to prevent concurrent builds.

**Waiting for container readiness:** After launching a builder, wait for:
1. Container to be exec-able (up to 60s)
2. IPv4 + DNS to work (up to 15s for DHCP)
3. If DHCP/DNS fail, apply workarounds (static IPv4, DNS proxy device)

### 1.6 Container provisioning

Given a target, bare repo path, and image name:

1. Launch an Incus container from the image
2. Wait for it to be ready (exec-able + network)
3. Mount the bare repo read-only at `/shared/git/<repo>.git` (Incus disk device)
4. Start sshd
5. Inject SSH public keys from `~/.ssh/*.pub`
6. Clone the repo inside the container:
   ```
   git clone --reference /shared/git/repo.git https://github.com/owner/repo.git /home/user/<repo>
   ```
7. Checkout the appropriate ref (see 1.7)
8. Configure git identity (`user.name`, `user.email`) from host
9. Apply network allowlist (if `--network`, see Stage 5)
10. Generate SSH config entry (see 1.8)
11. Register in lifecycle registry (see Stage 4)
12. Open editor (see 1.9)

**Failure cleanup:** If provisioning fails at any point after the container is
created, force-delete the partially-provisioned container.

### 1.7 Repo checkout

After cloning the default branch, checkout depends on the target kind:

| Kind | Behavior |
|------|----------|
| `repo` | Stay on default branch |
| `branch` | `git switch <branch>` |
| `commit` | `git checkout <sha>` |
| `pr` | Query GitHub API for head branch info. If same-repo PR: fetch and checkout with tracking. If fork PR: add fork remote, fetch, checkout with tracking. Fallback: `git fetch origin pull/N/head:pr-N && git checkout pr-N` |
| `issue` | Create branch `issue-<N>` from default branch |

**New branch mode (`-b`):** When `-b <branch>` is specified, create a new
branch from `--base` (or the default branch if `--base` is omitted).

### 1.8 SSH configuration

**Config file:** `~/.ssh/config.d/bubble`

**Include directive:** Prepend `Include ~/.ssh/config.d/*` to `~/.ssh/config`
if not already present.

**Entry format:**
```
Host bubble-<name>
  User user
  ProxyCommand incus exec <name> -- su - user -c "nc localhost 22"
  StrictHostKeyChecking no
  UserKnownHostsFile /dev/null
  LogLevel ERROR
```

The ProxyCommand uses `incus exec` instead of port forwarding because port
forwarding doesn't work reliably through Colima on macOS.

### 1.9 Editor launching

| Editor | Behavior |
|--------|----------|
| `vscode` | `code --disable-workspace-trust --folder-uri vscode-remote://ssh-remote+bubble-<name>/home/user/<repo>` |
| `emacs` | `ssh bubble-<name> -t "cd /home/user/<repo> && emacs ."` |
| `neovim` | `ssh bubble-<name> -t "cd /home/user/<repo> && nvim ."` |
| `shell` | `ssh bubble-<name>` |

For `--command CMD`: `ssh bubble-<name> <cmd args...>`

### 1.10 Reattachment

Before creating a new container, check if one already exists for the target:
1. Check by exact container name match against running containers
2. Check by generated name match against running containers
3. Check by registry lookup — but only for `pr` and `branch` kinds (matches
   on `org_repo` + `kind` + `ref`). Other kinds (`repo`, `issue`, `commit`)
   do not support registry-based reattachment.

If found:
- Ensure it's running (start if stopped/frozen)
- If working tree is clean and branch has an upstream, `git pull --ff-only`
- Open the editor

**Idempotency:** Running `bubble <target>` twice produces the same result — the
second invocation reattaches to the existing container.

### 1.11 macOS support (Colima)

Incus requires Linux. On macOS, a Colima VM provides the Linux environment:
- VM type: Apple Virtualization.Framework (`--vm-type vz`)
- Default resources: all host CPUs, 16 GB RAM, 60 GB disk
- Configurable via `[runtime]` config section

The implementation MUST start Colima automatically if needed.

### 1.12 Automation

On first container creation, install periodic automation:

**macOS (launchd):**
- `com.bubble.git-update` — hourly `bubble git update` (fetch all bare repos)
- `com.bubble.image-refresh` — weekly (Sunday 3am) `bubble images build base`

**Linux (systemd user timers):**
- `bubble-git-update.timer` — hourly
- `bubble-image-refresh.timer` — weekly (Sunday 3am)

---

## Stage 2 — Language hooks and images

Hooks detect project types and customize the container accordingly.

### 2.1 Hook interface

A hook provides:
- `detect(bare_repo_path, ref) → bool` — check bare repo for language markers
- `image_name() → string` — container image to use
- `post_clone(container, project_dir)` — run after cloning
- `network_domains() → list[string]` — extra domains for allowlist
- `shared_mounts() → list[(host_dir, container_path, env_var)]` — writable shared mounts
- `git_dependencies() → list[GitDependency]` — deps to pre-populate
- `workspace_file(project_dir) → string|null` — VS Code workspace file path

Hook detection runs against the host bare repo via `git show <ref>:<file>` — no
container needed.

### 2.2 Lean 4 hook

**Detection:** `lean-toolchain` file exists at the target ref.

**Image selection:**
- Stable/RC version (matches `v\d+\.\d+\.\d+(-rc\d+)?`): image `lean-v4.X.Y`
- Nightly or unknown: image `lean`

**Image hierarchy:**

| Image | Parent | Script |
|-------|--------|--------|
| `lean` | `base` | `lean.sh` (fallback elan install) |
| `lean-v4.X.Y` | `lean` | `lean-toolchain.sh` (installs specific toolchain) |

**Lean toolchain images are lazy-built:** If `lean-v4.X.Y` doesn't exist, use
the plain `lean` image (elan downloads on demand) and trigger a background build
of the versioned image for next time.

**Shared mounts:** If the project uses Mathlib (is `mathlib4` itself, or has a
`mathlib` dependency in `lake-manifest.json`):
- `~/.bubble/mathlib-cache/` ↔ `/shared/mathlib-cache/` (read-write)
- Environment variable `MATHLIB_CACHE_DIR=/shared/mathlib-cache`

**Post-clone behavior:**
- Pre-populate Lake dependencies from `lake-manifest.json` (see 2.4)
- Write auto-build command to `~/.bubble-fetch-cache` marker file
- For Mathlib-using projects: `cd <dir> && lake exe cache get && lake build`
- For the lean4 repo itself: cmake + make build
- For other Lean projects: `cd <dir> && lake build`

**Network domains:** `releases.lean-lang.org`, `reservoir.lean-lang.org`,
`reservoir.lean-cache.cloud`, `mathlib4.lean-cache.cloud`,
`lakecache.blob.core.windows.net`

### 2.3 Python hook

**Detection:** `pyproject.toml` file exists at the target ref.

**Image:**

| Image | Parent | Script |
|-------|--------|--------|
| `python` | `base` | `python.sh` |

**Post-clone behavior:**
- Writes `cd <dir> && uv sync` to `~/.bubble-fetch-cache` marker file
- `uv sync` runs automatically on first SSH login

**Network domains:** `pypi.org`, `files.pythonhosted.org`

### 2.4 Lake dependency pre-population

Parse `lake-manifest.json` from the bare repo. For each git dependency with a
GitHub URL:

1. Init/fetch the dependency's bare repo on the host
2. Verify the manifest's commit SHA is available
3. Mount the dependency bare repo into the container
4. Clone using `--reference` for zero-copy object sharing:
   ```
   git clone --reference /shared/git/dep.git file:///shared/git/dep.git .lake/packages/<name>
   ```
5. Set the remote URL to the original GitHub URL
6. Checkout the exact manifest revision

This is best-effort — failures are non-fatal (Lake will clone the dep normally).

**Validation:** Package names must match `[A-Za-z0-9._-]+`. Revisions must be
40-character hex SHAs. Non-GitHub URLs are skipped.

### 2.5 Build marker consumption

The `~/.bubble-fetch-cache` file contains a shell command. It is consumed:
- By the `.profile` shell hook on SSH login (for shell/emacs/neovim editors)
- By the editor launcher for emacs/neovim (reads file, starts command in background)
- By the VS Code Lean extension (if installed)

The marker is only consumed when `SSH_CONNECTION` is set (to avoid triggering
during provisioning).

---

## Stage 3 — Target shorthand and local paths

### 3.1 Short names and the repo registry

**File:** `~/.bubble/repos.json`

```json
{
  "repos": {
    "mathlib4": {"owner": "leanprover-community", "repo": "mathlib4"}
  },
  "ambiguous": {
    "core": ["owner1/core", "owner2/core"]
  }
}
```

**Learning:** When a target is successfully parsed with a full `owner/repo`,
register the short name. If the short name already maps to a different
owner/repo, move it to the `ambiguous` set.

**Built-in defaults:** A bundled `default_repos.json` provides fallback
resolution for well-known repositories. User-learned repos take precedence.

**Additional target forms:**

| Input | Parsed as |
|-------|-----------|
| `mathlib4` | Resolved via registry → kind=repo |
| `mathlib4/pull/123` | Resolved via registry → kind=pr |
| `mathlib4/issues/456` | Resolved via registry → kind=issue |
| `mathlib4/tree/branch` | Resolved via registry → kind=branch |

**Error cases:**
- Ambiguous short name → error listing options
- Unknown short name → error suggesting full `owner/repo` format, with hint about `./name` for local paths

### 3.2 Bare PR/issue numbers

```
bubble 123
```

When the target is a bare number:
1. Extract `owner/repo` from the current directory's git remote (origin)
2. Query GitHub API (`gh api repos/owner/repo/pulls/123`) to determine if it's a PR or issue
3. Default to PR if the API is unavailable

**Error:** If the current directory is not a git repo with a GitHub remote, error.

### 3.3 Local paths

Targets starting with `.`, `..`, or `/` are treated as local filesystem paths.

**Validation:**
- Path must exist
- Must be a git repo (has `.git`)
- Must have an `origin` remote pointing to GitHub
- Working tree must be clean (no modified/staged files; untracked OK)
- HEAD must not be detached

**Behavior:** The local `.git` directory is used as the `--reference` source
(instead of a bare repo), enabling containers to access unpushed branches.

**`--path` flag:** Forces interpretation as a local path (useful for names that
could be short names, e.g., `bubble --path mydir`). Prepends `./` if the target
doesn't already start with `/`, `.`, or `..`.

### 3.4 Multiple targets

```
bubble 12 13 14
```

Multiple targets open multiple bubbles sequentially. Errors for individual
targets are collected and reported at the end.

**Restrictions with multiple targets:**
- `--name` cannot be used (ambiguous)
- `-b` cannot be used (ambiguous)
- `--machine-readable` cannot be used

### 3.5 `-b` without explicit target

```
bubble -b my-branch
```

When `-b` is used without a target, infer `owner/repo` from the current
directory's git remote.

---

## Stage 4 — Lifecycle management

### 4.1 Commands

**`bubble list [--json] [-v|--verbose] [-c|--clean]`**

List active bubbles. Shows local containers, native workspaces, and remote
bubbles. Verbose mode includes IP and disk usage. Clean mode checks each
container's cleanness status.

JSON output format:
```json
[
  {
    "name": "mathlib4-pr-12345",
    "state": "running",
    "location": "local",
    "created_at": "2026-03-12T10:30:00+00:00",
    "last_used_at": "2026-03-12T10:30:00+00:00"
  }
]
```

With `-v`/`--verbose`, adds `ipv4` (string) and `disk_usage` (bytes, number).
With `-c`/`--clean`, adds `clean` (object with `status` bool and `reasons`
list, or null on error).

**`bubble pause NAME`**

Freeze a running container. Auto-routes to remote host if the bubble's registry
entry has `remote_host` set.

**`bubble pop NAME [-f|--force]`**

Destroy a bubble permanently:
1. With `-f`: skip cleanness checks entirely.
2. Without `-f`: check if clean (see 4.2). If clean, prompt for confirmation.
   If dirty, show the reasons then prompt for confirmation. The user can
   always confirm to proceed — `pop` does not refuse, it warns.
3. Delete the container (`incus delete --force`)
4. Remove SSH config entry
5. Remove auth proxy tokens
6. Remove relay tokens
7. Unregister from registry

**For native workspaces:** Delete the directory under `~/.bubble/native/`.
Safety check: refuse to delete paths not under `~/.bubble/native/`.

### 4.2 Cleanness checking

A container is "clean" (safe to discard) when ALL of:
1. No unexpected non-hidden files in `/home/user/` besides the project directory
2. Git working tree is clean (no modified/staged/untracked files)
3. No git stashes
4. No unpushed commits (checks all branches against their upstream, or against the initial commit SHA for branches without upstream)

**Reasons returned:**
- `extra_files` — unexpected files in home
- `dirty_worktree` — uncommitted changes
- `stashes` — git stashes exist
- `unpushed:<branch>` — commits not pushed on named branch
- `untracked_branch:<branch>` — branch with no upstream and no initial commit to compare

### 4.3 Registry

**File:** `~/.bubble/registry.json`

```json
{
  "bubbles": {
    "mathlib4-pr-12345": {
      "org_repo": "leanprover-community/mathlib4",
      "branch": "pr-12345",
      "commit": "",
      "pr": 12345,
      "created_at": "2026-03-12T10:30:00+00:00",
      "base_image": "lean-v4.16.0",
      "remote_host": "user@example.com",
      "native": true,
      "native_path": "/home/user/.bubble/native/project"
    }
  }
}
```

**Always present:** `org_repo`, `branch`, `commit`, `pr`, `created_at`.
**Conditionally present** (only written when truthy): `base_image`,
`remote_host`, `native`, `native_path`.

Registry modifications MUST use file locking to prevent concurrent corruption.
Writes MUST be atomic (write to temp file, rename).

### 4.4 `bubble git update`

Update all bare repos in `~/.bubble/git/`:

```
git -C ~/.bubble/git/<repo>.git fetch --all --prune
```

For each `*.git` directory found. Uses per-repo file locking.

### 4.5 `bubble images build IMAGE`

Build or rebuild a specific image. When rebuilding `base`, all derived images
(`lean`, `lean-v4.X.Y`, etc.) are purged so they rebuild from the fresh base
on next use.

---

## Stage 5 — Network isolation

### 5.1 Mechanism

Network allowlisting uses iptables rules **inside the container** (not Incus
ACLs), for portability across Colima/native setups. Rules are applied by
`incus exec` as root — the `user` account has no sudo and cannot modify them.

### 5.2 Rules

**IPv6:** Blocked entirely (DROP policy on OUTPUT, only loopback allowed).

**IPv4 OUTPUT chain:**
1. Allow loopback
2. Allow established/related connections
3. Allow DNS to container's configured resolver (from `/etc/resolv.conf`)
4. Allow DNS to upstream servers (from `resolvectl dns`)
5. For each allowed domain: resolve to IPs, allow the `/24` CIDR block
6. Default policy: DROP

**Why /24 CIDRs:** CDN domains (e.g., `*.githubusercontent.com`) rotate IPs
within their allocation. A point-in-time resolution may miss IPs returned later.

**SSH is NOT allowed outbound.** VS Code uses the ProxyCommand (incus exec)
which bypasses the container network entirely.

### 5.3 Domain sources

Domains come from three sources, merged into a single allowlist:
1. **Config:** `[network] allowlist` in config.toml
2. **Hook:** `network_domains()` from the detected language hook
3. **Tools:** `runtime_domains` from enabled tools (see Stage 6)

**Default allowlist:**
- `github.com`
- `raw.githubusercontent.com`
- `release-assets.githubusercontent.com`
- `objects.githubusercontent.com`
- `codeload.githubusercontent.com`

### 5.4 Domain validation

Domains must match `[a-zA-Z0-9.*-]+`. Wildcard domains (e.g., `*.example.com`)
resolve the base domain.

---

## Stage 6 — Pluggable tools

### 6.1 Tool registry

Each tool has:
- `script`: install script filename (in a tools script directory)
- `host_cmd`: command to check on host for auto-detection
- `network_domains`: extra domains needed during image build
- `runtime_domains`: domains needed at container runtime
- `priority`: install order (lower = first)

**Built-in tools:**

| Tool | Priority | Host cmd | Runtime domains |
|------|----------|----------|-----------------|
| `elan` | 10 | `elan` | — |
| `claude` | 50 | `claude` | `api.anthropic.com` |
| `codex` | 50 | `codex` | `api.openai.com` |
| `vscode` | 90 | `code` | VS Code marketplace domains |
| `emacs` | 90 | `emacs` | — |
| `neovim` | 90 | `nvim` | — |

### 6.2 Tool resolution

Each tool's config value is `"yes"`, `"no"`, or `"auto"` (default):
- `"yes"`: always install
- `"no"`: never install
- `"auto"`: install if the corresponding host command is found

**Editor tools are special:** The configured editor (from `editor` config key,
default `vscode`) is treated as `"yes"` unless explicitly set to `"no"` in
`[tools]`. Other editors are only installed if force-enabled.

**Configuration:**
```toml
[tools]
claude = "yes"
elan = "auto"
vscode = "no"
```

### 6.3 Installation

Tools are installed into the `base` image during build, in priority order
(language tools before editors). The combined install script includes pinned
versions from a `pins.json` file.

**`bubble tools set TOOL yes|no|auto`** — configure a tool.

### 6.4 Drift detection

A content-aware hash of the enabled tool set (tool names + script contents +
pinned versions) is stored in `~/.bubble/tools-hash`. On each `bubble open`, if
the current hash differs, trigger a background rebuild of the base image.

Similarly:
- VS Code commit hash (`~/.bubble/vscode-commit`) — rebuild if VS Code updates
- Customize script hash (`~/.bubble/customize-hash`) — rebuild if script changes

### 6.5 User customization

Users can place `~/.bubble/customize.sh` to run custom setup in all container
images. The script runs as root as the final build step.

---

## Stage 7 — Remote SSH hosts

### 7.1 `--ssh HOST`

Run the bubble on a remote machine instead of locally.

**Host specification formats:**
- `host`
- `user@host`
- `host:port`
- `user@host:port`

**Validation:** Hostname and user must match `[a-zA-Z0-9_][a-zA-Z0-9._-]*`
(prevents SSH option injection).

### 7.2 Remote deployment

On first use (or version mismatch), deploy bubble to the remote:
1. Bundle the local bubble package + dependencies into a tarball
2. SCP to `/tmp/bubble-remote/` on the remote
3. Verify: `PYTHONPATH=/tmp/bubble-remote python3 -m bubble --version`
4. Write version marker (`/tmp/bubble-remote/.version`)

The remote must have Python >= 3.10.

### 7.3 Remote open flow

1. Deploy bubble if needed
2. Run `bubble open --no-interactive --machine-readable <target>` on remote via SSH
3. Parse JSON result from stdout
4. Inject local SSH keys into the remote container
5. Set up GitHub auth tunnel (see Stage 10)
6. Write local SSH config with chained ProxyCommand:
   ```
   ProxyCommand ssh [options] user@host 'incus exec <name> -- su - user -c "nc localhost 22"'
   ```
7. Register locally with `remote_host` field
8. Open editor locally (connects through the chained proxy)

### 7.4 Remote lifecycle routing

`pause`, `pop`, and `list` auto-route to the remote host when the registry
entry has a `remote_host` field. The command is forwarded via
`bubble <command>` on the remote.

### 7.5 Priority chain

```
--local > --ssh HOST > --cloud > [cloud] default > [remote] default_host
```

`--local` forces local execution, overriding all remote/cloud config.

---

## Stage 8 — Cloud provisioning (Hetzner)

### 8.1 Requirements

- `HETZNER_TOKEN` environment variable (never stored)
- `hcloud` Python package (optional dependency)

### 8.2 Commands

**`bubble cloud provision [--type TYPE] [--location LOC]`**

1. Generate ed25519 SSH keypair at `~/.bubble/cloud_key` (mode 0600)
2. Register key with Hetzner API
3. Create server with cloud-init that:
   - Installs Incus via Zabbly
   - Runs `incus admin init --auto`
   - Installs idle auto-shutdown timer
4. Wait for server to be SSH-reachable and cloud-init complete
5. Save state to `~/.bubble/cloud.json`

**State file:** `~/.bubble/cloud.json`
```json
{
  "provider": "hetzner",
  "server_id": 12345,
  "server_name": "bubble-cloud",
  "ipv4": "1.2.3.4",
  "server_type": "cx43",
  "location": "fsn1",
  "ssh_key_id": 67890
}
```

**`bubble cloud destroy [-f]`** — destroy server, clean up SSH key from Hetzner.

**`bubble cloud status`** — show server status.

**`bubble cloud stop`** — power off (stops billing).

**`bubble cloud start`** — power on, wait for SSH.

**`bubble cloud ssh [ARGS]`** — SSH directly to cloud server.

### 8.3 Idle auto-shutdown

A systemd timer on the cloud server checks every 5 minutes. After
`idle_timeout` seconds (default 900) with no SSH connections AND low CPU, the
server powers off. Containers survive shutdown.

Running containers do NOT prevent shutdown — only active SSH connections and
high CPU load do.

### 8.4 `--cloud` flag

`bubble open --cloud <target>` auto-provisions or auto-starts the cloud server,
then uses the existing remote flow (Stage 7).

### 8.5 Cloud SSH options

When connecting to the cloud server, always use:
```
-i ~/.bubble/cloud_key -o IdentitiesOnly=yes
-o UserKnownHostsFile=~/.bubble/known_hosts -o StrictHostKeyChecking=accept-new
```

---

## Stage 9 — Relay (bubble-in-bubble)

### 9.1 Architecture

```
Container                              Host
────────                              ────
/usr/local/bin/bubble (stub)          bubble relay daemon
  → /bubble/relay.sock                ← ~/.bubble/relay.sock
  sends {"target": "...", "token": "..."}
  reads {"status": "...", "message": "..."}
```

### 9.2 Relay daemon

**macOS:** TCP listener on `127.0.0.1` (Unix sockets can't traverse Colima's
virtio-fs). Port saved to `~/.bubble/relay.port`.

**Linux:** Unix socket at `~/.bubble/relay.sock` (mode 0600).

**Managed via:** launchd (`com.bubble.relay-daemon`, KeepAlive) or systemd
(`bubble-relay.service`, Restart=always).

### 9.3 Request validation

1. **Token authentication:** Each container gets a relay token at creation time,
   stored in `~/.bubble/relay-tokens.json` (mode 0600). The token maps to a
   container name. Requests without a valid token are rejected.

2. **Target validation:**
   - No local paths (starting with `.`, `/`, `~`)
   - No CLI options (starting with `-`)
   - No `--path` flag
   - No shell metacharacters (`;|&$`\\(){}[]!#`)
   - No `..` path traversal
   - Must parse as a valid target
   - Owner and repo names must match `[a-zA-Z0-9._-]+`
   - Repo must exist in `~/.bubble/git/` (no new clones from containers)

3. **Rate limiting per container:** 3/minute, 10/10 minutes, 20/hour.
   Global: 30/hour.

4. **All requests logged** to `~/.bubble/relay.log`.

### 9.4 Request format

```json
{"target": "owner/repo/pull/123", "token": "hex_token"}
```

### 9.5 Response format

```json
{"status": "ok|error|unknown_repo|rate_limited", "message": "..."}
```

### 9.6 Relay action

On success, the daemon runs:
```
bubble open --local --no-clone --no-interactive <target>
```

`--no-clone` prevents creating new bare repos (TOCTOU protection — the
validation already confirmed the repo exists).

---

## Stage 10 — Credential forwarding and auth proxies

### 10.1 Claude Code config mounting

When `--claude-config` is enabled (default), mount specific items from
`~/.claude/` into `/home/user/.claude/` read-only:
- `CLAUDE.md`, `settings.json`, `skills/`, `keybindings.json`, `commands/`

Credential files (`.credentials.json`) are mounted by default. Disable with
`--no-claude-credentials` or set `claude.credentials = false` in config.

**Symlink safety:** Reject symlinks that escape `~/.claude/` to prevent
exposing arbitrary host files.

### 10.2 Codex config mounting

Similar to Claude. Config: `config.toml` (read-only). Credentials: `auth.json`
(mounted by default; disable via `--no-codex-credentials` or config).

### 10.3 Editor config mounting

**Emacs:**
- Config: `~/.config/emacs/` or `~/.emacs.d/` (read-only, first found)
  - Writable subdirectory overlays for: `elpa`, `eln-cache`, `straight`, `elpaca`, `auto-save-list`, `transient`, `.cache`
- Data: `~/.local/share/emacs/`, `~/.cache/emacs/` (read-write)

**Neovim:**
- Config: `~/.config/nvim/` (read-only)
- Data: `~/.local/share/nvim/`, `~/.local/state/nvim/`, `~/.cache/nvim/` (read-write)

### 10.4 GitHub auth proxy

An HTTP reverse proxy on the host provides GitHub authentication without
exposing the host's token. Git and REST API requests are repo-scoped;
GraphQL requests are operation-validated (queries vs mutations) but not
repo-scoped — see access levels below.

**Port:** 7654 (default, configurable).

**Flow:**
1. Container git is configured with `url.insteadOf` to route HTTPS through the proxy
2. Container sends request with `X-Bubble-Token` header
3. Proxy validates token against `~/.bubble/auth-tokens.json` (mode 0600)
4. For git/REST: proxy checks path matches the allowed `owner/repo`. For GraphQL: proxy validates operation type (queries allowed at level 3, mutations require level 4) but does not scope to a specific repo
5. Proxy adds `Authorization: token <real-token>` header
6. Proxy forwards to `https://github.com`
7. Response returned to container

**Token format:** `~/.bubble/auth-tokens.json`
```json
{
  "hex_token": {"container": "name", "owner": "owner", "repo": "repo"}
}
```

**Allowed paths (git smart HTTP only):**
- `GET /git/{owner}/{repo}[.git]/info/refs?service=git-upload-pack`
- `GET /git/{owner}/{repo}[.git]/info/refs?service=git-receive-pack`
- `POST /git/{owner}/{repo}[.git]/git-upload-pack`
- `POST /git/{owner}/{repo}[.git]/git-receive-pack`

**Access levels (per-container):**
| Level | Description | Scope |
|-------|-------------|-------|
| 1 | Git smart HTTP only (push/pull) | Repo-scoped |
| 2 | Git + REST API read-only | Repo-scoped (REST paths validated against `/repos/{owner}/{repo}/...`) |
| 3 (default) | Git + gh read-only (REST read + GraphQL queries) | Git and REST are repo-scoped; **GraphQL is account-wide** — queries can read any data the host token can access |
| 4 | Git + gh read-write (REST + GraphQL + mutations) | Git and REST are repo-scoped; **GraphQL queries and mutations are account-wide** |

> **Note:** GitHub's GraphQL API does not support path-based scoping.
> At the default level 3, a container can query any repository, org membership,
> or user data readable by the host token. To restrict containers to git-only
> access, use `bubble security set github-api off`.

**Security:**
- Path canonicalization: reject encoded separators, dot-segments, duplicate slashes
- No redirect following (returns redirects as-is to prevent token leakage)
- Pinned to `github.com:443` with TLS verification
- Ignores `HTTPS_PROXY`/`ALL_PROXY` environment variables
- Rate limited: 60/minute, 600/hour per container
- Maximum request body: 256 MB

**Local containers:** Exposed via Incus proxy device connecting host TCP port to
container `127.0.0.1:7654`.

**Remote containers:** SSH reverse tunnel (`ssh -R 7654:127.0.0.1:7654 remote`)
forwards the local proxy port to the remote host. Tunnel PID files in
`~/.bubble/tunnels/`. One tunnel per remote host (shared across containers).

**Daemon management:** launchd (`com.bubble.auth-proxy`) or systemd
(`bubble-auth-proxy.service`).

### 10.5 User mounts

Users can mount host directories into containers:

**CLI:** `--mount /host/path:/container/path[:ro|rw]` (repeatable, default: ro)

**Config:**
```toml
[[mounts]]
source = "/path/to/mount"
target = "/container/path"
mode = "ro"
exclude = ["subdir1", "subdir2"]
```

**Validation:**
- Source must exist on host
- No duplicate container targets
- No overlap with auto-mounts (Claude, editor, etc.)
- Rejected entirely when `security.user-mounts = "off"`

---

## Configuration

### Config file

**Location:** `~/.bubble/config.toml` (TOML format)

**Default config:**

```toml
editor = "vscode"

[runtime]
backend = "incus"
colima_cpu = <host_cpu_count>
colima_memory = 16
colima_disk = 60
colima_vm_type = "vz"

[images]
refresh = "weekly"

[network]
allowlist = [
  "github.com",
  "raw.githubusercontent.com",
  "release-assets.githubusercontent.com",
  "objects.githubusercontent.com",
  "codeload.githubusercontent.com",
]

[relay]
enabled = false
port = 7653

[remote]
default_host = ""

[cloud]
provider = "hetzner"
server_type = ""
location = "fsn1"
server_name = "bubble-cloud"
default = false

[claude]
credentials = true

[codex]
credentials = true

[security]
# All settings default to "auto"

[tools]
# Tool overrides: tool_name = "yes"|"no"|"auto"
```

Config is deep-merged with defaults — user settings override defaults, missing
keys inherit defaults.

Configuration is managed through dedicated subcommands rather than a generic
get/set interface:

- `bubble tools set TOOL yes|no|auto` — configure tool installation
- `bubble claude credentials on|off` — toggle Claude credential mounting
- `bubble codex credentials on|off` — toggle Codex credential mounting
- `bubble security set NAME on|off|auto` — configure security settings
- `bubble config set KEY VALUE` — set security settings (alias)
- `bubble config lockdown` — disable all off-by-default security features
- `bubble config accept-risks` — enable all on-by-default risk features
- `bubble config symlink-claude-projects` — symlink Claude projects directory

---

## Security

### Security settings

Every isolation-weakening feature is individually configurable with three
values: `auto`, `on`, `off`.

| Setting | Auto default | Description |
|---------|-------------|-------------|
| `network-github` | on | GitHub domains in network allowlist |
| `shared-cache` | on | Writable shared mounts (mathlib cache) |
| `user-mounts` | on | `--mount` flag support |
| `git-manifest-trust` | on | Auto-clone Lake manifest dependencies |
| `claude-credentials` | on | Mount Claude credentials into containers |
| `codex-credentials` | on | Mount Codex credentials into containers |
| `github-auth` | on | Repo-scoped GitHub auth via proxy (git push/pull) |
| `github-api` | on | GitHub API access via auth proxy: REST is repo-scoped; **GraphQL queries are read-only but account-wide** (can read any repo the host token can access). Set to `off` for git-only, or `read-write` for mutations |
| `github-token-inject` | off | Direct GitHub token injection (bypasses proxy) |
| `relay` | on | Bubble-in-bubble relay |
| `host-key-trust` | on | Disable SSH StrictHostKeyChecking |

**When set to `auto`:** A reminder is printed on each invocation directing the
user to `bubble security`. Suppressed by `BUBBLE_QUIET_SECURITY=1`.

**Commands:**
- `bubble security` — show full security posture
- `bubble security set NAME on|off|auto` — set individual setting
- `bubble security permissive` — set all to `on`
- `bubble security lockdown` — set all to `off`
- `bubble security default` — reset all to `auto`

When `network-github` is `off`, all GitHub-related domains are stripped from the
network allowlist.

---

## Native workspaces

`bubble open --native <target>` creates a non-containerized workspace:
- Clones to `~/.bubble/native/<name>/`
- Tracked in registry with `native: true`
- No network isolation, no container
- `pop` deletes the directory (safety check: only under `~/.bubble/native/`)
- `pause` is not supported

**Incompatible with:** `--ssh`, `--cloud`, `--no-network`, `--machine-readable`

---

## Additional commands

These commands support infrastructure management and are not core to the
container lifecycle, but a complete implementation should include them:

- `bubble doctor` — check system health (Incus, Colima, SSH config, etc.)
- `bubble network apply NAME` — apply network allowlist to a container
- `bubble network remove NAME` — remove network restrictions
- `bubble automation install` — install periodic automation jobs
- `bubble automation remove` — remove automation jobs
- `bubble automation status` — show automation status
- `bubble remote set-default HOST` — set default remote SSH host
- `bubble remote clear-default` — clear default remote host
- `bubble remote status` — show remote configuration and list remote bubbles
- `bubble gh status` — show GitHub authentication status

---

## Data locations summary

| Path | Contents |
|------|----------|
| `~/.bubble/config.toml` | User settings |
| `~/.bubble/git/` | Bare repo mirrors |
| `~/.bubble/git/<repo>.git.lock` | Per-repo file locks |
| `~/.bubble/repos.json` | Learned repo short name mappings |
| `~/.bubble/registry.json` | Bubble state tracking |
| `~/.bubble/native/` | Native workspace clones |
| `~/.bubble/relay.sock` | Relay daemon Unix socket (Linux) |
| `~/.bubble/relay.port` | Relay daemon TCP port (macOS) |
| `~/.bubble/relay-tokens.json` | Relay auth tokens (mode 0600) |
| `~/.bubble/relay.log` | Relay request log |
| `~/.bubble/auth-tokens.json` | Auth proxy tokens (mode 0600) |
| `~/.bubble/auth-proxy.port` | Auth proxy TCP port |
| `~/.bubble/auth-proxy.log` | Auth proxy request log |
| `~/.bubble/tunnels/` | SSH tunnel PID files |
| `~/.bubble/mathlib-cache/` | Shared mathlib cache |
| `~/.bubble/vscode-commit` | VS Code commit hash in current base image |
| `~/.bubble/tools-hash` | Hash of installed tools + scripts |
| `~/.bubble/customize.sh` | User customization script |
| `~/.bubble/customize-hash` | Hash of customize.sh |
| `~/.bubble/cloud.json` | Hetzner Cloud server state |
| `~/.bubble/cloud_key` | SSH private key for cloud (ed25519, mode 0600) |
| `~/.bubble/cloud_key.pub` | SSH public key for cloud |
| `~/.bubble/known_hosts` | SSH known_hosts for cloud |
| `~/.bubble/claude-projects/` | Claude session state for containers |
| `~/.ssh/config.d/bubble` | Auto-managed SSH config entries |

---

## External commands

The implementation shells out to these external tools:

| Command | Used for |
|---------|----------|
| `incus` | Container lifecycle, exec, file push, image management |
| `git` | Bare repo management, clone, checkout, status |
| `ssh` | Remote host commands, tunnels |
| `scp` | Remote deployment |
| `code` | VS Code CLI (version detection, editor launch) |
| `gh` | GitHub API queries (PR metadata, issue/PR detection) |
| `ssh-keygen` | Cloud SSH key generation |
| `launchctl` | macOS automation (launchd) |
| `systemctl` | Linux automation (systemd) |
| `colima` | macOS VM management |
| `iptables` / `ip6tables` | Network allowlisting (inside containers) |

---

## What this spec does NOT prescribe

- Language, runtime, or framework
- Internal module/package structure
- Concurrency model or async strategy
- Data structures or algorithms
- How to test it
- Error message wording (only error conditions and categories)
