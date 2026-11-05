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
| Day closed | Ask whether to create a unified framework diagram and summary connecting the Day's flows. Seal the active chain if one is open. |

For persistent create/resume work, **first read** [references/workspace-contract.md](references/workspace-contract.md). Inspect manager commands with `python3 scripts/manage_learning_workspace.py --help` and the relevant subcommand `--help`. Reuse [assets/templates/](assets/templates/); do not duplicate or infer the state schema.

## New persistent route

1. Confirm the canonical repository root. Scan entry points, README, build files, primary source directories, and tests read-only. Use file listings plus limited text samples; ignore `.git`, dependency caches, `vendor`, `node_modules`, build/generated output, and binaries. Stop expansion beyond 5,000 candidate files or 2 MiB sampled text. For a monorepo, ambiguous entry, nested repository, missing metadata, or permission error, present evidenced candidates and ask—do not guess.
2. Preserve an existing user roadmap; otherwise propose dependency-ordered Days and weighted checkpoints. **Obtain roadmap confirmation before the CLI creates the final sibling learning root.** Before confirmation, do not invent Day/progress/state. Local-only is the default.
3. Initialize through the manager, then start the first Day. Never substitute a flat Markdown checkpoint for authoritative JSON. A Day has no learning chain until one is opened explicitly with `begin-chain`.

## Teaching loop

- During an active persistent Day, begin every meaningful learning update with `Day N｜链路 M/K｜链名`. Read `M`/`K` from the authoritative JSON `chains` registry (`M` = current chain order or `—` if none is open, `K` = chains registered for the Day so far) and the chain's `name`; also show the Day's numeric `progress` from JSON (e.g. `Day 3｜链路 2/3｜渲染管线｜35%`). Never render a bar.
- A new real call chain starts with an explicit `begin-chain --day N --name <链名>`; the CLI seals the previous active chain, numbers the new one, creates `DayNN/NN-链名/` with `链说明.md` and `产物/`, and registers it in `学习状态.json`. Then present the chain's **代码执行调用链树** (execution call-chain tree), not pasted source. For each key function, state the caller/callee relationship, its role, its main effect, and its input, return value, or state change. Example:

  ```text
  CLI._run_x(args)                    # CLI 入口：接收参数并选择平台流程
    └─ run_x(...)                     # 平台编排器：准备输入、调用核心、交付结果
         ├─ load_context(...)         # 上下文读取器：事件数据 → Context
         ├─ build_input(...)          # 输入适配器：平台数据 → ReviewInput
         └─ ReviewEngine.run(...)     # 共享审查核心：ReviewInput → ReviewReport
  ```

- After the call-chain tree, wait for the learner to choose what to read closely. Only when the learner explicitly asks (`细读 X`, `看 X 源码`, `展开 X`, or equivalent), read and explain that small real-source segment; when reading closely still follow caller → callee → why the code is split there → return value or state change → only the needed syntax. Never paste complete source files or expand into unrelated source on your own.
- After a flow closes, derive that chain's acceptance goal from the active Day's `DayXX/学习单元.md` `验收与产出` section or its corresponding checkpoint, then ask exactly one oral question grounded in what this chain actually covered: responsibility boundaries, inputs/outputs, platform/core layering, or exception branches. Do not ask generic questions detached from the day's acceptance criteria.
- After the learner answers, give feedback first, then explicitly ask: `是否将这道口述题归档？回复‘归档’或‘不归档’。` Never write to the archive without clear consent. Append a record only after the learner replies `归档`; silence, urgency, or any other reply is not consent.
- Oral archive entries live in `DayNN/口述回答归档.md`, one fixed structure per question: `## 题干` → `## 代码执行调用链` (the chain tree with roles and effects) → `## 口述答案` (the learner's verbatim answer, never rewritten) → optional `## 复盘结论` → optional `## 面试速记`. The `题干 → 代码执行调用链 → 口述答案` order is required; the last two sections are recommended.
- Use the CLI for init, chain lifecycle (`begin-chain`), resume markers (`set-resume`), checkpoints, artifacts (`register-artifact`), and state projection; never edit `学习状态.json` directly. `DayNN/学习单元.md` and `DayNN/口述回答归档.md` are human-authored, marker-free files initialized from templates; they are not managed state. Edit them and the Day-level `问题与解答.md` / `当日总结.md` / `当日总览产物/当日统一总结.md` human-authored areas outside managed artifact blocks, preserving all markers. `链说明.md` records the source entry points for that chain; diagrams produced while learning a chain belong in that chain's `DayNN/NN-链名/产物/` directory.
- Close a flow only after following the real call/return chain. Before continuing, require an explicit yes/no diagram choice: urgency, silence, or `继续下一段`/`直接继续` is neither consent nor decline and must not mean `不画`. Generate only on yes; on explicit `不画` or equivalent, proceed.
- If `archify` is unavailable or fails, record the pending diagram in the active Day's `当日总结.md`, under its remaining/pending section and outside managed blocks—not in JSON or an artifact block. Do not switch renderer, claim success, or change checkpoint progress.

## Boundaries and publication

Never mutate business code, copy full source into notes, run Git, publish, or contact external systems unless explicitly authorized. Ordinary learning authorization does not authorize any of these.

If publication is explicitly requested, create a separate sanitized export candidate and leave the authoritative workspace unchanged. Scan the export for absolute local paths, credentials/tokens/private keys, internal hosts/IPs, environment files, private material, and unexpected large files. This best-effort scan is mandatory even when the user says to skip checking; any finding stops publication until the export is fixed and rescanned. After a clean scan, immediately confirm the target repository, the target subdirectory when publishing into a business repository, and the exact Git operation before any mutation.
