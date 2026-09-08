"""A corrected worker can coexist with the old pixels in one caller directory."""

from hashlib import sha256
import os
import shutil

from corridor.render_profiles import DEFAULT_WORKER_PROJECT, render_page_derivative
from test_native_pipeline_geometry import _author_page, _colour_deskew_profile


def test_worker_source_identity_separates_pixels_before_rendering(tmp_path, monkeypatch):
    authored = _author_page(tmp_path, rotation=0, skew=3)
    _colour_deskew_profile(monkeypatch)
    worker = tmp_path / "worker"
    worker.mkdir()
    for path in (*DEFAULT_WORKER_PROJECT.glob("*.py"), DEFAULT_WORKER_PROJECT / "pyproject.toml", DEFAULT_WORKER_PROJECT / "uv.lock"):
        shutil.copy2(path, worker / path.name)
    # The setup gate installed this exact locked environment. No test may
    # download dependencies for a copied worker project.
    environment_name = os.environ.get("UV_PROJECT_ENVIRONMENT", ".venv")
    (worker / environment_name).symlink_to((DEFAULT_WORKER_PROJECT / environment_name).resolve(), target_is_directory=True)
    monkeypatch.setenv("UV_OFFLINE", "1")
    source_path = worker / "render_worker.py"
    corrected_bytes = source_path.read_bytes()
    corrected = corrected_bytes.decode()
    old = corrected.replace("correction = angle if preprocessing_version == PDFIUM_PREPROCESSING_VERSION else -angle", "correction = -angle")
    assert old != corrected
    source_path.write_text(old)
    before = render_page_derivative(pdf_path=authored.path, page_number=1, profile_name="table_cv",
        output_dir=tmp_path / "same-caller-directory", worker_project=worker, rasterizer="pdfium")
    retained = before.artifact_path.read_bytes()
    source_path.write_bytes(corrected_bytes)
    after = render_page_derivative(pdf_path=authored.path, page_number=1, profile_name="table_cv",
        output_dir=tmp_path / "same-caller-directory", worker_project=worker, rasterizer="pdfium")
    assert before.artifact_path != after.artifact_path
    assert before.artifact_path.parent != after.artifact_path.parent
    assert before.parameters["worker_runtime_sha256s"] != after.parameters["worker_runtime_sha256s"]
    assert before.artifact_path.read_bytes() == retained
    assert sha256(retained).hexdigest() == before.artifact_sha256
    assert before.artifact_sha256 != after.artifact_sha256
    again = render_page_derivative(pdf_path=authored.path, page_number=1, profile_name="table_cv",
        output_dir=tmp_path / "same-caller-directory", worker_project=worker, rasterizer="pdfium")
    assert again.artifact_path == after.artifact_path
    assert before.artifact_path.read_bytes() == retained
