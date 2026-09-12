# Skill applicability and document identity v1

The request carries `available_skills` (catalog) and `explicit_skill_ids` (user
selection). Legacy `selected_skills` remains a catalog fallback, never evidence of
explicit selection. Checkpoints retain both the catalog and the selection source.

For `generate` and `execute_skill`, `operation.document_identity` contains `title`,
`source` (`user`, `existing`, `generated`), and `evidence` (literal instruction span
for user titles, existing H1 for preserved titles, complete instruction binding for generated titles).
After validation, persisted operations and checkpoints retain only `title`, `source`,
and `evidence_hash`; raw title evidence is discarded. The title describes the requested deliverable. Skill names and example titles are
not title sources. A user deliberately naming a document after a template is valid.
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
