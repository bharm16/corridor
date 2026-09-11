"""Let these tests read the repository's dependency-free modules.

`infra/pyproject.toml` deliberately excludes the application's dependencies,
so nothing here can import `corridor` -- which is why the assertions below
scrape `src/corridor/config.py` as text rather than importing `Settings`. That
constraint does not extend to `scripts/`, whose `__init__` forbids anything
outside the standard library: `scripts.container_entrypoint` is exactly the
module that declares what the container consumes and what shape a deployment
identifier may take, and reading it is how these tests stop retyping both.

The insert is needed only under `make test-infra`, which runs pytest from
`infra/`. `make check` runs `test_workflow_ordering.py` from the repository
root, where the root project already puts `scripts` on the path.
"""

from __future__ import annotations

import pathlib
import sys

REPOSITORY_ROOT = pathlib.Path(__file__).resolve().parents[2]

if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))
