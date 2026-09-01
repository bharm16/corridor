# Architecture decision records

One sequence, one directory. Every ADR is `NNNN-title.md`, numbered in order of
acceptance, and stays here for as long as the repository exists. The generated
[current-decision index](INDEX.md) says which decisions govern today; the
files say why the architecture took each shape along the way.

## Lifecycle

The format follows the sequential-numbering and status convention of the
common ADR template (proposed, accepted, deprecated, superseded by ADR-NNNN).
That template leaves the boundary between an in-place edit and a new decision
open, so Corridor defines it here and enforces it in
`tests/test_architecture.py`.

**Never physically delete an accepted ADR merely because the decision changed.**
Correct non-normative mistakes in place. Record every material decision change
in a new sequential ADR. Mark a fully replaced ADR as superseded, mark an
abandoned decision without a replacement as deprecated, and represent partial
amendments through explicit machine-readable relationships.

Two tests decide whether an edit is material:

1. Could code complying with the old ADR fail review under the proposed
   wording? If yes, it is a material change and needs a new ADR.
2. Does the edit change any normative "must", "may", "never", authority
   boundary, scope boundary, or rejected alternative? If yes, create a
   successor.

Everything else (typos, broken links, terminology notes that change no
authority, a corrected status line) is edited in place.

"Delete" applies only to an unmerged draft that was never accepted, an
accidentally generated duplicate, or a file that was never an ADR and has no
historical references. No archive directory exists: moving a file breaks
relative links, stale-dates issue and source references, and hides the
sequence. Filter by status instead.

## Frontmatter

Every ADR opens with YAML frontmatter, `status` first:

```
---
status: accepted
domain: record-inclusion
scope: current product
supersedes:
  - ADR-0029
amends:
  - ADR-0001
amended_by:
  - ADR-0081
migration: what is still unresolved, in one sentence
---
```

- `status`: exactly one of `proposed`, `accepted`, `deprecated`, or
  `superseded by ADR-NNNN` (several successors joined with ` and `). A
  superseded ADR cannot also be deprecated; a deprecated one names no
  successor.
- `domain`: the decision area the index groups by (`product`,
  `project-record`, `record-inclusion`, `extraction`,
  `supporting-documentation`, `human-work`, `reports`, `operations`,
  `intake`, `retention`, `testing`, `terminology`, `migration`).
- `scope`: `current product`, `optional module`, `historical`, or `future`.
  A superseded ADR is `historical`. An accepted ADR whose expansion is
  frozen (ADR-0075) is `optional module`.
- `supersedes`: reciprocal of a successor's `superseded by` status.
- `amends` / `amended_by`: a non-total amendment, declared on both sides. The
  successor writes `amends`; the predecessor writes `amended_by`. The body of
  the amended ADR is not rewritten.
- `migration`: present only while something the ADR requires is unbuilt.

The architecture test validates that every ADR has one valid status, every
superseded ADR names an existing successor that lists it under `supersedes`,
every `amends` has its reciprocal `amended_by`, the supersession and
amendment graph has no cycles, and `INDEX.md` matches the frontmatter.

## Writing one

Title as the decision in one sentence. Open with the context that forced the
decision, state the decision, list the considered options with why each was
rejected, and close with consequences: what changes, which ADRs it amends or
supersedes, and what remains unresolved. Name the superseded or amended ADRs
prominently at the top of the body as well as in the frontmatter. Operational
counters, thresholds, and runbooks go in `docs/operations/`, not here; a
temporary validation gate goes in `docs/`, not here.
