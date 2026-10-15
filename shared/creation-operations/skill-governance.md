# Skill applicability and document identity v1

The request carries `available_skills` (catalog) and `explicit_skill_ids` (user
selection). Legacy `selected_skills` remains a catalog fallback, never evidence of
explicit selection. Checkpoints retain both the catalog and the selection source.
For a confirmed Brainstorm chapter, Core additionally sends an internal
`brainstorm_skill_binding` derived only from the canonical persisted Brainstorm
session: `{version: 1, fingerprint, skills: [{id, role, selection_source}]}`.
The fingerprint binds the frozen IDs, roles, sources, persisted profiles and
verified full executable profiles. Legacy sparse snapshots can only check
fields they retained; current installed fields used to hydrate them also enter
the fingerprint, so a later change cannot silently reuse the old checkpoint.
`selection_source=user` requires an explicit `@Skill` match
in the original user request; automatic primary selection remains `automatic`,
and support dependencies remain `dependency`. Legacy sessions infer `user` only
from a literal original `@Skill title` intersecting a persisted primary Skill.
The chapter's available executable profiles must match the frozen fields;
missing or changed profiles fail with `BRAINSTORM_SKILL_BINDING_UNAVAILABLE`
without replacing the frozen selection. Normal direct turns retain the
catalog/explicit selection rules above.

For `generate` and `execute_skill`, `operation.document_identity` contains `title`,
`source` (`user`, `existing`, `generated`), and `evidence` (literal instruction span
for user titles, existing H1 for preserved titles, complete instruction binding for generated titles).
After validation, persisted operations and checkpoints retain only `title`, `source`,
and `evidence_hash`; raw title evidence is discarded. The title describes the requested deliverable. Skill names and example titles are
not title sources. A user deliberately naming a document after a template is valid.
For a confirmed chapter, its application-generated writing instruction controls the
operation scope, while the effective user-authored root request controls title
provenance. A manual edit of that root request takes precedence over its original
value. Merely mentioning a Skill does not establish its name or stripped alias as
the deliverable title. If a model copies a Skill or example title, the executor may
repair it to a shorter theme only when that theme is independently readable from
the effective user request after Skill-name mentions are excluded. Both local and
external routing validate the same title source before the identity is persisted.
Skill applicability and request evidence for that chapter use the same effective
user request; the generated chapter instruction continues to determine only the
current operation and writing scope.
Patch/transform/undo operations retain their bounded-edit semantics.

The request intent is assessed before candidate exposure. Independently reviewed
metadata and actual steps govern automatic primary and constraint skills alike.
Explicit mentions are separately resolved into use vs discussion or negation.

`execute_skill` additionally carries `skill_assessments`, one per requested skill:
`skill_id`, `metadata_consistent`, `applicable`, `request_evidence`, `conflicts`,
`missing_inputs`. Automatic admission requires consistent metadata, applicability,
literal request evidence, and no conflicts or missing required inputs. User-selected
workflows retain explicit provenance; they do not change the user's deliverable or
document title. Rejected automatic workflows fall back to ordinary generation.
An automatically selected primary workflow frozen into a confirmed Brainstorm
chapter is a committed writing policy rather than a loose catalog candidate:
it still receives independent applicability and missing-input review, but a
rejection is surfaced with reasons and preserved recovery state instead of
silently falling back to ordinary generation. Support Skills do not expand
their own workflow. The Sidecar reports `CREATION_SKILL_BINDING_INVALID` for a
malformed binding or candidate mismatch, `CREATION_SKILL_BINDING_CHANGED` for
a resumed checkpoint without the current fingerprint,
`CREATION_SKILL_REVIEW_UNAVAILABLE` for failed or unavailable independent
review, `CREATION_SKILL_NOT_APPLICABLE` for an actual policy conflict, and
`CREATION_SKILL_INPUTS_MISSING` when required material cannot be obtained.
Material declared in an enabled Skill retrieval step is pending work, not yet
a missing input; its result remains subject to final delivery verification.
A checkpoint without the current chapter binding, or with a different
fingerprint, starts a fresh routing attempt inside the same durable operation;
old generic search, plan and draft suffixes are invalid.
The executor never invents semantic evidence. Local and external model decisions
pass the same admission gate. Missing legacy assessments cannot auto-admit skills.

`skill_description.applicability` is an optional versioned object with
`version: 1`, `use_when`, `not_for`, `required_inputs`, and `output_structure` arrays.
It is persisted across edit/import/export; unknown or malformed versions are rejected
at save. Models compare the title, complete description and actual step objectives,
including legacy descriptions without this object. Description consistency is a
semantic assessment, not a promise supplied by a boolean or a keyword classifier.

`execution_steps[].output_role` is `process`, `section`, or `document`. An omitted role preserves compatibility.
Process results remain context. Section results alone become final sections. A
document result is a full deliverable, not a chapter named after a workflow action.
Legacy checkpoints without output roles retain their historical assembly layout;
new requests use declared roles. For legacy workflows with a full document writer,
the writer produces the document and other steps supply process context. Legacy
workflows without that writer preserve their existing section layout.

Audit events contain IDs, content fingerprints, admission codes and title source;
no new raw instruction logs. Existing user-edited skill data is preserved unless a
versioned migration matches the exact known legacy content fingerprint and creates
a backup. No broad name-based migration is allowed.

## Frozen chapter execution and source receipts

A confirmed Brainstorm chapter executes its frozen primary Skill steps in order.
The primary cannot silently become ordinary generation; support Skills constrain
the writing without adding sections. Each declared step needs its own completed
tool receipt and nonempty retained output after strict assembly. A later step's
tool result cannot satisfy an earlier step with the same tool ID. Strict assembly
removes document or chapter wrapper titles and entire foreign step subtrees,
including nested headings, while preserving the confirmed document title and
previously committed chapter prefix.

An explicit numeric sort and Top N row request is a relational subtask even when
the same step also asks for prose or an unavailable date filter. The executor
binds the sort field to one actual numeric source column, sorts numerically,
limits rows, and carries original row IDs and requested source columns. An
ambiguous relation or field stays an explicit missing query result. A verified
QueryResult is the writer's row-level view; the same source's full raw table is
omitted from that prompt view, while the checkpoint keeps the complete capture.
Delivery checks that verified result rows appear in the document and rejects an
explicit claim that a returned row was absent from its Top N.

The source's displayed statistical period is separate from page collection time.
A Skill step's data search and subsequent page refresh use the same query and
effective period. When a step refers to the requested period without specifying
one, it may inherit a single consistent relative period declared by another
step of the same frozen Skill; that provisional Skill period is a rejection
boundary for false current-period claims, not a user-confirmed report date.
For a time-bound step, every source-verified requested metric needs a statistical
period within that boundary. A refreshed page marked `verified` or `qualified`
does not by itself prove period coverage. Unknown periods and data from outside
the boundary remain labeled as such. Source/date contradictions and false
rank-membership statements request a corrected draft and are rechecked; missing
target-period source coverage leaves the delivery blocked.
Before delivery review, the executor can retract an audited contradiction in
the generated chapter by replacing only its matched sentence or exact table
column with the
verified source period, an explicit unknown-period note, or verified rank
membership. It archives the previous full preview, publishes the bounded
revision, and checkpoints it before the same delivery gate runs. A mismatch in
the audited text or a finding inside a protected chapter is left for review;
the revision never infers a report week or fills missing source values.
Rank auto-revision requires the same sourced section to contain verified row
names and order-field values; historical sections, ambiguous subjects, and
unbound sources remain delivery findings. Other facts in the same paragraph
are preserved when a single contradicted sentence can be identified.

An incomplete but preserved draft remains in the same operation checkpoint and
may be shown for review. It is not a completed deliverable, and `delivery.checked`
cannot pass on source counts, model opinion, or a correctly disclosed historical
value alone. Any later policy to accept a clearly marked gap draft requires an
explicit user choice and a new independent delivery check of that draft.
On resume, a failed frozen chapter reassembles its active preview from the
completed Skill step outputs before delivery review. This is allowed only when
the live body still matches the generated preview or protected base. The prior
preview is retained in the checkpoint as a superseded audit record, and the
replacement is published and checkpointed before validation. A changed user
body or prior completed chapter is never overwritten by this recovery.
