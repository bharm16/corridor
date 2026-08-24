# Corridor Operations

Corridor Operations turns registered Documents into reproducible Candidates and maintains current support without asking customer users to operate technical machinery.

## Language

**Document**:
A registered source artifact whose identity, type, date, and renditions are known to Corridor.
_Avoid_: file, upload, source of truth

**Extraction Run**:
One immutable attempt to read one Document with a stated extractor configuration.
_Avoid_: extraction, pass, job

**Active Run**:
The one production Extraction Run declared for current work on a Document.
_Avoid_: latest run, newest run, current extraction

**Candidate**:
A cited Dependency or External Party Statement proposed by an extractor and not yet in the Project Record.
_Avoid_: suggestion, draft, proposal

**Admission**:
The entry of a Candidate into the Project Record through human Adjudication or an exact deterministic policy.
_Avoid_: import, promotion, insertion

**Adjudication**:
The human act that decides an unresolved Candidate, Dispute, or Dismissal.
_Avoid_: review, triage, approval

**Abstention**:
The result when an automation cannot prove that one exact write is allowed; it leaves the Project Record unchanged.
_Avoid_: rejection, low confidence, automatic Adjudication

**Unplaced Statement**:
An External Party Statement Candidate whose attribution, timing, or Commitment Scope still needs bounded human work.
_Avoid_: orphan event, statement queue

**Revision Comparison**:
An immutable comparison of two exact Extraction Runs for predecessor and successor Documents.
_Avoid_: revision diff, change report, delta

**Supersession Review**:
The current operations work needed because Operative Support still uses a superseded Document or revision processing is incomplete.
_Avoid_: stale list, migration list

**Reconfirmation**:
The human act that moves established Operative Support to current Evidence without changing the admitted conclusion.
_Avoid_: re-adjudication, approval, ratification

**Automatic Carry-Forward**:
The fail-closed policy act that moves established Operative Support to exact unchanged current Evidence.
_Avoid_: automatic Reconfirmation, auto-approval, automatic Adjudication

**Carry-Forward Policy**:
The Corridor-released, named, and versioned rules for Automatic Carry-Forward.
_Avoid_: customer authorization, blanket approval, reviewer bot

**Carry-Forward Run**:
The immutable receipt for one Carry-Forward Policy evaluation and its carried or abstained outcomes.
_Avoid_: bot session, transient log

**Revision Processing**:
The ordered operation that verifies a Revision Comparison before it invokes the Carry-Forward Policy.
_Avoid_: comparison write-back, implicit approval

**Extraction Measurement**:
A scored comparison of exact Extraction Runs with a declared reference and its limits.
_Avoid_: Evaluation, benchmark, eval result

**Cohort Receipt**:
The immutable membership of one bounded rehearsal population.
_Avoid_: cohort, sample, batch

**Lane**:
A bounded operations path whose offered Candidates and allowed mutations share one scope.
_Avoid_: tab, view, filter, mode
