"""Repository commands, importable so their tests and each other can reach them.

`scripts/*.py` are entry points that `make` targets and the workflows invoke by
path. Before this file existed they could reach one another only by inserting
the repository root into `sys.path` and relying on an implicit namespace
package, by `runpy.run_path`, or by `importlib.util.spec_from_file_location`,
and every test of a script re-spelled one of those three. A regular package
gives every caller the same `from scripts... import` line. Nothing here may
import outside the standard library: `classify_ci_change.py` and
`ci_feedback.py` run on the bare `python3` of the classify and summary jobs.
"""
