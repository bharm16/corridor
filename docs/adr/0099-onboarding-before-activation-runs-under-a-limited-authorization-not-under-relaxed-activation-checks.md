---
status: accepted
domain: operations
scope: current product
amends:
  - ADR-0079
  - ADR-0083
migration: the limited onboarding authorization, its control-plane record, the retained proof that it was valid when an onboarding act committed, and the granted adoption command do not exist; #827 builds them and specifies how a withdrawn or stale authorization is observed, and until it does no production caller of the adoption or preview functions exists in `src/`, `workers/` or `scripts/`.
---

# Onboarding before activation runs under a limited authorization, not under relaxed activation checks

**Amends ADR-0079 and ADR-0083.** It adds one clause to ADR-0079 and two to
ADR-0083, and moves nothing else. ADR-0079's customer-isolation rule — one
database and one object-storage namespace per customer, with multiple projects
inside them — now also says under what authority a customer environment may be
reached before its activation exists. ADR-0083's correction of that rule — that
the project stays an authorization and data-partition boundary inside the
customer database, enforced by the application and its roles — now also says
that a pre-activation authorization is one of those roles' boundaries, bound to
a named project rather than to the environment as a whole. ADR-0083's
control-plane clause — that cross-customer control-plane data lives in a
separate control-plane database and object namespace, never inside a customer
environment — now also carries the limited onboarding authorization itself.
Every other clause of both ADRs stands unchanged: the topology, the
run-provenance fields, the storage interface, the observability list, and
ADR-0083's corrections to ADR-0075, ADR-0076, ADR-0078, ADR-0080 and ADR-0081.

The decision was recorded by the maintainer on 2026-09-10 on issue #826, after
the [customer-journey audit](../research/customer-journey-audit-2026-09-10.md)
section "Uploading a document is not adopting a baseline", and sharpened by his
review of this draft on 2026-09-11. #827 implements it; this ADR ships no code.

## Context

Adopting a baseline is the first thing a coordinator does on a provisioned
project, and it must happen before authoritative processing is activated. The
three rules that meet at that moment currently form a loop.

Customer processing requires a current activation receipt. In
`src/corridor/activation_runtime.py` (at 41c895d2, `current_activation`,
lines 40-67) a customer deployment without a readable, unstale,
matching activation refuses with "customer processing requires a current
activation receipt". Activation itself expects an adopted baseline: #535's
contract requires that the project "has completed #509 and is in #520
adopted-baseline mode". And the adoption command that would close that circle
enters a bootstrap context reserved for the schema owner —
`adopt_baseline` in `src/corridor/baseline_adoption.py` (lines 385-399)
validates its named human principal and then runs inside
`owner_source_bootstrap`, which in customer and shadow deployments refuses
anything but the schema owner on that exact session
(`activation_runtime.py` lines 126-134, "pre-activation baseline bootstrap
requires the separate schema owner"). The web role is not that owner.

The audit found the consequence in the product rather than in the database:
there is no user-facing route or screen that presents the coordinator preview
and invokes `adopt_baseline`. Re-verified in this worktree, the position is
the stronger one the audit reported — `adopt_baseline` and
`preview_baseline_adoption` have **no production caller anywhere** in `src/`,
`workers/` or `scripts/`. The only references outside their own module are the
`ADOPT_BASELINE` audit action name, the command-type string in the
`baseline_record` source-append migration, and tests.

Granting the adoption command to the web role would address only the last of
the three database calls. It would leave the onboarding-versus-activation loop
exactly where it is, because customer processing would still demand an
activation receipt that cannot yet exist, and it would answer the authority
question by widening a credential rather than by naming what onboarding is
allowed to do.

## Decision

A **limited onboarding authorization** exists. It is the authority under which
the work that must precede authoritative activation is done, and it is neither
an activation nor an exemption from one.

### Who issues it, and where it is held

**The control plane is authoritative for limited onboarding authorization.** A
restricted operations/deployment actor issues it based on the recorded
customer-data authorization. A project coordinator cannot self-authorize
customer-data processing merely by belonging to the project.

Membership and customer-data authorization are different grants held by
different parties. A designation recorded on a project membership says what a
person may do inside a customer database; it does not say that the customer has
permitted that database to be processed at all. Only the second is the
permission onboarding needs, and only the party that recorded it can extend it.

### What the control-plane record binds

The control-plane record binds: customer, environment, project, permitted
operations, source scope, authorization evidence, issue time, expiry, issuing
actor, version. **It contains identifiers and digests — not the customer's
workbook contents.**

The customer database retains what it owns: the validated preview, mapping,
answers to material questions, and the adoption receipt.

That split is the boundary ADR-0083 already draws (#656). An authorization to
process a named customer's data is cross-customer operations state and belongs
beside the customer registry and the hold state; the preview a coordinator
approved is the customer's own material and belongs in the environment whose
record it adopts. Holding the authorization in the control plane also keeps it
legible after the customer environment is disposed of, and keeps the customer's
workbook out of a store that spans customers.

An authorization is not a standing capability of a role or a deployment; it
names one project in one customer environment, the material it covers, and the
operations it permits over that material.

### The source scope binds in two stages

The authorization cannot bind exact source bytes before those bytes exist in
Corridor, because a person cannot upload until the unknown file's digest has
already been authorized. The binding happens in two stages:

1. A recorded customer/project/source-scope authorization permits **bounded
   receipt and safe staging**.
2. Once the bytes are received and hashed, the **exact source identity is
   bound** to the permission for compatibility processing, preview and
   adoption.

This is **not** permission to process arbitrary uploads: storage, compatibility
inspection, semantic capture and adoption remain separately bounded. The first
stage admits only material inside the recorded source scope and does nothing
with it but hold it safely. Every operation after the hash is permitted against
the exact source identity that was bound, never against the scope that admitted
it.

### What it allows

Only the onboarding activities that must precede authoritative activation:

- reaching the provisioned project and its onboarding status;
- bounded receipt and safe staging of material inside the authorized source
  scope;
- inspecting and processing that material once its exact source identity is
  bound;
- operations compatibility and mapping work;
- reviewing material baseline questions;
- recording the coordinator's adoption approval.

### What it does not allow

It must not enable general customer-source processing, live connector polling,
ordinary accepted-record modification, or release authorization.

**It is not "skip the activation checks while the project is unadopted".** The
checks are not relaxed, suspended, or made conditional on a project's adoption
state. The authorization permits a named, bounded set of operations; every
operation outside that set is refused for exactly the reasons it is refused
today.

### The adoption act stays narrow

The authenticated coordinator approves a server-retained, validated preview.
Before committing, PostgreSQL verifies:

- the current designation;
- the project;
- the preview identity;
- the source and mapping identities;
- the adoption preconditions.

All five are verified in the database, in the committing transaction, not in
the caller that assembled the request. "Designation" is the repository's
existing word for the explicit write authorities recorded on a project
membership (`src/corridor/access.py`, #331); no new term is coined here.

The authorization itself is verified the same way. The database adoption
command must verify a **server-trusted, project-bound authorization**, not
accept a browser-supplied Boolean saying onboarding is permitted. A flag the
request carries is a claim about authority made by the party seeking it; what
the command may act on is the authorization the server holds, for the project
the command names.

### The web role's capability

The web role receives **only the required command capability**. It never
receives schema-owner membership, and it never receives general source or
record write permission. The command is `SECURITY DEFINER` under a constrained
owner with a fixed `search_path` and explicit execution grants, never default
`PUBLIC` execute — as #492 already requires of every other command. This ADR
states no new rule about command authority; it refuses the shortcut of
exempting this one command from the existing one.

### Separation from the shadow runtime

One-time operations bootstrap remains separate from the shadow-processing
runtime. **A shadow worker never holds owner credentials.** The bootstrap that
establishes a baseline and the runtime that processes shadow sources are two
credentials and two acts, and the existence of the onboarding authorization
does not merge them.

### Expiry, withdrawal, and what activation asks for

The onboarding authorization is temporary; the record it helps establish is
not. A legitimate adoption must not become unusable history merely because the
temporary onboarding authorization expired afterwards. What is required is
therefore validity at the moment of the act, and retained proof of it
afterwards:

- the authorization valid **when the permitted onboarding act commits**;
- retained proof of that validity;
- current customer authorization and the normal activation prerequisites **at
  activation**;
- no continuing onboarding authority after it expires, is withdrawn, or is
  consumed.

**Withdrawal must stop future onboarding writes.** #827 must specify how the
customer-side authorization becomes stale or revoked, and fail closed when its
validity cannot be established.

What this ADR does not claim is immediate cross-database revocation. The
authorization is authoritative in the control plane and enforced in the
customer database, and nothing here implements the propagation or the
verification that would make a withdrawal effective in both at the same
instant. What is guaranteed is narrower, and is what the requirements above
actually give: an onboarding act commits only while a validity the customer
environment can establish still holds, and it refuses when that validity cannot
be established. How promptly a withdrawal reaches that check, and by what
propagation or verification, is #827's to specify and to state plainly rather
than to assume.

**Withdrawal does not erase previously recorded adoption.** Any later
correction follows an explicit supported act.

## Considered options

**Grant the adoption command to the web role and stop there.** Rejected. It
addresses only the last database call. Customer processing would still refuse
for want of an activation receipt that cannot yet exist, so the loop described
above is untouched, and the authority question is answered by widening a
credential instead of by stating what onboarding may do.

**Let the activation checks stand down while a project is unadopted.**
Rejected, and named explicitly above as what this authorization is not. A
check that switches itself off on a state the customer environment reports is
not a boundary; it is a mode. It would also make "unadopted" the most
permissive state a live customer environment can be in, which is the reverse
of what the state means.

**Treat the coordinator's project membership as the authorization.** Rejected.
It would let the party who wants customer data processed be the party who
permits it. Membership is recorded inside the customer database and says what a
designated person may do there; the customer's permission to process that data
at all is recorded elsewhere, by someone else, and is what onboarding actually
needs.

**Bind the exact source bytes in the authorization before they are received.**
Rejected as circular. An authorization that named the digest of the file a
person is about to upload could never be issued: the digest does not exist
until the upload does. The two-stage binding above keeps the property that made
the byte-exact version attractive — nothing is processed under a permission
that did not name it — without requiring the impossible order.

**Require a current, unexpired onboarding authorization receipt at
activation.** Rejected. The onboarding authorization is deliberately temporary,
so a rule of that shape would turn every legitimate adoption into unusable
history as soon as the authorization that permitted it lapsed, and would create
a standing reason to keep temporary authority alive. Validity is a property of
the act when it commits, not of the moment someone later reads the result.

**Call the existing owner bootstrap from a web handler with elevated database
credentials.** Rejected. The audit's finding is that exposing the adoption
code safely "requires a deliberately designed request/authority handoff, not
simply calling it from a web handler with elevated database credentials". A
web process holding schema-owner credentials for one command holds them for
every statement it can reach, which is precisely the arrangement #492
replaced.

**Have operations run the adoption on the coordinator's behalf.** Rejected.
Operations-assisted onboarding is acceptable, and operations compatibility and
mapping work is one of the permitted activities above. But the audit draws the
line one step earlier than the adoption: "Having operations prepare the mapping
is different from having an engineer impersonate the coordinator to make the
adoption decision." The adoption approval is the coordinator's attributable
act; the authorization exists so that act can be made from the coordinator's
own authenticated session.

## Consequences

- ADR-0079 and ADR-0083 record `amended_by: ADR-0099`. Their bodies are
  unchanged; the clauses that move are the three named at the top of this one.
- Activation does not require an unexpired onboarding authorization. It
  requires the retained proof that the authorization was valid when the
  onboarding act committed, together with current customer authorization and
  the normal activation prerequisites at activation itself: the adopted
  baseline #535's contract asks for is reached under that authorization, not by
  relaxing the checks that stand between the two. The
  [shadow and activation tooling contract](../operations/shadow-and-activation-tooling.md)
  carries that prerequisite alongside the other live evidence requirements
  that remain open on #535, and #535's own contract records the same reference
  to this ADR.
- #827 implements the control-plane authorization record, its issuance and
  withdrawal by the restricted operations actor, the retained proof of
  validity, the granted adoption command, and the route that reaches it, and it
  specifies how a customer-side authorization becomes stale or revoked. Nothing
  ships in this ADR.
- #509's adoption importer, #520's one-way operating-mode transition, and
  #492's command-authority rules are unchanged. This decision adds the
  authority under which #509's command can be reached from a coordinator's
  session; it changes neither what that command does nor what it may write.
- What remains unresolved: the strength of revocation. How promptly a
  withdrawal recorded in the control plane must be observed by the customer
  database, and by what propagation or verification, is #827's to decide and
  to state; this ADR requires only that a write fail closed when validity
  cannot be established, and forbids claiming more than is built. Whether
  "limited onboarding authorization" becomes a customer-visible label, rather
  than the internal technical name it is here, is also undecided and would need
  the terminology procedure in
  [the domain guide](../agents/domain.md#research-before-proposing-terminology).
