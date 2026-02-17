# Changelog

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
