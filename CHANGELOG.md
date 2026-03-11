# Changelog

## 0.6.3 — 2026-03-12
- Add PythonHook: detect `pyproject.toml`, select `python` image with `uv` and `ruff` pre-baked (#93)
  - Auto-runs `uv sync` on first login for dependency installation
  - Runtime network allowlist includes `pypi.org` and `files.pythonhosted.org`
- Don't prompt interactively during `bubble open` (#88)
  - `maybe_symlink_claude_projects()` now prints an informational message instead of blocking on a `[y/N]` prompt
  - New `bubble config symlink-claude-projects` command to perform the symlink manually
  - Suppress the hint with `claude_projects_symlink = "no"` in `~/.bubble/config.toml`
- Improve security posture warnings UX (#87)
  - Replace per-setting warning spam with a single summary line: "bubble is using default security assumptions. Review with: bubble security"
  - New `bubble security` command with grouped settings display (Network, Filesystem, Authentication, Cloud, SSH)
  - Quick presets: `bubble security permissive`, `bubble security default`, `bubble security lockdown`
  - `bubble security set <name> <value>` for individual settings
  - Fix duplicate security warnings (were printed twice per `bubble open` invocation)
  - Once all settings are explicitly configured, the summary message disappears entirely

## 0.6.2 — 2026-03-12
- Remove `.current-account` from Claude credential mounts (#91)
  - Only `.credentials.json` is needed for Claude auth inside bubbles
  - `.current-account` is local account-swapping machinery, not relevant for containers
- SSH tunnel auth proxy for remote/cloud bubbles (#81)
  - Remote and cloud bubbles now use the same repo-scoped auth proxy as local bubbles
  - SSH reverse tunnel (`-R`) forwards the local auth proxy to the remote host
  - Per-remote-host tunnel sharing: multiple containers reuse a single tunnel
  - Tunnel cleaned up automatically when the last bubble on a remote host is popped
  - Removed legacy direct token injection for remote containers
  - Host GitHub token never leaves the local machine, even for remote/cloud bubbles

## 0.6.1 — 2026-03-12
- Repo-scoped GitHub auth via HTTP reverse proxy (#71)
- `--gh-token` now keeps the host GitHub token on the host — never enters the container
- Auth proxy validates requests against a strict 4-pattern git smart HTTP allowlist
- Per-container tokens scoped to a single repository (owner/repo)
- Path canonicalization rejects encoded separators, dot-segments, duplicate slashes
- No redirect following, pinned outbound to github.com:443 with TLS verification
- Daemon managed via launchd (macOS) / systemd (Linux), auto-started on first `--gh-token` use
- `bubble gh proxy start/stop/daemon` commands for manual management
- `bubble gh status` now shows auth proxy state
- Fix: relay and auth proxy tokens cleaned up on `bubble pop` (fixes stale token leak)
- Fix race between parent and derived image builds (#80)
  - Derived-image builds now hold a shared lock on the parent image
  - Prevents parent rebuilds from running concurrently with derived builds
  - Multiple derived builds can still proceed in parallel (shared locks coexist)

## 0.6.0 — 2026-03-12
- Migrate editors and elan to the pluggable tools system (#47)
  - Editors (vscode, emacs, neovim) are now tools installed on the base image
  - Elan + leantar is now a tool with `"auto"` detection (installs if elan is on host)
  - Eliminates 6 editor-specific image variants (base-vscode, base-emacs, base-neovim, lean-vscode, lean-emacs, lean-neovim)
  - IMAGES dict reduced to just `base` and `lean`
  - Tools install in priority order: language tools (elan) before editors (vscode) so cross-tool setup works
  - Editor selection via `editor` config key (default: vscode), individual tools overridable via `[tools]` section
  - VS Code runtime domains (marketplace, gallery) now flow through the tool system instead of being hardcoded
- Fix Claude integration for remote/cloud bubbles (#24):
  - Issue prompts are now auto-generated locally and forwarded to remote hosts
  - `BUBBLE_CLAUDE_PROMPT` env var now works with `--ssh` and `--cloud`
  - Claude task injection runs on the remote container host as expected
- Fix `_purge_derived_images()` to recursively walk the full dependency tree (#69)
  - Also purges dynamic toolchain images (`lean-v4.x.y`, etc.)
- Fix race condition between background and foreground image builds (#67)
  - Add `fcntl`-based file locking to `build_image()` and `build_lean_toolchain_image()`
  - Concurrent builds of the same image now serialize; the second skips if the first completed
  - Builder container cleanup is now in a `finally` block to prevent orphaned containers
  - Replace fragile touch-based lock files with proper `fcntl.flock()` locks
- Lean cache/build workflow now triggers for emacs, neovim, and shell editors (#26):
  - emacs/neovim: build starts in background when editor opens via SSH
  - shell: build starts in background on first login via `.profile` hook
  - Build output saved to `~/build.log` inside the container
- Templated Claude prompts for issues and PRs (#33)
- PR bubbles now auto-inject a Claude prompt that checks CI status and summarizes PR comments
- User-customizable prompt templates via `~/.bubble/templates/issue.txt` and `~/.bubble/templates/pr.txt`
- Issue template placeholders: `{owner}`, `{repo}`, `{issue_num}`, `{title}`, `{body}`, `{comments}`, `{comments_section}`, `{branch}`
- PR template placeholders: `{owner}`, `{repo}`, `{pr_num}`, `{title}`, `{body}`, `{branch}`
- Falls back to built-in defaults when no custom template exists

## 0.5.15 — 2026-03-11
- Split cli.py (~4200 lines) into focused modules (#45):
  - `setup.py`: dependency installation, runtime setup
  - `image_management.py`: image detection, building, background rebuilds
  - `provisioning.py`: container provisioning and mount setup
  - `clone.py`: repo cloning and checkout inside containers
  - `finalization.py`: post-clone setup, SSH, registration, editor launch
  - `container_helpers.py`: shared container helpers (find, SSH, git config, network)
  - `native.py`: non-containerized workspace mode
  - `commands/list_cmd.py`: list command and formatting helpers
  - `commands/lifecycle.py`: pause, pop, cleanup commands
  - `commands/images.py`: image list, build, delete commands
  - `commands/infrastructure.py`: git, network, automation command groups
  - `commands/relay_cmd.py`: relay enable, disable, status, daemon
  - `commands/remote_cmd.py`: remote host management commands
  - `commands/cloud_cmd.py`: Hetzner Cloud management commands
  - `commands/settings.py`: skill, claude, tools, gh, config command groups
  - `commands/doctor.py`: diagnostic doctor command
- cli.py reduced to ~815 lines: Click infrastructure and open command orchestration only
- All extracted modules under 420 lines each
- Pure refactor: no behavior changes

## 0.5.14 — 2026-03-11
- Inject GitHub auth token into bubbles so `gh` CLI works inside containers (#38)
- New `--gh-token` flag on `bubble open` (opt-in, default disabled)
- Persistent config via `bubble gh token on/off` and `bubble gh status`
- Token is obtained from host's `gh auth token` and injected via `gh auth login --with-token`
- Nag tip shown when host has `gh` auth but `--gh-token` is not enabled

## 0.5.13 — 2026-03-11
- Persistent config for Claude credentials: `bubble claude credentials on/off` and `[claude] credentials` in config.toml (#37)
- `bubble claude status` shows current Claude settings
- Nag message suppressed when credentials are explicitly configured (on or off)
- Fix shallow copy bug in `DEFAULT_CONFIG` that could leak state between config loads

## 0.5.12 — 2026-03-11
- User-defined image customization script: place `~/.bubble/customize.sh` to run custom setup in all container images (#34)
- Script runs as root as the final build step (base, lean, lean-toolchain images)
- Automatic background rebuild when the script is added, changed, or removed (hash-based detection)
- Sync bubble Claude projects state via git-tracked symlink: on `bubble open`, if `~/.claude/projects/` is git-tracked, offer to replace `~/.bubble/claude-projects/` with a symlink so session state is synced across machines (#4)
- Mount editor configs into containers for emacs/neovim (#44): config directories mounted read-only, data/state/cache directories mounted read-write so plugin managers work
- Emacs: mounts `~/.config/emacs/` (preferred) or `~/.emacs.d/`, plus `~/.local/share/emacs/` and `~/.cache/emacs/` read-write
- Neovim: mounts `~/.config/nvim/`, plus `~/.local/share/nvim/`, `~/.local/state/nvim/`, and `~/.cache/nvim/` read-write
- User mounts (`--mount`) take precedence and suppress overlapping editor config mounts
- Forward `-b`/`--new-branch` and `--base` flags to remote host in `--ssh` and `--cloud` modes (#25)
- Pin tool installer versions and verify checksums for reproducible, hardened image builds (#43)
- Node.js installed from official tarball with SHA256 verification (replaces `curl | bash` from NodeSource)
- npm packages pinned to exact versions (`@anthropic-ai/claude-code@X.Y.Z`, `@openai/codex@X.Y.Z`)
- GitHub CLI GPG key verified by SHA256 checksum before trusting
- `bubble tools update` command fetches latest upstream versions and bumps pins
- Configurable security posture: every isolation-weakening feature now has `auto`/`on`/`off` settings in `[security]` config section (#40)
- `bubble config security` shows current security posture with effective values
- `bubble config set security.<name> <value>` configures individual settings
- `bubble config accept-risks` silences on-by-default warnings; `bubble config lockdown` disables off-by-default features
- Security warnings printed to stderr for all `auto` settings during `bubble open`
- `BUBBLE_QUIET_SECURITY=1` env var suppresses warnings for CI/automation
- Settings: `shared_cache`, `user_mounts`, `network_github`, `relay`, `claude_credentials`, `host_key_trust`, `cloud_root`, `git_manifest_trust`
- When `shared_cache=off`, shared mounts are read-only; when `network_github=off`, GitHub domains stripped from allowlist
- When `user_mounts=off` or `claude_credentials=off`, corresponding CLI flags are rejected
- Relay enable/disable migrated to `[security] relay` with backwards compatibility for `[relay] enabled`
- Replaces ad-hoc "Tip: use --claude-credentials" message with unified security warning system
- Fix: `DEFAULT_CONFIG` shallow copy mutation bug (nested dicts are now deep-copied)

## 0.5.11 — 2026-03-11
- Tools now declare runtime network domains (e.g. `api.anthropic.com` for Claude Code) that persist in the container firewall, fixing connectivity for tools at runtime (#49)

## 0.5.10 — 2026-03-11
- Pluggable tool installation system: tools like Claude Code, Codex, and GitHub CLI can be installed in container images
- Per-tool install scripts in `bubble/images/scripts/tools/` (self-contained, run as root during image build)
- Config-driven activation via `[tools]` section in `~/.bubble/config.toml` with `"yes"`, `"no"`, or `"auto"` (default) per tool
- `"auto"` mode detects whether the tool is installed on the host and mirrors it in containers
- `bubble tools list` shows available tools and settings, `bubble tools set` changes settings, `bubble tools status` shows resolved state
- Background image rebuild when resolved tool set changes (same pattern as VS Code hash drift)
- Tools installed in the `base` image and inherited by all derived images

## 0.5.9 — 2026-03-11
- GitHub issue targets: `bubble https://github.com/owner/repo/issues/123` creates a branch `issue-123` and opens a bubble
- Bare numbers auto-detect PR vs issue via GitHub API
- New branch mode: `bubble -b my-feature owner/repo` creates a fresh branch in a new bubble
- Claude Code integration: issue bubbles auto-inject a Claude prompt from the issue body/comments, runs on VS Code folder open
- `BUBBLE_CLAUDE_PROMPT` env var for custom Claude prompts on any bubble
- Pull latest on reattach: clean containers auto-pull before reopening
- Emacs and Neovim editor support: `--emacs`, `--neovim` flags or `editor = "emacs"` / `editor = "neovim"` in config
- Uniform editor image naming: `base-vscode`, `base-emacs`, `base-neovim`, `lean-vscode`, `lean-emacs`, `lean-neovim`
- `lean` is now the core image (elan + leantar); VS Code Server and extensions live in `-vscode` variants
- VS Code Server moved from `base` to `vscode.sh` script (shared by `base-vscode` and `lean-vscode`)
- Lean VS Code extensions installed conditionally (only when elan is present in the image)
- Mount `~/.claude` config read-only into containers by default (CLAUDE.md, settings.json, skills/, keybindings.json)
- Credentials (`.credentials.json`) opt-in via `--claude-credentials` for security
- Nag message reminds you about `--claude-credentials` when credentials exist on host
- Writable per-bubble `projects/` directory (`~/.bubble/claude-projects/<name>/`) for persistent session memory
- Session history and transient state are excluded by design
- Symlink validation: rejects mounts that escape `~/.claude` directory
- Path overlap detection: user mounts take precedence over auto mounts (ancestry-aware)
- `--no-claude-config` properly forwarded to remote/cloud bubbles
- Opt out with `--no-claude-config`

## 0.5.8 — 2026-03-10
- User-configurable host directory mounts via `--mount` CLI flag and `[[mounts]]` config section
- Supports read-only (default) and read-write modes
- Subdirectory exclusion via `exclude` list (overmounts excluded paths with tmpfs)
- Exclude entry validation prevents path traversal (`..`, absolute paths)
- `bubble skill install/uninstall/status` commands for managing the Claude Code skill
- Skill file bundled with the package at `bubble/data/skill.md`
- Auto-install the skill on first `bubble open` when Claude Code is detected
- Native mode: `bubble --native <target>` creates non-containerized workspaces
- Clones into `~/.bubble/native/<name>/` with shared git objects
- Prints prominent warning about lack of isolation
- `bubble list` shows native workspaces with location "native"
- `bubble pop` supports native workspaces with dirty-check confirmation
- `bubble pause` rejects native workspaces (no container state to freeze)
- Cleanness checking for native workspaces (dirty worktree, unpushed commits)

## 0.5.7 — 2026-02-17
- Remote-aware `bubble list`: shows cloud and SSH-remote bubbles from registry
- `--cloud` flag queries cloud server for live container status
- `--ssh HOST` flag queries SSH host for live container status
- `--local` flag shows only local bubbles
- Fix cloud SSH options lost after registry round-trip (affected `pause`/`destroy`)
- Fix probe servers sending scary root-password emails from Hetzner
- SSH config generation supports custom ssh_options from RemoteHost

## 0.5.6 — 2026-02-17
- Auto-install QEMU on Intel Macs (needed by Colima; Apple Silicon uses Virtualization.Framework)
- Skip `--vm-type` flag on Colima versions that don't support it (e.g. Colima 0.10+)

## 0.5.4 — 2026-02-17
- Auto-install missing Homebrew dependencies (colima, incus) on remote hosts without TTY

## 0.5.3 — 2026-02-17
- Auto-discover Python >= 3.10 on remote hosts (probes multiple paths when `python3` is too old)
- Fix Homebrew not found on macOS SSH sessions with minimal PATH

## 0.5.2 — 2026-02-17
- `--command` option: run a command inside a bubble via SSH (`bubble --command "lake build" FLT`)
- NixOS container networking: static IPv4 assignment and DNS proxy when nftables blocks bridge DHCP/DNS
- Auto-detect and inject host git identity into containers
- Auto-initialize Incus (`incus admin init --auto`) when no storage pool exists
- Stream remote bubble creation progress to local terminal
- Improved `IncusError` with stderr details in error messages
- Fix `remote_open` potential deadlock by draining stderr concurrently
- Dynamic version from `bubble.__version__` in pyproject.toml

## 0.5.1 — 2026-02-16
- Auto-build for lean4 repo: `cmake --preset release && make -C build/release -j$(nproc)` runs in VS Code terminal on connect
- Open `lean.code-workspace` automatically when working on the lean4 repo
- Add cmake to base image
- Remove Emacs and Neovim editor support (too heavy for container images)

## 0.5.0 — 2026-02-16
- Pre-populate Lake dependencies via git alternates: parse `lake-manifest.json`, mirror dependency repos on host, and clone them into `.lake/packages/` with shared objects — eliminates slow GitHub clones during `lake build`
- File locking on bare repo operations to prevent corruption from concurrent `bubble open` runs
- Fetch tags in bare repo mirrors (fixes deps pinned to tag-only commits)
- Input validation for manifest-sourced package names and revisions

## 0.4.1 — 2026-02-16
- Hetzner Cloud support: `bubble open --cloud` auto-provisions remote servers with idle auto-shutdown
- Built-in default repo mappings for Lean ecosystem (mathlib4, lean4, etc.)
- Auto-run `lake build` for all Lean repos, not just mathlib-downstream
- `bubble images delete --all` command
- NixOS Incus auto-installation

## 0.4.0 — 2026-02-16
- Editor selection: `--emacs`, `--neovim`, `--shell`, or `bubble editor <choice>`
- Share mathlib cache across containers via writable mount
- Fix relay daemon port handling and response through Incus proxy

## 0.3.0 — 2026-02-16
- Remote SSH host support: `--ssh HOST` runs bubbles on remote machines
- Chained SSH ProxyCommand for seamless editor integration with remote hosts

## 0.2.2 — 2026-02-16
- Reservoir network domains in allowlist
- Improved image selection output

## 0.2.1 — 2026-02-16
- Published to PyPI as `dev-bubble` with GitHub Actions trusted publishing
- Lazy Lean toolchain images: per-version images built on demand
- Pre-baked VS Code Server in base image with auto-rebuild on update
- Container cleanness checking for safe cleanup (`bubble cleanup`)
- Bubble-in-bubble relay for nested container creation
- Auto-download mathlib cache, `bubble doctor` command
- Network allowlisting via iptables, DNS restriction, no outbound SSH

## 0.1.0 — 2026-02-13
- URL-first interface: `bubble <github-url>` creates isolated dev containers
- Language-agnostic design with pluggable hooks (Lean 4 first)
- Git object sharing via bare mirrors and `--reference` clones
- Local paths and bare PR numbers as targets
- Automation: launchd/systemd jobs for git updates and image refresh
- Interactive dependency installation (Incus, Colima on macOS)
