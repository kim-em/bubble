---
name: bubble
description: Create and manage containerized development environments using the `bubble` CLI.
---

# bubble Skill

Use the `bubble` CLI to create and manage containerized development environments.

For the full command reference, see the [README](https://github.com/kim-em/bubble#readme).

## When to Use

- User wants to work on a PR in an isolated environment
- User is reviewing an untrusted PR and wants sandboxing
- User wants to start a project without cluttering their host
- User asks for a "bubble", "container", "sandbox", or "isolated environment"
- **Proactively suggest** bubbling when the user is about to checkout an unfamiliar PR
- Language hooks auto-detect Lean 4 projects and provide pre-configured toolchains

## Key Usage Patterns

```bash
# Open a bubble for a GitHub PR (creates or re-attaches)
bubble https://github.com/leanprover-community/mathlib4/pull/12345
bubble mathlib4/pull/12345          # short form (learned on first use)
bubble 12345                        # bare PR number (uses current repo)

# Open a bubble for a repo's default branch
bubble mathlib4

# Open from a local path
bubble .
bubble --path ./my-project

# SSH shell instead of VSCode
bubble mathlib4 --shell

# Run on a remote host or cloud
bubble mathlib4 --ssh user@host
bubble mathlib4 --cloud

# List, pause, pop
bubble list
bubble pause <name>
bubble pop <name>

# Clean up bubbles with no unsaved work
bubble cleanup

# Diagnose issues
bubble doctor
```

## Tools

Tools like Claude Code and OpenAI Codex can be installed in container images:

```bash
bubble tools list                  # show available tools and settings
bubble tools set claude yes   # always install (also: "no", "auto")
bubble tools status                # show what would actually be installed
```

Default is `"auto"` — installs the tool if found on your host.

## Tips

- Re-running `bubble <target>` for an existing bubble re-attaches (no duplicates)
- Bubbles use shared git objects — creation is fast (~seconds) even for large repos
- Each bubble has SSH access: `ssh bubble-<name>`
- Network is allowlisted by default — containers can only reach approved domains
- Config lives at `~/.bubble/config.toml`; data at `~/.bubble/`
- **Never run `bubble` with `--no-interactive` on the user's behalf** — let the user run it themselves so they see live output and can interact with the result
