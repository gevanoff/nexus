# Coding forced-action enforcement

## Failure addressed

The stagnation controller could classify repeated inspection and issue assist, interrupt, and recovery guidance, but those interventions were advisory. A model could continue invoking read and search tools, and an unchanged resume could receive another run-level no-progress window. Review missions also inherited implementation-oriented change and commit requirements.

## Enforcement model

A controller-owned `nexus_coding_forced_action.v1` record is activated at interrupt, recovery, or no-progress continuation. It is keyed to the same durable state fingerprint used by stagnation recovery. While active, native tool definitions and text-tool guidance expose only focused edits, recognized validation commands, `coding_git_diff`, and `coding_finish`. The execution layer independently rejects any other requested tool.

Two rejected calls cause either a semantic backend reroute with the same restrictions or an immediate `forced_action_noncompliance` pause. Increasing cycle budgets cannot bypass this boundary. The record remains active across unchanged resumes and becomes inactive automatically when the durable state key changes.

## Required action

The controller extracts the latest explicit model commitment framed as an edit, validation, or finish action. It replaces generic required-action text only when the existing directive is generic. Assistant commitments remain provenance-marked; they are used as execution directives, not trusted findings.

## Review missions

Goals that are clearly review/audit-only default to no required file change and no required new commit. If a review produces edits, the ordinary validation, diff-review, and commit gates still apply. Fix-oriented goals retain the existing mandatory-delta behavior.

## Observability

Debug reports expose the active forced-action record, durable state key, required action, activation and resume counts, and the current allowed-tool list. Runtime events distinguish individual rejections (`forced_action_tool_rejected`), model reroutes (`forced_action_reroute`), and terminal policy failure (`forced_action_noncompliance`). This makes enforcement failures separable from ordinary no-progress pauses.

## Compatibility

Existing task fields remain optional. Stale forced-action records are ignored when their durable state key no longer matches. New no-progress continuations do not receive a recovery-counter reset; legacy metadata remains readable but is not revived into fresh continuation credit.
