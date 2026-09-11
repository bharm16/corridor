---
status: accepted
domain: retention
scope: current product
amends:
  - ADR-0080
migration: the pass exists and is proved, but nothing runs it on a schedule; the environment-scoped deployed invocation specified below is a deployment task that is not built, and until it exists expiry depends on somebody running the command. All three periods are recommended operating defaults subject to the applicable customer records schedule and to legal review; no such schedule has been read.
---

# A sign-in record is deleted once it can no longer authorize anyone, and an attempt log is kept longer

**Amends ADR-0080.** It adds one class of data that ADR-0080's four retention
classes never covered, and one expiry regime for it. Everything ADR-0080 says
about the Project Record, about Class B intermediaries, and about governed
disposition is unchanged, including its rule that a legal hold suspends *every*
deletion path — the pass this ADR authorizes is suspended by one too.

## Context

`web_sessions`, `sign_in_tokens` and `sign_in_attempts` are the three relations
the sign-in system writes (#331). All three evaluate expiry inline in the
`WHERE` clause against an injectable clock, which is correct for authorization:
`resolve_web_session` will not return an expired or revoked session, the
consuming `UPDATE` will not spend an expired or already-spent link, and
`count_recent_attempts` counts only inside `ATTEMPT_WINDOW`. An expired row is
never *read* as valid.

But nothing deleted them. Expired sessions, consumed and unconsumed links, and
every recorded attempt to reach the product accumulated for the life of the
customer database (#907). These are personal data — an email address, a
principal subject, a client address — held with no stated period.

ADR-0080 does not cover them. Its four classes are the Project Record (A),
assistant and processing intermediaries (B), rebuildable indexes (C) and cost
records (D). A session record is none of those: it is not the customer's
record, it is not an intermediary of any processing run, it cannot be rebuilt,
and it is not a cost. ADR-0080's disposition apparatus does not fit it either.
That apparatus is per project — every Class B candidate carries a `project_id`,
a hold is placed on a project, a dry-run manifest lists a content digest per
row, and execution *nulls the content columns* so the receipt keeps its
identity. A sign-in record has no project, has no content to null because the
whole row is the credential, and is keyed to a person or, for an attempt, to a
normalized email or client address that may belong to nobody. So this is not a
case of an existing sweep having missed three tables. There was no mechanism
they could be added to.

Published practice, researched before any number was proposed
([note](../research/sign-in-record-retention-2026-09-11.md)), turns out to be
uneven in a way that shapes this decision:

- **It is emphatic that a period must exist, and it delegates the number.**
  NIST SP 800-63B-4 §2.4.2 (July 2025) directs a verifier to comply with the
  records retention requirements that apply to it and, where none mandates a
  period, to run a privacy and security risk process "to determine how long
  records should be retained" and inform the subscriber. OWASP ASVS 5.0
  requirement 16.1.1 requires the log inventory to document "for how long logs
  are kept". Neither supplies a value.
- **It says nothing about deleting a session record or a spent link.** Every
  requirement found — NIST SP 800-63B-4 §5.1, NIST SP 800-63A-4 §3.8, ASVS
  7.4.1 — says *invalidate*, never *delete*. There is abundant guidance on when
  a session must stop working and none on when its row must go. A permitted
  credential lifetime is not a retention allowance for the dead row: AAL1's
  thirty-day reauthentication ceiling bounds how long a session may keep
  working, and says nothing about keeping its expired row for thirty days
  afterwards.
- **It gives audit-log figures, and none of them is established as governing
  this relation.** CIS Critical Security Controls v8 Safeguard 8.10 recommends
  a minimum of ninety days for enterprise assets under IG2 and IG3. CNIL
  Délibération n° 2021-122 ¶8 generally discusses six months to one year for
  connection logs, with contextual exceptions (¶19 allows three years where
  logging also serves internal control) and an explicit minimisation argument
  (¶22: a log should not keep personal data the logged processing no longer
  keeps). PCI DSS v4.0.1 10.5.1's twelve months **was not verified against the
  primary document** — PCI SSC gates it behind a click-through — and Corridor,
  which stores no cardholder data, is not in PCI scope; it is recorded because
  it is the figure most often quoted, not because it applies. Longer is not
  automatically safer or more compliant, and a period chosen to satisfy a scope
  Corridor is not in would be a year of personal data held for a hypothetical.

And no source draws the distinction #907 assumes — that an attempt record is a
security log while a session and a token are operational credentials. The
research note says so plainly rather than assembling support out of fragments.
The argument for that distinction has to be made from Corridor's own facts, and
it can be: `audit.SIGN_IN` is written at session creation, so a **successful**
sign-in leaves a durable `audit_log` entry that `identity_audit` exports. A
**failed or unknown-email** attempt leaves no audit entry at all.

## Decision

### 1. The three relations are one class of their own, and a row in it is deleted, not emptied

They are **sign-in records**: the machinery that establishes and bounds access,
keyed to a person rather than to a project. A sign-in record carries no
authority, supports no accepted value, and is cited by nothing. When its period
ends the whole row goes. There is no manifest of digests, because there is no
content whose identity survives its deletion; there is no attributable human
approval, because there is no customer record being disposed of and nothing for
a person to review; and there is no export obligation, because everything
durable about the act is already in `audit_log`.

### 2. The three periods, each measured from the moment the row could last authorize something

| Relation | Period | Measured from |
|---|---|---|
| `web_sessions` | **7 days** | the earlier of `expires_at` and `revoked_at` |
| `sign_in_tokens` | **24 hours** | the earlier of `expires_at` and `consumed_at` |
| `sign_in_attempts` | **180 days** | `occurred_at` |

**Each number is when a row becomes eligible for deletion. It is not a maximum
row lifetime, and the two are separate terms.** A record becomes eligible for
deletion at its retention threshold. Under normal operation, the next daily
sweep removes it within an additional 24 hours. Legal holds, failed runs, and
recovery delays are reported separately. So the `sign_in_tokens` row of the
table above says that a spent link becomes eligible twenty-four hours after it
died — not that it is gone within twenty-four hours, and not that it has a
twenty-four hour maximum lifetime. Section 5 says how a hold is reported and
what it does and does not undo; the cadence bullet under Consequences says how
a run, a failure and a missed window are.

**No exact-second deletion promise is made here, and none is being engineered
towards.** A deadline tighter than "eligible at the threshold, removed at the
next daily sweep" would take a time index on the three relations or a more
frequent invocation, and neither is warranted before the scheduled pass of #943
has been deployed and its cost observed: measure the pass as it runs, and add
an index when its query plan or observed cost calls for one. A customer who
requires a tighter deletion deadline needs an explicit cadence-and-lag
commitment agreed before enrollment. None of the three numbers above is one.

**These are recommended operating defaults, and all three are product
judgements.** They are subject to the applicable customer records schedule and
to legal review: where such a schedule exists it governs and these numbers
change to match it. They are not values mandated by NIST or by any other source
read for this decision, and adopting them certifies nothing about any
particular customer's obligations. What published practice supplied was a
procedure — follow the retention requirements that apply, and otherwise decide
by a privacy and security risk assessment — not the values.

The anchor is deliberate. A period measured from creation would be the "N years
after ingest" schedule ADR-0080 rejects; a period measured from the moment the
row stopped being able to authorize anything is a triggering event. It also
handles the case the code actually produces: `deprovision_principal` revokes
every unrevoked session without checking expiry, so a `revoked_at` later than
`expires_at` exists, and reading the *earlier* of the two stops an offboarding
from extending the life of a session that had already died.

### 3. Why the three differ

All three are judgements, so the question is not which one a standard settles —
none of them does — but what each row is still *for* once it stops working, and
how much personal data that remaining use justifies keeping.

**A sign-in link: 24 hours, the shortest, because the row is dead
authentication material that answers one question.** It is usable for fifteen
minutes (`SIGN_IN_TOKEN_TTL`), once, and only as a hash. After that the row
answers exactly one question — "was the link I was sent spent, or did it run
out?" — which is worth answering while somebody is still asking about a sign-in
that did not work, and worth much less the following day. Twenty-four hours
covers that immediate support investigation without retaining dead
authentication material for a week by default. NIST SP 800-63A-4 §3.8 puts a
twenty-four hour ceiling on how long an emailed confirmation code may still be
*redeemed*; that is a validity limit, not a retention period, and the
coincidence of numbers is not a citation for this one.

**A session record: 7 days, because a dead session supports recovery and
short-term diagnosis.** Two facts set it. First, `access.expired_web_session`
deliberately reads a session that expired and was not revoked, so #844 can hand
a coordinator back what they had typed. That reader needs the row only while a
held draft could still exist, and `form_drafts.DRAFT_TTL` is thirty minutes, so
seven days — 10,080 minutes — clears the draft-recovery interval by a factor of
336, comfortably beyond it, while a period measured in minutes would break #844.
Second, after that the row's only remaining use is reconstructing an incident
within the week it happened, which is the span over which anyone is still
asking. No source bears on this number; it is a judgement about those two uses.

**A sign-in attempt: 180 days, the longest, because it is the only record that
an attempt happened.** A successful sign-in is in `audit_log` as `SIGN_IN` and
survives this sweep untouched. An attempt from an address bound to nobody, or
one that never reached consumption, is recorded **only** in `sign_in_attempts`.
Deleting a dead session or a spent link destroys no history; deleting an attempt
row destroys the only evidence that somebody tried and did not get in — which is
precisely the pattern an access review or an incident investigation looks for.
So what this relation needs is an investigation history rather than an
operational window, and 180 days is a bounded one: half a year of failed-attempt
history, above CIS Safeguard 8.10's ninety-day minimum recommendation and inside
the span CNIL's logging guidance generally discusses, without adopting a year of
personal data to satisfy a standard that has not been shown to govern these rows.

The operational need is far shorter than any of this: the backoff counter stops
mattering after `ATTEMPT_WINDOW`, fifteen minutes, and NIST SP 800-63B-4 §3.2.2
says a verifier SHOULD reset the retry count on successful authentication.
Everything past fifteen minutes is retained as a security record, not as a
throttle.

### 4. What is settled here, and what would change it

The three numbers were approved as operating defaults rather than derived from a
source, and this section records that difference so a later reader does not
mistake one for the other.

- **None of the three is fixed by a citation.** An earlier draft of this ADR
  presented `sign_in_attempts` = 365 days as "the widest compliant envelope
  absent a customer contract". That was wrong in kind and not only in value: no
  source read for this decision is established as governing these relations, so
  there is no envelope for a number to be widest inside, and a wider one would
  not be safer. The research note carried the same error and has been corrected
  with it.
- **A customer records schedule or legal review supersedes all three.** Where a
  customer contract or an agency records schedule states a period for
  authentication records, it governs and these numbers change to match it. None
  has been read, so these defaults stand in the meantime.
- **Each is one constant and one table row.** Any of the three can be changed by
  editing the constant in `corridor.sign_in_retention` and the matching row
  above, and bumping `POLICY_VERSION` beside it so a receipt written under the
  old numbers still says which numbers produced its counts. Changing one does
  not disturb the other two, and the reasons above are written so that one can
  be reconsidered without rereading the rest.

### 5. A hold stops this pass, for the reason it stops an unattributable object deletion

An active `RetentionHold` anywhere — on any project — refuses this pass, and it
deletes nothing. This is not a new rule: `retention.permit_unreferenced_deletion`
already decided what a project hold means for a deletion that cannot be
attributed to a project, and decided "any active hold anywhere refuses it",
because the thing being deleted might be the held project's. A sign-in record is
in exactly that position — the person whose session it was may be the person a
litigation hold is about — so it takes the same answer. ADR-0080's "legal holds
suspend every deletion path" therefore remains literally true with this pass
added.

**The hold is read inside each delete, not once before them all.** A read that
finds no hold followed by a delete leaves the window between the two, and a hold
committed in that window is honoured by neither statement. So the predicate is
part of the `DELETE`: at the READ COMMITTED isolation this application runs
under, each statement takes its own snapshot, so a hold committed at any point
before a given delete begins is seen by it and that delete removes nothing. The
separate read at the top of the pass is kept only for what it is good for, which
is telling the receipt that a hold is the reason nothing happened. What this
does not do, because it cannot, is put back rows a delete had already removed
when the hold was placed; those rows were past their stated period and no hold
existed at the moment they went.

**The `NOT EXISTS` predicate is not complete serialization, so the ordering is
a protocol.** `retention.take_hold_ordering_lock` is one environment-level
PostgreSQL advisory lock, held by the acquiring transaction until it commits.
`retention.place_hold` takes it before it does anything else, and so does each
deletion batch, which reads hold state only *after* it holds it. That closes
the window between a hold's `INSERT` and its `COMMIT` in which a batch could
otherwise read the holds table and see nothing. The lock is scoped to the
database, which is scoped to the customer environment, and it needs no schema
change and no grant: `corridor_worker`, which is the role both sides run under,
can already execute `pg_advisory_xact_lock`.

Three statements are then precise rather than hopeful:

- **Hold activation wins first — the deletion batch refuses.** The batch waits
  at the boundary, reads the committed hold, and removes nothing.
- **A deletion batch wins first — it may finish before the hold becomes
  enforced.** Hold activation waits for that batch rather than silently
  overlapping it, and the hold then governs every batch that begins after it.
  This is the accepted outcome, not a defect.
- **The acknowledgement does not claim enforcement it has not won.**
  `place_hold` returns only once it holds the boundary, so an acknowledged hold
  means every deletion batch beginning after that transaction commits is
  refused. It does not mean a running batch stopped, and **no path anywhere
  recovers a row a batch already deleted.**

**What the boundary does not order.** A hold written by something that does not
take the lock is outside the protocol and falls back to exactly the `NOT
EXISTS` guarantee above. `place_hold` is the only application path that
activates a hold and it takes the lock, and `corridor_web` holds no privilege
on `retention_holds` at all since #680 revoked them, so what is left outside
the protocol is someone connecting as `corridor_worker` or as the owner and
issuing `INSERT` by hand. The Class B deletion paths do not take the lock
either; they have their own manifest recheck, and extending the boundary to
them was not decided here.

**Batch length is part of the contract**, because hold activation now waits for
a running batch. Today the pass takes the boundary once and issues one `DELETE`
per relation with no row limit, so the batch a hold waits behind is the whole
pass over three relations. That is acceptable while they are small — the same
fact the daily cadence rests on — and it is a further reason growth is answered
with a time index rather than a rarer schedule. If the pass stops being short,
the deletes have to be bounded first: row-limited, each committing its own
transaction so the boundary is released between them.

The cost, stated rather than hidden: a hold on one project suspends sign-in
record expiry for the whole customer environment, which over-retains. That is
the correct direction for a hold, which exists to override minimisation, and it
is bounded by holds being rare and lifted by a separate attributable act.

## Considered and rejected

- **Adding the three relations to the Class B apparatus.** Rejected in the
  Context above. Every load-bearing part of it — the project scope, the
  completion-shaped anchor, the digest manifest, the null-the-content execution
  — is either absent or meaningless for a credential row.
- **Deleting a session or a link the moment it stops working.** It is what
  minimisation argues for and it is wrong twice over: it would break #844's
  draft hand-back, which reads exactly that row, and it would leave an incident
  with no trace of a session that existed an hour ago.
- **Keeping attempts only as long as the throttle needs them (15 minutes).**
  That treats the relation as only a counter. It is also the product's only
  record of an access attempt that did not succeed, and PCI, CIS and CNIL all
  measure that in months.
- **Sweeping on the sign-in path, the way #844 discards drafts where they are
  kept.** Attractive, because growth would then be self-limiting by
  construction. Rejected: the sweep would run on an unauthenticated request
  path, and a delete over `sign_in_attempts` by time alone cannot use
  `ix_sign_in_attempt_scope`, so every unauthenticated attempt would pay for a
  scan. Worse, an attacker who never succeeds never triggers the pass that
  would remove their own rows.
- **Extending Due Work's scope model to carry this schedule.** Declined.
  Due Work is per project by schema — `due_work_schedules.project_id` and
  `due_work_receipts.project_id` are `NOT NULL` foreign keys, and
  `DueWorkScheduling.project_id` is required — and that is an invariant its
  handlers and receipts are built on, not an incidental column shape. Sign-in
  record expiry is one customer-environment-wide maintenance operation, so
  declaring one schedule per project would run the same global delete N times.
  The way through is *not* to make those foreign keys nullable as a side effect
  of retention work: if Due Work should ever carry environment-scoped work, that
  is a deliberate invariant change across every handler and receipt, scoped and
  decided on its own. What this pass needs instead is an environment-scoped
  scheduled invocation of the command, specified under Consequences below.
- **Making this an operator command and stopping there.** Rejected as the
  answer. #488 already established what happens to a retention boundary whose
  only caller is a command: "intermediary content expired when somebody
  remembered to run a command". The command exists as the recovery entry point,
  as `retention_cli` is for Class B, but it is not the mechanism, and this ADR
  does not pretend otherwise.

## Consequences

**What is built.** `corridor.sign_in_retention` holds the three periods and one
pass, `sweep_sign_in_records`, which deletes what is past its period from the
three relations and nothing else under the hold predicate, writes one
`audit_log` entry recording what it removed, and returns the same receipt
whether it deleted, refused or found nothing due: policy version, executing
identity, observation time, the three cutoffs, the count removed per relation,
the outcome and any refusal — counts and timestamps only, nothing copied out of
a row before it went. `tests/test_sign_in_retention.py` proves an expired row is
actually removed, that the period runs from revocation rather than a later
expiry, that an idle pass writes no domain audit event, and the four boundaries
this decision turns on: an active credential still authorizes after a pass, a
draft's permitted recovery window survives one, a credential the pass removed
stays invalid rather than becoming ambiguous, and a hold another transaction
commits after the pass has begun is honoured by the delete itself. Two further
tests take the ordering boundary from both sides in committed transactions:
hold activation holding it makes the batch refuse, and a batch holding it
finishes and keeps its deletions once the hold that waited behind it commits.
`make retention ARGS="expire-sign-in-records"` runs it, with an optional
`--as-of <iso>` for reproducing a past pass.

**What is not built: the recurring trigger, which is a deployment task.**
Recorded in the frontmatter's `migration` key rather than half-implemented, and
specified here concretely enough to act on.

- **What it runs.** `python -m corridor.retention_cli expire-sign-in-records`,
  with no arguments. `--as-of` defaults to the moment the process starts,
  because a fixed command line in a task definition cannot compute a timestamp
  per run; it stays available for an operator reproducing a past pass.
- **Where it runs.** The batch Fargate task definition
  `CorridorApplicationStack` already publishes as `BatchTaskDefinitionArn`. It
  runs the same image under the `corridor_worker` credentials this pass needs,
  and `corridor_worker` already holds `DELETE` on the three relations and
  `INSERT` on `audit_log`, so the schedule needs no grant change and no new
  role.
- **How many.** Exactly one schedule per customer environment, explicitly scoped
  to that environment's database. Never one per project, and never a placeholder
  project: the relations are customer-wide, so a per-project schedule would run
  the same global delete once per project.
- **How often.** Daily. None of the three relations carries a time-only index —
  `sign_in_attempts` is indexed on `(scope_kind, scope_value, occurred_at)` for
  the throttle, and the other two on their hashes — so each pass scans all three.
  That is cheap while they are small and is the reason not to run it hourly. The
  interval is the deletion lag section 2 states as its own term: a row becomes
  *eligible* when its period ends and goes at the next pass, so a daily schedule
  removes a spent link within a further twenty-four hours of its twenty-four
  hours ending, and neither number is a maximum row lifetime. If these relations
  ever grow enough for the scan to cost something, the answer is a time index,
  which is a schema change, not a rarer schedule — added when the deployed
  pass's query plan or observed cost calls for one, not before it has run.
- **What success and failure look like.** Success is exit status zero and one
  printed receipt. A hold is not a failure: the receipt reads `"outcome":
  "refused"` with `"refusal": "hold_active"`, the exit status is still zero, and
  the schedule should not alarm on it, because a hold is a deliberate state
  lifted by a separate attributable act. A failure is a non-zero exit with the
  traceback in the same log. A scheduled window with no receipt at all is the
  signal that the job did not run.
- **What makes an idle run observable.** Every pass prints its receipt, so the
  scheduled invocation's own run record shows that the job ran and what it
  concluded even when it removed nothing. Only a pass that deleted something
  also writes the `audit_log` entry: a domain audit event per idle sweep would
  be a daily row saying nothing happened, and the run record is the right place
  for "it ran".

**#907's "nothing ever runs the purge" belongs to that deployment task.** A
manually runnable purge does not resolve it, so the criterion transfers rather
than closing with this change — the defect #488 named for Class B is the same
one, and it is not resolved by the command existing.

**The receipt's home.** `audit_log` is an acceptable interim home and stays.
It takes the entry without a schema change: `access.CUSTOMER_WIDE_RELATIONS`
already classifies it as "the append-only attribution ledger of the whole
customer database", it already carries sign-in, sign-out and offboarding,
`corridor_worker` already holds `INSERT` on it, and `UNBOUND_IDENTITY` already
exists for an act that names no identity row. It is a fair home and not the
best one — the entry names no subject, and a sweep is not a ledger mutation —
so `sign_in_record_expiry_receipts` in `models/operations.py` remains the better
one for a change that holds the schema slot: `id` bigserial, `public_id`
varchar(64) unique, `as_of` timestamptz, the three periods, the three deleted
counts, a bounded `refusal` string, and `created_at`; `SELECT, INSERT` to
`corridor_worker` and nothing to `corridor_web`, and no partition policy because
it carries no personal data. It is not a prerequisite for this pass, and it was
not built here: this change held no schema slot.

**The fourth per-person store, and whether this covers it.** #844 holds what a
coordinator had typed across an expired session. It is deliberately not a
relation: the web process holds it, bounded by a thirty-minute expiry and a
fixed count of 256, and it is discarded where it is kept rather than by a job
that may never run. Nothing here changes that, and nothing here should: a store
with no rows needs no sweep, and #844's own docstring is right that enforcing
the bound at the point of keeping is stronger than enforcing it from a
schedule.

If it ever becomes a relation — the specification is the last paragraph of
`form_drafts`: "a table of its own, with a person, a customer, a project, an
occurrence, the input and an expiry" — this class covers it and this pass would
extend to it, because a draft is the same kind of thing these three are:
permitted input that is never a record, keyed to a person, with an expiry and
no authority. Three things would have to be true first. Its period would have
to be stated here beside the other three, and it would be the shortest of them,
because a draft that is not handed back within its TTL has no further use at
all. The anchor would be the row's expiry, the same "moment it stopped being
usable" the three above use. And the sweep's closed relation list —
`SWEPT_RELATIONS` — would have to name it, which is the deliberate gate that
stops this pass reaching anything it was not given. None of that is built
speculatively; the relation does not exist.

**What this ADR does not touch.** `SESSION_TTL`, `SIGN_IN_TOKEN_TTL`,
`ATTEMPT_WINDOW` and the three attempt limits are unchanged; how long a session
*works* is a different question from how long its record is *kept*, and #907
asked only the second. `audit_log` itself has no stated retention and does not
get one here — it is the attribution ledger, it is read by the identity export,
and ADR-0080's posture on record history governs it.

**What remains open.** The research note records one design observation that
#907 did not ask for and this ADR does not settle: `sign_in_attempts` is doing
two jobs, a fifteen-minute throttle counter and a six-month security record, over
the same rows keyed by an email address or a client address. CNIL ¶22 and the
OWASP Logging Cheat Sheet both point toward pseudonymizing the scope once the
counter has reset. Neither prescribes it, and separating the two is a change to
the sign-in path rather than to its retention.
