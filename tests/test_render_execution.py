"""The render interface owns completion, child cleanup and response identity."""

import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

from corridor import render_profiles as rendering
from pdf_fixture_support import PdfFixture


def worker_project(tmp_path, monkeypatch, body):
    worker = tmp_path / 'worker'
    worker.mkdir()
    for name in ('pyproject.toml', 'uv.lock'):
        shutil.copy2(rendering.DEFAULT_WORKER_PROJECT / name, worker / name)
    environment = os.environ.get('UV_PROJECT_ENVIRONMENT', '.venv')
    (worker / environment).symlink_to((rendering.DEFAULT_WORKER_PROJECT / environment).resolve(), target_is_directory=True)
    monkeypatch.setenv('UV_OFFLINE', '1')
    (worker / 'render_worker.py').write_text('import sys, json\nfrom pathlib import Path\n' + body)
    return worker


def source_pdf(tmp_path):
    fixture = PdfFixture()
    fixture.add_page().text((30, 30), 'source')
    return fixture.save(tmp_path / 'source.pdf')


def test_empty_success_is_a_processing_failure(tmp_path, monkeypatch):
    worker = worker_project(tmp_path, monkeypatch,
        "Path(sys.argv[sys.argv.index('--manifest')+1]).write_text(json.dumps({'manifests': []}))\n")
    with pytest.raises(ValueError, match='complete'):
        rendering.render_page_derivatives(pdf_path=source_pdf(tmp_path), page_number=1,
            profile_names=('review', 'ocr_layout'), output_dir=tmp_path / 'output', worker_project=worker)


def test_render_deadline_kills_and_reaps_its_process_group(tmp_path, monkeypatch):
    worker = worker_project(tmp_path, monkeypatch, '''import os, subprocess, time
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
Path(__file__).with_name('pids.json').write_text(json.dumps([os.getpid(), child.pid]))
time.sleep(60)
''')
    with pytest.raises(TimeoutError, match='render'):
        rendering.render_page_derivatives(pdf_path=source_pdf(tmp_path), page_number=1,
            profile_names=('review',), output_dir=tmp_path / 'output', worker_project=worker,
            limits=rendering.RenderExecutionLimits(wall_seconds=2))
    for pid in json.loads((worker / 'pids.json').read_text()):
        state = subprocess.run(['ps', '-o', 'stat=', '-p', str(pid)], capture_output=True, text=True, timeout=2)
        assert not state.stdout.strip() or state.stdout.strip().startswith('Z')
    assert not list((tmp_path / 'output').glob('.*.json'))


@pytest.mark.parametrize('mutation', ['partial', 'duplicate', 'page', 'source', 'size', 'pixels'])
def test_renderer_refuses_incomplete_or_misbound_worker_results(tmp_path, monkeypatch, mutation):
    body = '''import runpy, hashlib
try:
    runpy.run_path(str(Path(__file__).with_name('real_worker.py')), run_name='__main__')
except SystemExit as done:
    if done.code: raise
path = Path(sys.argv[sys.argv.index('--manifest')+1])
payload = json.loads(path.read_text())
rows = payload['manifests']
''' + {
        'partial': 'rows.pop()\n',
        'duplicate': 'rows[1] = rows[0]\n',
        'page': "rows[0]['regenerable_from']['page_number'] = 2\n",
        'source': "rows[0]['regenerable_from']['source_sha256'] = 'a' * 64\n",
        'size': "rows[0]['artifact_bytes'] += 1\n",
        'pixels': "p = Path(rows[0]['artifact_path']); p.write_bytes(b'not a PNG'); rows[0]['artifact_sha256'] = hashlib.sha256(p.read_bytes()).hexdigest(); rows[0]['artifact_bytes'] = p.stat().st_size\n",
    }[mutation] + 'path.write_text(json.dumps(payload))\n'
    worker = worker_project(tmp_path, monkeypatch, body)
    shutil.copy2(rendering.DEFAULT_WORKER_PROJECT / 'render_worker.py', worker / 'real_worker.py')
    shutil.copy2(rendering.DEFAULT_WORKER_PROJECT / 'raster.py', worker / 'raster.py')
    with pytest.raises((ValueError, OSError)):
        rendering.render_page_derivatives(pdf_path=source_pdf(tmp_path), page_number=1,
            profile_names=('review', 'ocr_layout'), output_dir=tmp_path / 'output', worker_project=worker)
