# Contributing

## Setup

```
uv venv && uv pip install -e . pytest ruff pytest-xdist pytest-cov pytest-randomly
pytest                    # unit + end-to-end tests against generated git repos (~2-3 min)
ruff check src tests
```

The VS Code extension lives in `vscode-ext/` and bundles a copy of `src/memblame` at build
time (`npm run compile`), so there is a single engine to change:

```
cd vscode-ext
npm ci
npm test                  # unit tests
npm run test:integration  # launches a real VS Code (needs a display; xvfb on Linux)
```

## Guidelines

* `runner.py` runs inside the *project's* interpreter and must stay standard-library only and
  must not import the rest of memblame (see its module docstring).
* The JSON output is a public contract (`"schema": 1`, `memblame/contract.py`, mirrored by
  `vscode-ext/src/contract.ts`). Changing a field means updating both sides and their tests.
* Add a test with every fix. Tests build throwaway git repos (`tests/fixture_repo.py`,
  `tests/test_edge_cases.py::Repo`); prefer that over mocking git.
* CI runs Linux, macOS and Windows on Python 3.9, 3.12 and 3.14, so avoid syntax newer than
  3.9 in `src/` and keep path handling portable.

## Releasing

Bump `__version__` in `src/memblame/__init__.py` and `version` in `vscode-ext/package.json`
(a test keeps them equal), update the changelogs, then push a tag `vX.Y.Z`. The
[release workflow](.github/workflows/release.yml) publishes to PyPI and, if configured, to the
VS Code Marketplace.

## Security

Report vulnerabilities privately, see [SECURITY.md](SECURITY.md).
