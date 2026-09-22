# Changelog

## 0.1.2

- Bundled engine update: a workload that exited just as its timeout fired could abort the
  whole measurement with a macOS-only `PermissionError`. Fixed; no extension-side change.

## 0.1.1

- The bundled engine finds a virtualenv kept beside the code (`backend/.venv`), not only one at the repository root.

## 0.1.0

- First release: compare working tree vs HEAD, compare commits, adaptive range timeline, bisect.
- CodeLens on blamed functions and on pytest tests; inline hot-line annotations.
- Bundled stdlib-only engine; uses the interpreter selected in the Python extension.
