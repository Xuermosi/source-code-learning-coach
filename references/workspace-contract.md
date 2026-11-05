# Source-Code Learning Workspace Contract

This document defines the persistent workspace written by the source-code learning coach. The command surface is intentionally not duplicated here; run `python3 scripts/manage_learning_workspace.py --help` and the relevant subcommand `--help` for current usage.

## 1. Roadmap input

The roadmap is a UTF-8 JSON object. Day numbers are consecutive integers beginning at 1. Checkpoint IDs are non-empty and unique across the route. Each checkpoint weight is a positive integer, and the weights within every Day total exactly 100. Progress is a Day-internal number only; it does not drive any directory lifecycle.

Complete minimal example:

```json
{
  "title": "Repository Learning Route",
  "days": [
    {
      "day": 1,
      "title": "Entry Flow",
      "checkpoints": [
        {
          "id": "day1-entry-flow",
          "title": "Close the entry flow",
          "weight": 100
        }
      ]
    }
  ]
}
```

## 2. Authoritative state

`学习状态.json` is the sole state authority. Its `schema_version` is currently `3`; unsupported versions fail closed. It contains the learning-root ID and canonical paths, route title and Day count, ordered Day/checkpoint state, current Day/chain/progress/resume data, chain/artifact registries, history, and `updated_at`.

`学习状态.md` is only a human-readable projection of that JSON. Never infer authoritative state from the Markdown file. A projection mismatch is repairable by synchronizing from valid JSON.

## 3. Names and layout

The learning root is a sibling of the canonical repository and is named:

```text
<sanitized-project-name>-<total-days>天学习路线
```

All Day directories are created at initialization. Their numeric width is `max(2, digits(total-days))`, producing names such as `Day01` for a route below 100 Days. Each Day directory also receives `学习单元.md`, `口述回答归档.md`, and — when the Day starts — `问题与解答.md`, `当日总结.md`, and `当日总览产物/当日统一总结.md`. A learning chain directory is `NN-<链名>` created by `begin-chain`; it holds `.learning-chain.json`, `链说明.md`, and a `产物/` directory for diagrams produced while learning that chain. There are no generated source directories; the chain's `链说明.md` records which repository files or symbols that chain is about to read.

## 4. Progress

Day progress is the sum of weights for completed checkpoints in that Day. It is recomputed rather than trusted as an independent counter. Progress has no effect on directory layout; it is reported as a plain percentage.

## 5. Active and sealed lifecycle

At most one Day and one chain are active. Starting a Day does not create any chain; the learner or coach opens the first chain explicitly with `begin-chain`, which seals the currently active chain (if any), numbers the new one consecutively within the Day, and registers it in state. `complete-day` seals an open chain, then completes the Day. Sealed chain trees are immutable history.

## 6. Ownership of chain and Day overview files

The coach owns generated marker files, authoritative JSON, generated Markdown projections, chain directories (`.learning-chain.json`, `链说明.md`, `产物/`), and the artifact-managed blocks inside chain briefs and Day overviews. Human-authored content outside a managed block belongs to the user and must not be rewritten by artifact projection. Day-level questions, summaries, and overview material belong to the Day directory; diagrams produced while learning a chain belong in that chain's `产物/` directory.

## 7. Path and symlink constraints

Repository, learning root, Day, and chain paths are canonicalized and checked by path components, not string prefixes. The learning root must remain a sibling of the repository. Resume files must be existing regular files inside the canonical repository. Generated paths must remain inside their declared owner. Symlinked Day/chain parents, escaping symlinks, unsafe filename components, and unsupported secure directory operations fail closed. A symlink passed as the repository may be resolved once to its canonical directory before the sibling root is chosen.

## 8. Resume and drift handling

A valid active resume point is a pair: canonical repository file plus a non-empty marker such as a symbol or line description. Both values are present or both are empty. On resume, load and validate the root marker and authoritative JSON before trusting any projection. If the repository, learning-root path, chain tree, markers, Day/checkpoint semantics, or resume file has drifted, stop without mutation and report the mismatch. Repair only derived Markdown from valid authority; never guess repairs for invalid authority.

## 9. Local-only and sanitized export

The workspace is local-only by default. Initialization, learning, diagrams, notes, and summaries do not imply permission to initialize Git, commit, push, publish, message another system, or modify the business repository. If the user later authorizes publication, build a sanitized export that uses repository-relative identities, removes canonical local paths and private material, review the export, and publish only that reviewed copy.

## 10. Root identity

`.learning-root.json` marks an owned learning root. Its `schema_version`, `learning_root_id`, canonical learning-root path, and repository path must agree with `学习状态.json`. A missing, copied, forged, mismatched, or unsupported marker invalidates the workspace. Cleanup may act only on residue whose complete marker matches the expected root identity and record.

## 11. Commit point and cleanup

Atomic replacement of `学习状态.json` is the authority commit point. Markdown replacement occurs afterward and may be retried because it is a projection. If authority replacement fails before commit, a newly generated chain tree may be removed only when its full generated marker and stable filesystem identity match the expected record. Unknown, replaced, malformed, or user-owned directories are preserved. Initialization publishes a complete temporary root with a no-replace rename; any uncommitted matching temporary root is removed on failure.

## 12. Source identity

Source entry points for a chain are recorded in its `链说明.md` as plain text; there is no generated source directory registry.

## 13. Artifact projection blocks

Generated chain briefs and Day overviews each contain exactly one managed block:

```text
<!-- learning-coach:artifacts:start -->
...generated artifact links...
<!-- learning-coach:artifacts:end -->
```

Artifact projection may replace only the bytes between these markers. It must preserve all human-authored bytes before and after the block. The JSON artifact registry is authoritative; projection failure leaves committed JSON intact, and retrying projection from the same JSON is idempotent. Missing, duplicated, reversed, or malformed markers are drift and must fail without rewriting the file.
