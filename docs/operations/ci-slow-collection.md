# Balance slow-suite imports without changing test selection

Required run 34381546750 passed every behavior and migration test but measured
193.13 seconds against the unchanged gate budget. Slow shards were assigned
189, 3 and 92 files; only 5, 2 and 4 files respectively recorded nonzero test
execution time. Their commands took 131.43, 83.05 and 128.44 seconds. Each xdist
worker still imports all assigned files before pytest applies the marker filter.
A zero timing therefore cannot mean that a file costs nothing to collect.

The slow partition now uses a minimum scheduling weight of 0.5 seconds per file,
plus the existing 0.05-second collection term. This is a conservative scheduling
floor, not a claim that each import takes that long. Replaying the exact run's
input weights gives 109, 74 and 101 assigned files instead of 189, 3 and 92. The
six expensive files retain their existing shards; simulated two-worker execution
loads from that run's measured file work remain 75.74, 72.75 and 84.94 seconds.
A larger 1-second floor only improves the file split to 102, 84 and 98.

No file is pruned by its timing, source text or inferred markers. New and dynamic
slow cases still reach pytest. The marker expression, three slow runners, two
workers, fixture-preserving scheduler, complete partition checks, actual timing
receipts and feedback budgets remain unchanged. Ordinary and migration partition
behavior is unchanged. Required CI must measure whether the lighter import load
reduces elapsed time; these partition calculations do not promise a passing gate.

This run also delayed one ordinary hosted runner roughly 37 seconds before its
first action. The wheel cache itself restored approximately 167 MiB successfully.
That provisioning delay is distinct from package installation and collection.
