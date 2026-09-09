# Minutes on the source and delta spine

The v1 contract is the five capabilities in #456: Commitments, Changes to Promised
Timing, Completion Reports, speaker attribution and Applies To. Source capture
does not change accepted authority. It uses the existing bounded prose call and
strict typed-output validator, the registered subject catalog, source materializer,
Source Fact append command and Proposed Delta lifecycle.

The input is a registered minutes Document in an adopted project, with exact
native prose segments and retained rendition/reader identity. The reader verifies
that complete reading from the retained bytes before a provider sees the catalog.
It allows 500 segments and 120,000 characters, with no silent truncation. A missing
or unreplayable reading refuses processing. Scanned sources use the existing
authorized PDF/OCR preparation path; this lane adds no provider bypass.

The catalog classifies meeting metadata, agenda/action markers, speaker labels,
authored text, quoted history and draft markers. A statement names its source
segment, mechanically associated speaker label, registered organization/person,
optional timing reference, accepted predecessor reference and explicit scope
references. Provider output contains integer references and closed enums only.
Names, dates, wording, customer/project boundaries and acceptance commands cannot
be supplied as model literals.

Timing phrases use one shared parser with the legacy minutes adapter. Days,
months and explicit endpoint ranges keep their stated precision. A changed date
from one month to another is not interpreted as a delivery range. Qualifiers such
as “around” remain approximate with no invented date bounds. A timing change or
Completion Report needs one unambiguous accepted predecessor; unresolved identity,
attribution, scope or timing remains a retained source question. Completion Reported
is distinct from closing a Constraint or Contract Acceptance.
An explicit Required By clause cannot supply Promised Timing. Negated, qualified
or hypothetical completion stays unresolved. Registered people with overlapping
names or aliases require clarification; the model cannot rank them into certainty.

Selected Applies To references name accepted Project Record subjects and their
own exact source spans, not new legacy Dependency rows. The original source
identifier stays beside each reference. No scope silently expands when another
Constraint appears. Missing scope remains “Applies To: not yet known” as read-only
context. A source that explicitly states unknown scope can supply that Fact;
otherwise absence of scope produces no invented Source Fact or accepted change.

Each capture retains the accepted revision it compared, input identity, run and
configuration, source accounting, Facts, deltas and unresolved outcomes. A declared
source family identifies revisions; filenames/content never merge independent
meetings. The newest captured revision supersedes only that family's unaccepted
deltas through `DeltaSupersession`. Accepted decisions and independent sources
stay intact. No absence from minutes creates an apparent-removal delta.

```sh
make minutes ARGS="project-slug"
make minutes-source ARGS="inspect 123"
make minutes-source ARGS="capture 123 --source-family meeting-2026-09-09 --response response.json"
make test-focused ARGS="tests/test_minutes_spine.py"
```

Normal project extraction also dispatches adopted-project minutes to this lane.
Both commands and normal processing use the retained delivery's external identity
and version by default. Without a delivery, the document registry ID (or rendition
digest) supplies the family and its digest supplies the revision. Explicit family
and revision flags describe a declared source lineage, never model output.
The `minutes` command preserves the frozen legacy command for legacy projects.
`inspect` exposes the actual catalog for an offline response replay. Repeating an
unchanged capture returns its receipt without a provider call. A rolled-back or
crashed capture retries atomically. A changed accepted revision during reading
refuses before Facts or proposals are frozen.

The latest source questions appear in the existing review and project-work views;
they create neither accepted facts nor external follow-up obligations. Supporting
assessments and Resolve Delta remain the existing human decision boundary. Source
quotes and successful schema validation do not prove real-world completion.
Apply on a new Commitment carries every captured field in one Review Packet act;
each field needs its own effective value-support assessment before it is ready.

Fixtures cover all five capabilities, source replay, month/range precision,
project-side and ambiguous speakers, unknown scope, ambiguous predecessors,
drafts/quotes, contradictory readings, normal dispatch and crash retry. Account
configuration, actual deployments, signatures/authorizations, customer runs and
pilot findings remain later operational work. A sixth semantic capability or a
different source taxonomy needs separately scoped work.
