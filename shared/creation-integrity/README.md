# Creation document acceptance

Generated prose is a candidate until deterministic validation succeeds. Writer,
Skill and external model returns share the same acceptance gate. Model-generated
local patches also reject repeated prose before mutation. Explicit user-supplied
patches keep the document-operation contract, including intentional deletions.

- Substantive repeated lines or sentences reject the candidate; short labels,
  Markdown separators and fenced code are exempt.
- Automated polish must preserve ordered level-two headings and must not remove
  most of an existing document. Rejected polish retains the prior valid document
  and emits `document.mutation.rejected` with non-content problem codes.
- Initial generation and final acceptance reject repetitive documents with
  `CREATION_DOCUMENT_INVALID`. Local/cloud completion with a length stop raises
  `CREATION_DOCUMENT_TRUNCATED`; partial output is not a successful document.
- Risk disclosure applies to adopted facts, not every retrieved metric. Keep real
  periods and necessary limitations; combine duplicate risks per source. Rebuild
  managed disclosures idempotently. Never infer a weekly reporting requirement
  for ordinary historical background.

The guards are deterministic symptom checks, not a claim that all factual or
semantic quality can be decided without review. The final candidate is still
subject to normal evidence and scope validation.
