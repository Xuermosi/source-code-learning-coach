# Source Code Learning Coach

[简体中文](README.zh-CN.md) | English

A reusable Codex Skill for learning unfamiliar repositories from real source code while preserving a resumable, auditable learning trail.

It is designed for multi-day source study: it proposes a roadmap before creating notes, teaches one small call-chain segment at a time, tracks weighted checkpoint progress, and records source guides, Q&A, summaries, and optional diagram artifacts in a sibling learning workspace.

## What it does

- Scans a repository read-only and proposes dependency-ordered Days and checkpoints.
- Requires roadmap confirmation before creating the persistent learning workspace.
- Uses authoritative JSON state with Markdown projections for resumable progress.
- Keeps source-specific notes and artifacts attached to the source that owns them.
- Guards learning-workspace paths, symlinks, transactions, concurrent edits, and artifact ownership.
- Divides each Day into explicitly named learning chains (`begin-chain`), each with its own `NN-链名/` directory holding a brief and a `产物/` folder for that chain's diagrams.
- Teaches each new call chain as a role-annotated execution call-chain tree first; real source is read closely only on the learner's explicit request.
- After a closed flow, asks one oral question derived from that Day's acceptance criteria.
- Archives an oral Q&A into `DayNN/口述回答归档.md` only after the learner explicitly confirms archiving; each entry keeps the fixed 题干 → 代码执行调用链 → 口述答案 order.
- Asks for explicit consent before generating a diagram after a flow closes.
- Keeps publication local by default; export requires a separate sanitized copy and a scan.

## Install locally

Copy or clone this directory into your Codex user-skill directory:

```text
~/.codex/skills/source-code-learning-coach/
```

Restart Codex if the Skill does not appear immediately, then invoke it with:

```text
$source-code-learning-coach
```

For repository-scoped use, place the Skill in a repository Skill location supported by your Codex setup.

## Use it

Ask Codex to study a repository from real code and retain progress, for example:

```text
Use $source-code-learning-coach to help me study this repository from real source code and preserve my progress.
```

The Skill distinguishes a one-off explanation from persistent study. Persistent study creates a sibling `<project>-<N>天学习路线` workspace only after you approve the roadmap.

## Contents

```text
SKILL.md                         Agent instructions and routing rules
agents/openai.yaml               Codex UI metadata
assets/templates/                Durable note templates
references/workspace-contract.md Workspace and state contract
scripts/manage_learning_workspace.py
                                 Workspace manager CLI
tests/                           Transaction, safety, and behavior tests
```

Inside a learning workspace, each Day directory holds `学习单元.md`, `口述回答归档.md`, Day-level `问题与解答.md` / `当日总结.md` / `当日总览产物/`, source directories (`NN-label/`), and named learning chains (`NN-链名/` with `链说明.md` and `产物/`).

## Verify

```bash
python3 -m unittest discover -s tests -p 'test_manage_learning_workspace.py' -v
python3 scripts/manage_learning_workspace.py --help
```

The Skill uses only local workspaces by default. Review its publication rules before exporting learning notes to any remote service.
