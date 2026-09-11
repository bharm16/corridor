"""The CI test gate's shared definitions, in one importable place.

The gate was one module spread across six scripts that could not import one
another. The shard receipt was written by `run_test_gate.py` and validated by
a key set in `test_feedback.py`; three JUnit readers computed per-file seconds
by three rules, so `make test-timing --write` and the CI receipt disagreed
about the same run; the test-file enumeration, the strict JSON reader and the
GitHub job identity were each spelled twice or more. Tests therefore
hand-built the receipt dictionary, and a writer change reached the validator
only in CI.

Each module here owns one of those definitions: `receipt` the shard receipt
that writes and validates itself, `junit` the one JUnit-to-per-file-seconds
rule, `partition` the test-file enumeration and the bootstrap weights it is
balanced by, `evidence` the strict JSON reader and value checks, `contract`
the workflow's job names, output slots and marker lines, `broad_run` the
local full-suite authorization the wrapper and the collector share, and
`feedback` the aggregation and assessment built on them. Standard library
only: the summary job imports this package on a bare `python3`.
"""
