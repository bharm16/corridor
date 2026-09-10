# How a rehearsal harness proves what it claims

Corridor has several harnesses that drive the real application and then publish
a receipt: the SH 99 Admission acceptance and its shared-operation seal, the
Event Admission acceptance, the SH 99 coordinator rehearsal, the M8 acceptance
gate, and Product Proving. They kept answering two questions differently, which
made their receipts look comparable when they were not. This note settles both
answers. It changes no requirement in
[ADR-0046](../adr/0046-product-proving-runs-start-from-bounded-raw-documents.md);
it says how those requirements are proved.

## Did the practitioner really use the frontend?

**Every claim about the record is re-read from the database after the frontend
has committed.** This is the posture
`src/corridor/product_proving_frontend_capture.py` already takes, and it is now
the rule for every harness. The browser or test client is an actor, not a
witness: it may report timings, the controls it saw, usability friction, and
digests of what it retained, but a receipt never states a Candidate outcome, an
append-only history entry, a Report byte, or a release binding on the strength
of parsed HTML. It reads those back from the live Project Record.

A rehearsal may still parse a page, and two kinds of claim genuinely require it:

- **Navigation and input.** The practitioner has to find the work from the Work
  List, follow visible links, fill a form using the labels and browser-carried
  values the page offered, and retrieve a file through the control the page
  showed. A rehearsal that reached a screen by constructing a URL from a
  database identity has not proved the practitioner could get there.
- **What the page did or did not display.** "The release history shows the
  coordinator's display name and never the raw principal" and "the form does not
  expose an artifact identity" are claims about rendering. No database read can
  answer them.

Anything else — the outcome, the identity, the history, the bytes — is read from
the database. When a parsed value and a database value must agree, the receipt
binds them: parse to navigate, then re-read to claim.

## Which substrate does a receipt live in?

Three substrates are in use. A new harness picks by who has to read the receipt
back, not by which one is nearest:

- **A digest-pinned bundle** (`m8_acceptance_bundle`, published once through
  `m8_acceptance_publication`) for evidence a person keeps and later hands to
  someone else. The bundle is immutable, its manifest digest is the identity, and
  it can be verified without opening PostgreSQL. Use it for an acceptance or
  rehearsal result that outlives the database it ran against.
- **A database receipt row** for a fact the product itself must read back and
  act on. The Event Admission acceptance receipt and its activation history are
  the example: policy status, permitted operations, and the current-receipt check
  are ordinary application reads, so the receipt has to be a row.
- **Checked-in JSON under `docs/operations/`** only for a report that is itself
  documentation — a measurement or timing report a human reads in the
  repository, with no runtime reader. It is never the substrate for a claim the
  product enforces, and never a substitute for a bundle a person is meant to
  keep.

A receipt that is read by the product *and* handed to a person is a row plus a
bundle that quotes it, not one substrate stretched to do both jobs.

## Keep the harness bodies apart

`docs/operations/CONTEXT.md` defines Product Test Run, Rehearsal Input Manifest,
Extraction Measurement and Processing Scope as different things with different
claim boundaries. The shared clone mechanics live in
`src/corridor/rehearsal_environment.py`: `rehearse_on_disposable_clone` owns
capturing the pinned source, provisioning a disposable clone, refusing a clone
that is not at the checkout's migration head, restoring, refusing a clone that
does not match the pinned source, running one operation, and returning the
before/after pair. Each harness supplies the operation, the state reader, and
its own `claim_boundary`, and keeps its own assertions and receipt. The seam
carries the claim boundary so that one harness can never publish another's.
