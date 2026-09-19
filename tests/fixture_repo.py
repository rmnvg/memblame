"""Generate small git repos with known memory behaviour, for tests and demos.

Variants:
  planted  - two planted regressions:
             * "direct":    load_rows() starts holding a 100-byte payload per row (peak grows)
             * "retention": summarize() starts caching its rows (memory retained at end grows,
                            but the allocation still happens inside the unchanged load_rows())
  clean    - same history shape, no memory changes

Run as a script to print the generated history:
    python tests/fixture_repo.py /tmp/demo-repo [planted|clean] [flat|src]
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

N_ROWS = 200_000

PARSE_V1 = '''\
"""Row loading."""


def load_rows(n):
    rows = []
    for i in range(n):
        rows.append(("item-%d" % i, i * 3))
    return rows
'''

PARSE_DIRECT = '''\
"""Row loading."""


def load_rows(n):
    rows = []
    for i in range(n):
        # keep the raw payload around for debugging
        rows.append(("item-%d" % i, i * 3, bytes(100)))
    return rows
'''

REPORT_V1 = '''\
"""Reporting."""
from {pkg}.parse import load_rows


def summarize(n):
    rows = load_rows(n)
    return sum(row[1] for row in rows)
'''

REPORT_HELPER = REPORT_V1 + '''

def format_total(total):
    return f"total={{total:,}}"
'''

REPORT_RETENTION = '''\
"""Reporting."""
from {pkg}.parse import load_rows

_CACHE = {{}}


def summarize(n):
    rows = _CACHE.get(n)
    if rows is None:
        rows = load_rows(n)
        _CACHE[n] = rows
    return sum(row[1] for row in rows)


def format_total(total):
    return f"total={{total:,}}"
'''

APP = '''\
"""Entry point used as the memblame workload."""
from {pkg}.report import summarize

N = {n}


def run():
    return summarize(N)
'''

TEST_APP = '''\
from {pkg}.app import run
from {pkg}.report import summarize


def test_pipeline():
    assert run() > 0


def test_small():
    assert summarize(10) == 135
'''


@dataclass
class FixtureRepo:
    path: Path
    pkg: str
    commits: dict[str, str] = field(default_factory=dict)  # label -> sha
    order: list[str] = field(default_factory=list)  # labels, oldest first

    @property
    def workload(self) -> str:
        return f"call:{self.pkg}.app:run"


def _git(repo: Path, *args: str, env: dict | None = None) -> str:
    out = subprocess.run(
        ["git", *args], cwd=repo, env=env, check=True, capture_output=True, text=True
    )
    return out.stdout.strip()


def make_repo(path: Path, variant: str = "planted", layout: str = "flat") -> FixtureRepo:
    """Create a ~10 commit repo at `path` (must not exist or be empty)."""
    if variant not in ("planted", "clean"):
        raise ValueError(variant)
    path.mkdir(parents=True, exist_ok=True)
    pkg = "shop"
    pkg_dir = path / ("src" if layout == "src" else "") / pkg
    fx = FixtureRepo(path=path, pkg=pkg)

    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.name", "Fixture Bot")
    _git(path, "config", "user.email", "fixture@example.com")
    _git(path, "config", "commit.gpgsign", "false")

    def write(rel: str, text: str) -> None:
        target = (pkg_dir / rel) if not rel.startswith("/") else path / rel[1:]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text.format(pkg=pkg, n=N_ROWS))

    def commit(label: str, subject: str, author: str) -> None:
        idx = len(fx.order)
        env = dict(os.environ)
        date = f"2026-01-{idx + 1:02d}T12:00:00+00:00"
        env.update(
            GIT_AUTHOR_NAME=author,
            GIT_AUTHOR_EMAIL=f"{author.split()[0].lower()}@example.com",
            GIT_AUTHOR_DATE=date,
            GIT_COMMITTER_NAME=author,
            GIT_COMMITTER_EMAIL=f"{author.split()[0].lower()}@example.com",
            GIT_COMMITTER_DATE=date,
        )
        _git(path, "add", "-A")
        _git(path, "commit", "-q", "--allow-empty", "-m", subject, env=env)
        fx.commits[label] = _git(path, "rev-parse", "HEAD")
        fx.order.append(label)

    write("__init__.py", "")
    write("parse.py", PARSE_V1)
    write("report.py", REPORT_V1)
    write("app.py", APP)
    write("/tests/test_app.py", TEST_APP)
    if layout == "src":
        write("/pyproject.toml", '[project]\nname = "shop"\nversion = "0"\n')
    commit("initial", "initial pipeline", "Asha Rao")

    write("/README.md", "# shop\n\nA tiny pipeline.\n")
    commit("docs", "add README", "Asha Rao")

    write("report.py", REPORT_HELPER)
    commit("helper", "add format_total helper", "Ben Ito")

    write("app.py", APP.replace('"""Entry point', '"""Main entry point'))
    commit("docstring", "tweak app docstring", "Ben Ito")

    if variant == "planted":
        write("parse.py", PARSE_DIRECT)
        commit("direct", "include raw payload in rows", "Rahul Mehta")
    else:
        write("parse.py", PARSE_V1.replace('"""Row loading."""', '"""Row loading (v2)."""'))
        commit("direct", "reword parse docstring", "Rahul Mehta")

    write("/README.md", "# shop\n\nA tiny pipeline. Run `run()`.\n")
    commit("docs2", "document run()", "Asha Rao")

    parse_now = PARSE_DIRECT if variant == "planted" else PARSE_V1
    if variant == "planted":
        write("report.py", REPORT_RETENTION)
        commit("retention", "cache summarize results", "Chen Wu")
    else:
        write("report.py", REPORT_HELPER.replace("total={", "Total={"))
        commit("retention", "capitalise total label", "Chen Wu")

    write("/tests/test_app.py", TEST_APP + "\n\ndef test_zero():\n    assert summarize(0) == 0\n")
    commit("tests", "add zero test", "Ben Ito")

    write("parse.py", parse_now.replace("def load_rows(n):", "def load_rows(n):  # noqa"))
    commit("noqa", "silence linter", "Asha Rao")

    write("/CHANGELOG.md", "## 0.2\n- misc\n")
    commit("changelog", "changelog", "Asha Rao")
    return fx


def main() -> None:
    dest = Path(sys.argv[1] if len(sys.argv) > 1 else "fixture-repo")
    variant = sys.argv[2] if len(sys.argv) > 2 else "planted"
    layout = sys.argv[3] if len(sys.argv) > 3 else "flat"
    fx = make_repo(dest, variant, layout)
    print(_git(fx.path, "log", "--format=%h %an: %s", "--reverse"))
    print(f"\nworkload: {fx.workload}")


if __name__ == "__main__":
    main()
