"""First-write-only staged publication for M8 acceptance artifacts.

Acceptance outputs are immutable evidence bundles.  Publication therefore
builds into a sibling staging directory and renames it into place only once
the full directory is ready, so a crash or injected failure cannot strand a
half-written capture or silently overwrite a frozen one.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
import shutil
import tempfile


DirectoryBuilder = Callable[[Path], None]


def publish_directory_once(
    output_dir: Path,
    *,
    temp_prefix: str,
    build: DirectoryBuilder,
) -> Path:
    """Build a directory tree once, atomically enough for local evidence use."""

    output_dir = Path(output_dir)
    if output_dir.exists() or output_dir.is_symlink():
        raise FileExistsError(f"{output_dir} already exists")

    output_dir.parent.mkdir(parents=True, exist_ok=True)
    staging_dir = Path(
        tempfile.mkdtemp(prefix=f".{temp_prefix}-", dir=output_dir.parent)
    )
    try:
        build(staging_dir)
        staging_dir.rename(output_dir)
    except Exception:
        shutil.rmtree(staging_dir, ignore_errors=True)
        raise
    return output_dir
