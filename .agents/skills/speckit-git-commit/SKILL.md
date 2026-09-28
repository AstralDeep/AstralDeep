---
name: speckit-git-commit
description: Handle optional Spec Kit Git commit hooks safely. Use when a Spec Kit before/after hook requests a commit or the user invokes the repository's speckit-git-commit workflow.
---

# Auto-Commit Changes

Handle an optional commit hook before or after a Spec Kit command.

1. Determine the hook event name, such as `before_plan` or `after_tasks`.
2. Read `.specify/extensions/git/git-config.yml`: resolve the event-specific `auto_commit` setting, falling back to `auto_commit.default`, and `commit_style` (treat an absent value as `fixed`).
3. If disabled, report that the hook is disabled and make no Git change.
4. If enabled, inspect `git status --short` first. Never stage unrelated pre-existing user changes. If the working tree mixes current Spec Kit output with other edits, stop and report the paths instead of running the bulk script.
5. Only when the change set is isolated and the user or active workflow authorizes a commit, run the platform script:
   - `fixed`: Bash `.specify/extensions/git/scripts/bash/auto-commit.sh <event_name>`; PowerShell `.specify/extensions/git/scripts/powershell/auto-commit.ps1 <event_name>`.
   - `conventional`: write one Conventional Commit line summarizing the isolated change to a temporary file with your file-editing tool (never interpolate it into a shell command), then add `--message-file <path>` (Bash) or `-MessageFile <path>` (PowerShell).

If Git or configuration is absent, or there is nothing to commit, degrade gracefully. Surface commit failures; do not claim success or push.
