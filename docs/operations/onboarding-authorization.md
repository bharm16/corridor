# The limited onboarding authorization

What it is, who issues it, what it permits, how it ends, and — the part
[ADR-0099](../adr/0099-onboarding-before-activation-runs-under-a-limited-authorization-not-under-relaxed-activation-checks.md)
requires before customer deployment — **how long a stale positive answer can
survive**.

Built by #827. This page is the contract operations answers with; the code is
`src/corridor/onboarding_authorization.py` (the customer environment's half),
`src/corridor/control_plane.py` (`OnboardingCustody`, the authoritative half),
and `src/corridor/migrations/source_append_commands/onboarding_authorization.py`
(the enforcement).

## What it is, and what it is not

A limited onboarding authorization is the permission under which the work that
must precede authoritative activation is done: reaching a provisioned project,
receiving and safely staging a baseline workbook inside a recorded source
scope, inspecting and mapping it once its exact bytes are hashed, reviewing the
material questions it raises, and recording the coordinator's adoption
approval.

It is **not** permission to skip the activation checks while a project is
unadopted. Every operation outside the set it names is refused for exactly the
reasons it is refused today.

## Who issues it

The **control plane is authoritative**. A restricted operations or deployment
actor issues it from the recorded customer-data authorization, through the
control-plane operations credential:

```bash
make control-plane ARGS="onboarding-issue --file onboarding-authorization.json"
```

A project coordinator cannot issue, extend, annotate or revive one. That is
enforced rather than described: `corridor_web` holds no execute on
`record_onboarding_grant` or `record_onboarding_grant_event` and no write on
any of the four onboarding relations, and the control-plane database is a
different database reached by a different credential.

Membership and customer-data authorization are different grants held by
different parties. A designation on a project membership says what a person may
do inside a customer database; it does not say the customer permitted that
database to be processed at all.

## What the record holds, and what it never holds

The control-plane row binds customer, environment, project, permitted
operations, source scope, the governing customer-authorization identity **and
version**, the retained evidence's reference **and digest**, issue time,
expiry, issuing actor and version. It holds **identifiers and digests** — never
the signed document and never the customer's workbook.

The customer environment holds what it owns: the validated preview, the
mapping, the answers, and the adoption receipt.

## Recording it in the customer environment

The grant the customer database enforces against is recorded by the operations
capability, through `record_onboarding_grant`. A reissue is a new row at a
higher `grant_version`; nothing is ever updated.

## Three identities, and what each one binds

Two of the three name material a customer supplied, so they read as one and
are not. Operations answering a question about "the evidence" has to say which
of these it means.

| Identity | What it proves |
|---|---|
| Governing authorization identity **and version** | Which customer permission governs the operation |
| Authorization evidence reference **and digest** (`evidence_identity`, `evidence_sha256`) | Which retained document supports that permission |
| Source delivery identity **and content digest** | Which project material the operation will process |

A signed authorization document and an uploaded UCM workbook ordinarily have
**different digests**. `evidence_sha256` is therefore never the workbook's
digest: a check comparing the two would refuse every honest preparation, and a
grant recording a workbook digest there would change what older authorization
evidence means. `tests/test_architecture.py` keeps the field inside the modules
that record, carry and enforce the grant.

## The two-stage source binding

1. The recorded customer/project/source-scope authorization permits **bounded
   receipt and safe staging**, and nothing but holding the material safely.
2. Once the bytes are received and hashed, the **exact source identity** is
   bound to the permission, and every operation after that point is permitted
   against that identity rather than against the scope that admitted it.

This is not permission to process arbitrary uploads.

**What stage 1 enforces today, and what it does not.** The permission itself is
enforced: `prepare_baseline_reading` proves a current `inspect_compatibility`
grant for this project in the database before it opens anything, and the
delivery it names is re-proved against the ledger — this project, these exact
bytes, a stored disposition, and a source binding this project's recorded
authorization still permits. The grant's own `source_scope` is **recorded and
not compared**: it is a narrative sentence, and no operation is admitted or
refused by it. Giving it a typed, versioned contract — permitted alternatives,
conjunctive restrictions within each — is [#951](https://github.com/bharm16/corridor/issues/951),
and until it lands a narrative scope should be read as a statement of intent
that operations enforces by what it issues, not as an enforced restriction.

## What consumes it

| Operation | Consumes the adoption permission? |
|---|---|
| Bounded receipt and staging | No |
| Compatibility inspection | No |
| Preparing or correcting a mapping | No |
| Reading or regenerating a reading | No |
| Answering a material baseline question | No |
| Failed or rolled-back adoption | No |
| **Successful adoption commit** | **Yes** |
| Exact retry of the completed adoption | Returns the existing receipt; no second adoption |

Consumption is **derived**, not flagged: a committed `adopt_baseline` row in
`project_onboarding_acts` *is* the consumption, and that relation is immutable
and uniquely keyed on `(project_id, operation)`, so a concurrent second attempt
cannot consume it twice.

## What is permitted after adoption

Consumption ends the adoption permission. It does not end the others, and the
others are not unconditional either.

| After adoption | Decision |
|---|---|
| Read the completed reading, answers and receipt | Allowed under current read authorization |
| Repeat compatibility inspection of the bound source | Allowed while explicitly permitted, in scope, unexpired and unwithdrawn |
| Regenerate a technical reading | Allowed for verification where permitted, and **clearly not a new adoptable baseline** |
| Change the answers recorded in the completed adoption | **Not allowed** — immutable history |
| Register a replacement mapping | The separate supported act and its own authority (#829) |
| Approve the initial issue profile before activation | Only through the **explicit bounded setup permission** named for it (`approve_issue_profile`) |
| Adopt again | Refused, except an exact retry returning the original result |
| Upload and process ordinary later revisions | The ordinary activated path, not leftover onboarding authority |

**A remaining preview permission does not override the project's one-way
adopted state.** `retain_onboarding_preview` reads `project_operating_mode` and
records the answer on the row, so a reading prepared after the project adopted
is retained with `adoptable = false` and `commit_onboarding_act` refuses it —
while the compatibility permission that produced it stays perfectly valid,
which is the point.

## Three endings, and they are not one sentence

ADR-0099's "no continuing onboarding write authority after it expires, is
withdrawn, or is consumed" reads as one rule and is three:

- A **consumed adoption permission** ends permission for new adoption writes
  and nothing else.
- **Expiry or withdrawal of the relevant permission** ends new protected
  onboarding writes under that permission. It reaches back into nothing that
  was already committed, and decides nothing about a different permission the
  same grant carries.
- **Authorized retrieval of an existing record** is neither. Reading the
  completed onboarding status, or returning the receipt an exact retry names,
  is retrieval of a prior result under current read access.

## The exact retry

An exact retry is a **separate request idempotency key** plus a **canonical
material-payload binding**. The five identities PostgreSQL validates are
payload and authority checks; they do not identify the user's particular
submission, and the key plus the digest do.

| Submission | Result |
|---|---|
| Same key, same material payload, previously committed | The original receipt |
| Same key, different material payload | Refused as conflicting reuse |
| New key, project already adopted | Refused; a second adoption has nowhere to land |
| First request rolled back | A valid retry may perform the act |
| Concurrent identical submissions | One adoption; both converge on its retained result |

Fresh cookies and request-forgery tokens are not adoption content and are not
in the digest. A retry checks current identity and read access and **does not**
require the temporary permission still to be live, and does not fail because a
later configuration change means the original reading is no longer today's
proposed one.

## A superseded governing authorization

A grant names a specific governing customer-authorization version. When that
version is superseded or withdrawn, record
`governing_authorization_superseded` against the grant. New protected writes
then require **explicit revalidation and a current grant**; nothing silently
inherits broader or narrower permission from the replacement document.

Completed acts keep their recorded evidence and their valid-at-the-time status.
Reissuing permission does not require re-adopting a valid baseline.

For an administrative correction that genuinely does not change permission,
operations records explicit continuity or reissues the grant. **There is no
"probably just a typo" inference from document differences, and there will not
be one.**

## Withdrawal

**Who may.** An authorized operations or security actor executes a withdrawal.
A verified customer representative authorized to control the processing
permission may require one. Ordinary project membership confers neither.
Withdrawal does not depend on the individual who issued the authorization still
being available.

**What is recorded.** Who requested it, who executed it, the reason, and its
effective and enforcement state — which are two different facts, recorded as
two different events:

| Event | Meaning |
|---|---|
| `withdrawal_requested` | The customer required it; new protected onboarding writes stop |
| `withdrawal_enforced` | Enforcement in the customer environment is confirmed, at a recorded time |
| `withdrawal_enforcement_failed` | An enforcement attempt failed, with the detail operations has to answer for |

**What each audience is told.**

- *Operations* gets the full record: requester, executor, governing
  authorization and version, effective request time, customer-environment
  enforcement state, and any outstanding failure. Read it with
  `make control-plane ARGS="onboarding-show <authorization-id>"` and, for the
  customer-side half, `withdrawal_record(...).operations_record()`.
- *The verified customer requester* gets an accurate acknowledgement, sent as
  an attributable operations communication — no new portal for the pilot. The
  wording is `WithdrawalRecord.customer_acknowledgement`, and it says
  "confirmation that processing has stopped in the customer environment is
  pending" until an enforcement event exists.
- *The project coordinator* is told why onboarding is paused and which
  operations remain available, on their own project's page, **without** the
  global control-plane registry and without the customer's legal evidence.

The two coordinator sentences are distinct on purpose:

> Withdrawal requested. Confirmation that processing has stopped is pending.

> Onboarding writes are disabled for this project as of \[time].

**An unqualified "withdrawn" is never shown while the customer-side grant is
still usable.**

Withdrawal does not erase a previously recorded adoption. Any later correction
follows an explicit supported act.

## The maximum stale-validity window

**There is no offline grace for starting a new protected onboarding act.**

A positive validity answer is good for **15 minutes**
(`onboarding_authorization.REVALIDATION_WINDOW`) measured from the grant's
issue time or from the most recent recorded `revalidated` event, whichever is
later. Past that, `onboarding_grant_standing` returns
`onboarding_authorization_revalidation_required` and every protected act
refuses until operations records a fresh `revalidated` event. A grant whose
validity cannot be established refuses; a stale local copy never proves
validity indefinitely.

**What that does and does not guarantee.** It guarantees that an onboarding act
commits only while a validity answer at most 15 minutes old still holds, and
that it fails closed otherwise. It does **not** claim instantaneous
cross-database revocation. The authorization is authoritative in the control
plane and enforced in the customer database, and the propagation between them
is an operations act, not a transaction.

**The operation and verification window, end to end.** Recording a withdrawal
in the control plane is one command; recording the matching customer-side event
is a second. Between them, the customer environment continues to permit
protected onboarding writes. So the stated window is:

> the time operations takes to run the customer-side event, plus at most 15
> minutes for any positive answer already in flight — and no longer.

**In-flight work is fenced by the transaction, not by the window.** A protected
act reads the standing inside the transaction that writes, so an act that
started before a withdrawal and commits after it is refused at commit. An act
that already committed stays committed, with its retained proof of the validity
it had, which is exactly what ADR-0099 requires.

## The runbook

1. **Issue.** `onboarding-issue` in the control plane, then
   `record_onboarding_grant` in the customer environment.
2. **Revalidate** while onboarding is in progress, at least every 15 minutes of
   active work: `record_onboarding_event(kind="revalidated")`.
3. **Withdraw.** `onboarding-event` with `withdrawal_requested` in the control
   plane, then the same event in the customer environment — that second one is
   what stops the writes. Send the requester the acknowledgement.
4. **Confirm enforcement.** Record `withdrawal_enforced` in both, with the
   time. Only then does any surface say writes are disabled.
5. **If enforcement fails**, record `withdrawal_enforcement_failed` with the
   detail. The coordinator still reads "pending", because that is the truth.
