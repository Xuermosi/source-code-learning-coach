# Source Code Learning Coach

A reusable Codex Skill for learning unfamiliar repositories from real source code while preserving a resumable, auditable learning trail.

It is designed for multi-day source study: it proposes a roadmap before creating notes, teaches one small call-chain segment at a time, tracks weighted checkpoint progress, and records source guides, Q&A, summaries, and optional diagram artifacts in a sibling learning workspace.

## What it does

- Scans a repository read-only and proposes dependency-ordered Days and checkpoints.
- Requires roadmap confirmation before creating the persistent learning workspace.
- Uses authoritative JSON state with Markdown projections for resumable progress.
- Keeps source-specific notes and artifacts attached to the source that owns them.
- Guards learning-workspace paths, symlinks, transactions, concurrent edits, and artifact ownership.
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

## Verify

```bash
python3 -m unittest discover -s tests -p 'test_manage_learning_workspace.py' -v
python3 scripts/manage_learning_workspace.py --help
```

The Skill uses only local workspaces by default. Review its publication rules before exporting learning notes to any remote service.
