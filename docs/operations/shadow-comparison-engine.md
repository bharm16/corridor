# Frozen shadow comparison engine

The #499 engine compares explicit subject/field values from frozen native
Proposed Deltas with a customer's frozen working reference. It writes no
accepted record. `freeze_predictions` accepts no successor input; the later
`compare_revisions` refuses a different project or a successor whose supplied
receipt time is not later than the freeze.

The reference adapter supplies already normalized typed values, exact source
digests, stable subject identities, native delta IDs, source references and
receipt times. Whole-subject deltas may supply several field predictions under
one native ID; duplicate delta/field identities and no-change predictions
refuse. Input containers are defensively frozen. This is a comparison engine,
not a source-normalization step: an absent field differs from an explicit null;
removal predictions use `present: false` and a null value. It is
not an independent verifier of the adapter's source identity or chronology.
The receipted #564 runtime and registered-source export integration still have
to supply those guarantees before a real customer run can satisfy #499.

```bash
make shadow-comparison ARGS="freeze prediction-input.json"
make shadow-comparison ARGS="compare --freeze-sha256 <returned-sha256> --successor successor.json --reference-dataset <working-reference-id>"
```

The first command preserves a content-addressed prediction artifact. The second
loads and verifies that exact artifact before creating an Extraction
Measurement artifact. `FrozenRevision.payload`, `Prediction.payload`, and
`ComparisonPolicy.payload` define the JSON fields; all times are explicit ISO
timestamps with timezones. Reference records are `values[subject][field]`.

Each field question is matched, Corridor-only, customer-only, or ambiguous.
Conflicting predictions and disagreement with a changed customer field remain
ambiguous. Customer-only differences retain an unreviewed cause, and
Corridor-only findings retain an unreviewed handling outcome. These are inputs
to human reconciliation, not automatic declarations that a customer or Corridor
was wrong. Every material stratum prints its own matched numerator and
prediction/reference denominators. Ambiguity withholds definitive rates;
otherwise rates mean agreement with the working reference, never independent
semantic accuracy. Each exact match reports elapsed days before arrival.

`select_source_sample` implements the predeclared partner-week rule: all
eligible arrivals up to 20, otherwise 20 without replacement, from a sorted
population and declared seed. Recorded Verbal Statements remain excluded from
the initial population and are listed explicitly. `assess_material_sample`
requires one independent named reviewer per inspected arrival and the explicit
collection of actual resolvers (empty for an entirely missed arrival), exact case
identities, and causes for confirmed misses. Insufficient inspected arrivals
or material cases withhold the miss rate. A confirmed material automatic false
write fails its policy class even when sample evidence is insufficient.

#499 remains open for integration with the isolated shadow runtime, retained
human reconciliation, and actual partner comparison evidence. No synthetic
artifact produced by this engine establishes a pilot outcome.
