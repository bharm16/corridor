"""Main-branch dependency caches are reusable across PRs without rerunning tests."""
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def workflow(name):
    return yaml.load((ROOT / '.github/workflows' / name).read_text(), Loader=yaml.BaseLoader)


def test_cache_warming_is_main_only_and_dependency_scoped():
    value = workflow('warm-python-cache.yml')
    assert set(value['on']) == {'push', 'workflow_dispatch'}
    assert value['on']['push']['branches'] == ['main']
    paths = set(value['on']['push']['paths'])
    # Include every setup-uv default cache-dependency glob, plus Python and
    # workflow policy changes that can change the selected cache identity.
    assert paths == {'**/pyproject.toml', '**/uv.lock', '**/*.py.lock',
        '**/*requirements*.txt', '**/*requirements*.in', '**/*constraints*.txt', '**/*constraints*.in',
        '**/.python-version', 'uv.toml', '.github/workflows/release-gate.yml',
        '.github/workflows/full-suite.yml', '.github/workflows/warm-python-cache.yml', 'scripts/ci_environment.sh'}
    assert value['jobs']['warm']['if'] == "${{ github.ref == 'refs/heads/main' }}"
    assert value['permissions'] == {'contents': 'read'}
    assert value['concurrency']['cancel-in-progress'] == 'false'


def test_warmer_uses_the_release_gates_exact_dependency_cache_populations():
    warm = workflow('warm-python-cache.yml')['jobs']['warm']
    gate = workflow('release-gate.yml')['jobs']
    profiles = warm['strategy']['matrix']['include']
    assert profiles == [{'cache_suffix': 'root-check-wheels-v1', 'render': 'false'},
                        {'cache_suffix': 'root-render-wheels-v1', 'render': 'true'}]
    setup = next(s for s in warm['steps'] if s.get('uses', '').startswith('astral-sh/setup-uv@'))
    assert setup['with']['cache-suffix'] == '${{ matrix.cache_suffix }}'
    for name, expected in [('check', profiles[0]['cache_suffix']), ('pytest', profiles[1]['cache_suffix']),
                           ('slow', profiles[1]['cache_suffix']), ('migration', profiles[1]['cache_suffix'])]:
        reference = next(s for s in gate[name]['steps'] if s.get('uses', '').startswith('astral-sh/setup-uv@'))
        assert reference['uses'] == setup['uses']
        assert reference['with']['cache-suffix'] == expected
        assert warm['runs-on'] == gate[name]['runs-on']
        for key in ('enable-cache', 'prune-cache', 'python-version', 'version', 'version-file', 'cache-dependency-glob'):
            assert setup['with'].get(key) == reference['with'].get(key)
    assert setup['with']['enable-cache'] == 'true'
    assert setup['with']['prune-cache'] == 'false'


def test_cache_warming_only_installs_locked_dependencies():
    warm = workflow('warm-python-cache.yml')['jobs']['warm']
    # An explicit step allowlist excludes tests, PostgreSQL, deployment tools,
    # repository writes, and side-effectful hooks hidden in additional steps.
    assert warm['steps'] == [
        {'uses': 'actions/checkout@v4'},
        {'uses': 'astral-sh/setup-uv@v6', 'with': {'enable-cache': 'true', 'prune-cache': 'false',
            'cache-suffix': '${{ matrix.cache_suffix }}'}},
        {'run': 'uv sync --locked'},
        {'if': '${{ matrix.render }}', 'run': 'uv sync --project workers/render --frozen'},
    ]
    assert 'services' not in warm
    assert 'permissions' not in warm
    assert int(warm['timeout-minutes']) <= 10
