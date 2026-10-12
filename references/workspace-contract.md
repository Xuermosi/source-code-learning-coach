# Source-Code Learning Workspace Contract

This document defines the persistent workspace written by the source-code learning coach. The command surface is intentionally not duplicated here; run `python3 scripts/manage_learning_workspace.py --help` and the relevant subcommand `--help` for current usage.

## 1. Roadmap input

The roadmap is a UTF-8 JSON object. Day numbers are consecutive integers beginning at 1. Checkpoint IDs are non-empty and unique across the route. Each checkpoint weight is a positive integer, and the weights within every Day total exactly 100. Stage ranges are ordered, adjacent, non-overlapping inclusive integer intervals that cover 0 through 100. When omitted, `stage_ranges` defaults to `[[0, 30], [31, 60], [61, 100]]`.

Complete minimal example:

```json
{
  "title": "Repository Learning Route",
  "stage_ranges": [[0, 30], [31, 60], [61, 100]],
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

`学习状态.json` is the sole state authority. Its `schema_version` is currently `1`; unsupported versions fail closed. It contains the learning-root ID and canonical paths, route title and Day count, stage ranges, ordered Day/checkpoint state, current Day/stage/progress/resume data, stage/source/artifact registries, history, and `updated_at`.

`学习状态.md` is only a human-readable projection of that JSON. Never infer authoritative state from the Markdown file. A projection mismatch is repairable by synchronizing from valid JSON.

## 3. Names and layout

The learning root is a sibling of the canonical repository and is named:

```text
<sanitized-project-name>-<total-days>天学习路线
```

All Day directories are created at initialization. Their numeric width is `max(2, digits(total-days))`, producing names such as `Day01` for a route below 100 Days. A stage directory is `进度条<start>-<end>%`; later revisions add `-修订NN`. A source directory begins with its two-digit stage-local reading order followed by a collision-safe label.

## 4. Progress and range mapping

Day progress is the sum of weights for completed checkpoints in that Day. It is recomputed rather than trusted as an independent counter. Inclusive stage boundaries map progress to the one interval satisfying `start <= progress <= end`. A transition may skip an unused interval when one checkpoint crosses multiple boundaries.

## 5. Active, sealed, and revision lifecycle

At most one Day and one stage are active. Starting the next pending Day creates its zero-progress stage. Moving to another progress interval seals the active stage and creates the target stage. Sealed stage trees are immutable history. Reopening a completed checkpoint may move progress backward; when its base stage path has already been registered, a new revision is created instead of overwriting the old tree. Revisions start at `01` and increase monotonically for that Day and range.

## 6. Ownership of source and Day overview files

The coach owns generated marker files, authoritative JSON, generated Markdown projections, source directories registered in JSON, and the artifact-managed blocks inside source guides and Day overviews. Human-authored content outside a managed block belongs to the user and must not be rewritten by artifact projection. Day-level questions, summaries, and overview material belong to the stage in which they were created; source-specific material belongs to its registered source directory.

## 7. Path and symlink constraints

Repository, learning root, Day, stage, and source paths are canonicalized and checked by path components, not string prefixes. The learning root must remain a sibling of the repository. Source and resume files must be existing regular files inside the canonical repository. Generated paths must remain inside their declared owner. Symlinked Day/stage/source parents, escaping symlinks, unsafe filename components, and unsupported secure directory operations fail closed. A symlink passed as the repository may be resolved once to its canonical directory before the sibling root is chosen.

## 8. Resume and drift handling

A valid active resume point is a pair: canonical repository file plus a non-empty marker such as a symbol or line description. Both values are present or both are empty. On resume, load and validate the root marker and authoritative JSON before trusting any projection. If the repository, learning-root path, stage tree, markers, Day/checkpoint semantics, or resume file has drifted, stop without mutation and report the mismatch. Repair only derived Markdown from valid authority; never guess repairs for invalid authority.

## 9. Local-only and sanitized export

The workspace is local-only by default. Initialization, learning, diagrams, notes, and summaries do not imply permission to initialize Git, commit, push, publish, message another system, or modify the business repository. If the user later authorizes publication, build a sanitized export that uses repository-relative identities, removes canonical local paths and private material, review the export, and publish only that reviewed copy.

## 10. Root identity

`.learning-root.json` marks an owned learning root. Its `schema_version`, `learning_root_id`, canonical learning-root path, and repository path must agree with `学习状态.json`. A missing, copied, forged, mismatched, or unsupported marker invalidates the workspace. Cleanup may act only on residue whose complete marker matches the expected root identity and record.

## 11. Commit point and cleanup

Atomic replacement of `学习状态.json` is the authority commit point. Markdown replacement occurs afterward and may be retried because it is a projection. If authority replacement fails before commit, a newly generated stage or source tree may be removed only when its full generated marker and stable filesystem identity match the expected record. Unknown, replaced, malformed, or user-owned directories are preserved. Initialization publishes a complete temporary root with a no-replace rename; any uncommitted matching temporary root is removed on failure.

## 12. Source identity

A source registration is identified by the pair `(stage_id, canonical_source_path)`. The same physical source may therefore be studied again in a later stage and receive a new stage-local directory and order. A path alone is not a repository-global source identity. JSON also stores a repository-relative path for portable or sanitized export.

## 13. Artifact projection blocks

Generated source guides and Day overviews each contain exactly one managed block:

```text
<!-- learning-coach:artifacts:start -->
...generated artifact links...
<!-- learning-coach:artifacts:end -->
```

Artifact projection may replace only the bytes between these markers. It must preserve all human-authored bytes before and after the block. The JSON artifact registry is authoritative; projection failure leaves committed JSON intact, and retrying projection from the same JSON is idempotent. Missing, duplicated, reversed, or malformed markers are drift and must fail without rewriting the file.
