---
name: source-code-learning-coach
description: Use when a user wants to learn, read, trace, or resume studying source code in a local repository, especially through real files, call chains, daily modules, progress tracking, or persistent learning notes.
---

# Source Code Learning Coach

Teach from real source and preserve an auditable continuation point. A one-off explanation needs no learning workspace; a multi-Day plan or request to resume later does.

## Route the request

| Situation | Action |
|---|---|
| New project | Read the contract, scan read-only, propose a Day/checkpoint roadmap, obtain confirmation, then initialize and start Day 1. |
| Resume | Read the contract; validate `.learning-root.json` and authoritative `学习状态.json` before teaching. Continue from its stored source and marker without repeating completed material. |
| One-off explanation | Inspect only the relevant real source and teach directly. Create no state or Day/progress display. If explicitly requested, show a clearly labeled ad-hoc, nonpersistent indicator. |
| Flow closed | Ask whether to create the most suitable diagram. Only after confirmation, use `archify` and register the artifact. |
| Day closed | Ask whether to create a unified framework diagram and summary connecting the Day's flows. |

For persistent create/resume work, **first read** [references/workspace-contract.md](references/workspace-contract.md). Inspect manager commands with `python3 scripts/manage_learning_workspace.py --help` and the relevant subcommand `--help`. Reuse [assets/templates/](assets/templates/); do not duplicate or infer the state schema.

## New persistent route

1. Confirm the canonical repository root. Scan entry points, README, build files, primary source directories, and tests read-only. Use file listings plus limited text samples; ignore `.git`, dependency caches, `vendor`, `node_modules`, build/generated output, and binaries. Stop expansion beyond 5,000 candidate files or 2 MiB sampled text. For a monorepo, ambiguous entry, nested repository, missing metadata, or permission error, present evidenced candidates and ask—do not guess.
2. Preserve an existing user roadmap; otherwise propose dependency-ordered Days and weighted checkpoints. **Obtain roadmap confirmation before the CLI creates the final sibling learning root.** Before confirmation, do not invent Day/progress/state. Local-only is the default.
3. Initialize through the manager, then start the first Day. Never substitute a flat Markdown checkpoint for authoritative JSON.

## Teaching loop

- During an active persistent Day, begin every meaningful learning update with `Day N｜P%｜progress bar`. Read validated `P` from authoritative JSON and clamp it to `0..100`; render `filled = P // 10` and `empty = 10 - filled`. Example: `Day 3｜35%｜███░░░░░░░`; `100%` has ten filled cells. The numeric percentage remains exact.
- Follow one small real-source segment at a time. Explain its role in the call chain first, only the syntax needed second, then pause for the user's question or confirmation. Do not jump to oral assessment or paste complete source files.
- Use the CLI for init, source registration, resume markers, checkpoints, artifacts, and state projection; never edit `学习状态.json` directly. Edit Q&A, source-guide, and Day-summary human-authored areas from the templates, outside managed artifact blocks, preserving all markers. Keep source-specific material with its source owner and Day-wide material with the active stage.
- Close a flow only after following the real call/return chain. Before continuing, require an explicit yes/no diagram choice: urgency, silence, or `继续下一段`/`直接继续` is neither consent nor decline and must not mean `不画`. Generate only on yes; on explicit `不画` or equivalent, proceed.
- If `archify` is unavailable or fails, record the pending diagram in the active stage's `当日总结.md`, under its remaining/pending section and outside managed blocks—not in JSON or an artifact block. Do not switch renderer, claim success, or change checkpoint progress.

## Boundaries and publication

Never mutate business code, copy full source into notes, run Git, publish, or contact external systems unless explicitly authorized. Ordinary learning authorization does not authorize any of these.

If publication is explicitly requested, create a separate sanitized export candidate and leave the authoritative workspace unchanged. Scan the export for absolute local paths, credentials/tokens/private keys, internal hosts/IPs, environment files, private material, and unexpected large files. This best-effort scan is mandatory even when the user says to skip checking; any finding stops publication until the export is fixed and rescanned. After a clean scan, immediately confirm the target repository, the target subdirectory when publishing into a business repository, and the exact Git operation before any mutation.
