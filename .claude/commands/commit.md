---
description: Create conventional commit(s) for reviewed changes
allowed-tools: Bash(git status:*), Bash(git diff:*), Bash(git add:*), Bash(git commit:*), Bash(git log:*)
---

Commit the current changes.

1. Run `git status` and `git diff`. If any of these are staged or about to be added, stop and warn me: `.env*` files, certificates or keys (`*.pfx`, `*.p12`, `*.key`, `*.pem`), `dist/`, `build/`, `.venv/`, `*.db` / local index files, logs, or `.claude/.session/`.
2. If the changes contain more than one logical change, propose how to split them and wait for confirmation.
3. Use Conventional Commits (`feat:`, `fix:`, `refactor:`, `test:`, `chore:`, `docs:`, `build:`) with an imperative subject under 72 characters, and a short body explaining why. Add a `Refs:` line listing the requirement IDs from `docs/requirements.md` the change implements.
4. Show me the message(s), then commit. Never push.
