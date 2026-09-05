"""One JSON-in/JSON-out adapter subprocess."""

from __future__ import annotations

import json
import io
import sys
import time
from contextlib import redirect_stdout
from pathlib import Path

started = time.perf_counter()
from adapter_protocol import AdapterRequest  # noqa: E402
from registry import load_adapter  # noqa: E402
imported = time.perf_counter()


def main() -> int:
    request = json.loads(sys.stdin.read())
    adapter = load_adapter(request["engine"])
    # Native libraries may print advisory text; stdout remains a single JSON
    # frame so such output can never corrupt the parent protocol.
    native_output = io.StringIO()
    with redirect_stdout(native_output):
        response = adapter.execute(AdapterRequest(
            source=Path(request["source"]), source_sha256=request["source_sha256"],
            password=request.get("password"), clip=tuple(request["clip"]) if request.get("clip") else None,
            render_dpis=tuple(request.get("render_dpis", (150, 200, 300))),
        ))
    warnings = sorted({line.strip() for line in native_output.getvalue().splitlines() if line.strip()})
    response.deterministic_output["warnings"] = warnings
    json.dump({"deterministic_output": response.deterministic_output,
               "operation_ms": response.operation_ms, "output_bytes": response.output_bytes,
               "import_ms": round((imported - started) * 1000, 6)}, sys.stdout,
              sort_keys=True, separators=(",", ":"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
