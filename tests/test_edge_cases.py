"""Edge cases found while reviewing the code: real-world project setups that must not break
measurements or produce misleading findings."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from memblame import api, git
from memblame.measure import Settings

BIG = "def run():\n    keep = [bytes(100) for _ in range(100_000)]\n    return len(keep)\n"
SMALL = "def run():\n    return 1\n"


class Repo:
    """A tiny throwaway git repo with helpers to commit files."""

    def __init__(self, path: Path):
        self.path = path
        path.mkdir(parents=True)
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.email", "t@example.com")
        self.git("config", "user.name", "T")
        self.git("config", "commit.gpgsign", "false")

    def git(self, *args: str) -> str:
        return subprocess.run(["git", *args], cwd=self.path, check=True, capture_output=True,
                              text=True).stdout.strip()

    def write(self, files: dict[str, str | bytes]) -> None:
        for rel, content in files.items():
            f = self.path / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(content, bytes):
                f.write_bytes(content)
            else:
                f.write_text(content)

    def commit(self, files: dict[str, str | bytes], msg: str) -> str:
        self.write(files)
        self.git("add", "-A")
        self.git("commit", "-qm", msg)
        return self.git("rev-parse", "HEAD")


def session(repo: Repo, workload: str, cache: bool = False, **kw) -> api.Session:
    settings = Settings(workload=workload, runs=2, python=sys.executable, **kw)
    return api.Session(repo.path, settings, use_cache=cache, progress=lambda m: None)


def peak_finding(out: dict) -> dict | None:
    return next((f for f in out.get("findings", []) if f["metric"] == "peak"), None)


def test_script_imports_sibling_module_and_non_utf8_source(tmp_path):
    """`python bench/run.py` puts bench/ on sys.path; latin-1 sources must still be parsed."""
    r = Repo(tmp_path / "repo")
    latin = "# -*- coding: latin-1 -*-\n# caf\xe9\n" + BIG
    r.commit({"bench/helper.py": SMALL, "bench/run.py": "from helper import run\nrun()\n",
              "bench/__init__.py": ""}, "v1")
    r.commit({"bench/helper.py": latin.encode("latin-1")}, "v2 bigger, latin-1")
    with session(r, "script:bench/run.py") as s:
        out = api.diff(s, "HEAD~1", "HEAD")
    f = peak_finding(out)
    assert f, out["warnings"]
    assert f["verdict"]["function"] == "bench/helper.py::run"


def test_async_call_workload_is_awaited(tmp_path):
    r = Repo(tmp_path / "repo")
    code = "import asyncio\n\nasync def run():\n    keep = [bytes(100) for _ in range({n})]\n" \
           "    await asyncio.sleep(0)\n    return len(keep)\n"
    r.commit({"pkg/__init__.py": "", "pkg/a.py": code.format(n=1000)}, "v1")
    r.commit({"pkg/a.py": code.format(n=100_000)}, "v2")
    with session(r, "call:pkg.a:run") as s:
        out = api.diff(s, "HEAD~1", "HEAD")
    f = peak_finding(out)
    assert f and f["delta"] > 5_000_000
    assert f["verdict"]["function"] == "pkg/a.py::run"


def test_project_pytest_config_with_xdist_cov_randomly(tmp_path):
    """addopts `-n 2 --cov` would run tests in worker processes we cannot see."""
    pytest.importorskip("xdist")
    pytest.importorskip("pytest_cov")
    r = Repo(tmp_path / "repo")
    test = "from pkg.a import run\n\ndef test_run():\n    assert run()\n\n" \
           "def test_other():\n    assert 1\n"
    r.commit({"pkg/__init__.py": "", "pkg/a.py": SMALL.replace("1", "[0] * 10"),
              "tests/test_a.py": test,
              "pytest.ini": "[pytest]\naddopts = -n 2 --cov=pkg -p randomly\n"}, "v1")
    r.commit({"pkg/a.py": BIG}, "v2")
    with session(r, "pytest:tests/test_a.py") as s:
        out = api.diff(s, "HEAD~1", "HEAD")
    names = {u["name"] for u in out["units"]}
    assert names == {"tests/test_a.py::test_run", "tests/test_a.py::test_other"}, out
    assert {f["unit"] for f in out["findings"]} == {"tests/test_a.py::test_run"}


def test_broken_commit_is_not_reported_as_an_improvement(tmp_path):
    """A commit where the workload crashes early uses little memory; that is not a finding."""
    r = Repo(tmp_path / "repo")
    r.commit({"pkg/__init__.py": "", "pkg/a.py": BIG}, "v1")
    r.commit({"pkg/a.py": BIG.replace("return len(keep)", "return len(keep) + None")}, "broken")
    r.commit({"pkg/a.py": BIG + "\n"}, "fixed")
    with session(r, "call:pkg.a:run") as s:
        out = api.range_(s, "HEAD~2", "HEAD", exhaustive=True)
    assert out["findings"] == []
    assert any("workload error" in w for w in out["warnings"])


def test_bisect_skips_commits_where_the_workload_breaks(tmp_path):
    r = Repo(tmp_path / "repo")
    r.commit({"pkg/__init__.py": "", "pkg/a.py": SMALL}, "good")
    r.commit({"pkg/a.py": "def run():\n    raise RuntimeError('wip')\n"}, "broken wip")
    bad = r.commit({"pkg/a.py": BIG}, "grows")
    r.commit({"README": "x"}, "docs")
    with session(r, "call:pkg.a:run") as s:
        out = api.bisect(s, "HEAD~3", "HEAD")
    assert out["status"] == "found", out
    assert out["culprit"]["sha"] == bad
    assert any(t["skipped"] for t in out["measurements"])
    assert any("could not be measured" in w for w in out["warnings"])


def test_commit_that_times_out_is_skipped_in_range(tmp_path):
    r = Repo(tmp_path / "repo")
    r.commit({"pkg/__init__.py": "", "pkg/a.py": SMALL}, "v1")
    r.commit({"pkg/a.py": "import time\n\ndef run():\n    time.sleep(30)\n"}, "hangs")
    r.commit({"pkg/a.py": SMALL + "\n"}, "v3")
    with session(r, "call:pkg.a:run", timeout=3) as s:
        out = api.range_(s, "HEAD~2", "HEAD", exhaustive=True)
    assert [p["valid"] for p in out["points"]] == [True, False, True]
    assert any("SKIPPED" in w and "timed out" in w for w in out["warnings"])
    assert out["findings"] == []


def test_external_benchmark_edit_invalidates_cache(tmp_path):
    r = Repo(tmp_path / "repo")
    r.commit({"pkg/__init__.py": "", "pkg/a.py": SMALL}, "v1")
    bench = tmp_path / "bench.py"
    bench.write_text("keep = [0] * 1000\n")
    with session(r, f"script:{bench}", cache=True) as s:
        first = api.run(s, "HEAD")["result"]["units"]["workload"]["peak"]["median"]
    bench.write_text("keep = [0] * 1_000_000\n")
    with session(r, f"script:{bench}", cache=True) as s:
        second = api.run(s, "HEAD")["result"]["units"]["workload"]["peak"]["median"]
        assert s.measured == 1  # not served from the stale cache
    assert second > first + 5_000_000


def test_clean_working_tree_is_measured_once(tmp_path):
    r = Repo(tmp_path / "repo")
    r.commit({"pkg/__init__.py": "", "pkg/a.py": SMALL}, "v1")
    with session(r, "call:pkg.a:run") as s:
        out = api.diff(s, "HEAD", git.WORKTREE)
        assert s.measured == 1
    assert out["findings"] == [] and out["notes"]


def test_untracked_files_count_as_working_tree_changes(tmp_path):
    r = Repo(tmp_path / "repo")
    r.commit({"pkg/__init__.py": "", "pkg/a.py": "from pkg import b\n\n" + SMALL.replace(
        "return 1", "return b.load()"), "pkg/b.py": "def load():\n    return 1\n"}, "v1")
    (r.path / "pkg" / "b.py").write_text("def load():\n    return 1\n")
    assert not git.is_dirty(r.path)
    data = r.path / "settings.json"
    data.write_text('{"size": 1000}\n')
    assert git.is_dirty(r.path)  # data and config can affect an otherwise unchanged workload
    data.unlink()
    assert not git.is_dirty(r.path)
    (r.path / "pkg" / "new mödule.py").write_text("x = 1\n")  # spaces + non-ASCII
    assert git.is_dirty(r.path)
    hunks = git.diff_hunks(r.path, "HEAD", git.WORKTREE)
    assert any(h.file == "pkg/new mödule.py" for h in hunks)


def test_non_ascii_and_spaces_in_changed_paths(tmp_path):
    r = Repo(tmp_path / "repo")
    r.commit({"my pkg/modül.py": "x = 1\n"}, "v1")
    r.commit({"my pkg/modül.py": "x = 2\ny = 3\n"}, "v2")
    hunks = git.diff_hunks(r.path, "HEAD~1", "HEAD")
    assert [h.file for h in hunks] == ["my pkg/modül.py"]


def test_range_rejects_non_ancestor(tmp_path):
    r = Repo(tmp_path / "repo")
    r.commit({"pkg/__init__.py": "", "pkg/a.py": SMALL}, "v1")
    r.git("checkout", "-q", "-b", "side")
    r.commit({"x.py": ""}, "side")
    r.git("checkout", "-q", "main")
    r.commit({"y.py": ""}, "main")
    with session(r, "call:pkg.a:run") as s, pytest.raises(ValueError, match="not an ancestor"):
        api.range_(s, "side", "main")


def test_range_rejects_ancestor_outside_first_parent_history(tmp_path):
    r = Repo(tmp_path / "repo")
    r.commit({"pkg/__init__.py": "", "pkg/a.py": SMALL}, "root")
    r.git("checkout", "-q", "-b", "side")
    side = r.commit({"side.py": "x = 1\n"}, "side")
    r.git("checkout", "-q", "main")
    r.commit({"main.py": "x = 1\n"}, "mainline")
    r.git("merge", "-q", "--no-ff", "side", "-m", "merge side")
    assert git.is_ancestor(r.path, side, "HEAD")
    assert not git.is_first_parent_ancestor(r.path, side, git.resolve(r.path, "HEAD"))
    with session(r, "call:pkg.a:run") as s, pytest.raises(ValueError,
                                                                  match="not on its first-parent"):
        api.range_(s, side, "HEAD")


def test_worktree_measurement_does_not_write_python_bytecode(tmp_path):
    r = Repo(tmp_path / "repo")
    r.commit({"pkg/__init__.py": "", "pkg/a.py": SMALL}, "v1")
    with session(r, "call:pkg.a:run") as s:
        out = api.run(s, git.WORKTREE)
    assert out["result"]["units"]["workload"]["outcome"] == "passed"
    assert list(r.path.rglob("__pycache__")) == []


def test_missing_dependency_gets_an_interpreter_hint(tmp_path):
    r = Repo(tmp_path / "repo")
    r.commit({"pkg/__init__.py": "", "pkg/a.py": "import not_installed_xyz\n" + SMALL}, "v1")
    with session(r, "call:pkg.a:run") as s:
        out = api.run(s, "HEAD")
    assert any("not_installed_xyz" in w and "--python" in w for w in out["warnings"])


def test_stale_worktree_from_killed_run_is_removed(tmp_path):
    r = Repo(tmp_path / "repo")
    sha = r.commit({"a.py": ""}, "v1")
    base = Path(tempfile.mkdtemp(prefix=git.WORKTREE_PREFIX))
    (base / "pid").write_text("999999")  # no such process
    git.git(r.path, "worktree", "add", "--detach", str(base / "wt"), sha)
    live = Path(tempfile.mkdtemp(prefix=git.WORKTREE_PREFIX))
    (live / "pid").write_text(str(os.getpid()))  # still running: must be kept
    git.git(r.path, "worktree", "add", "--detach", str(live / "wt"), sha)
    try:
        removed = git.remove_stale_worktrees(r.path)
        assert [Path(p).parent.name for p in removed] == [base.name]
        assert not base.exists()
        assert (live / "wt").exists()
    finally:
        git.git(r.path, "worktree", "remove", "--force", str(live / "wt"), check=False)


def test_runner_preloads_no_modules_the_workload_might_import(tmp_path):
    """Modules the runner imports before tracing are 'free' for the workload, which hides
    import-time memory (a real tomlkit commit added `import dataclasses` -> `inspect`)."""
    probe = "import sys\nopen(sys.argv[1], 'w').write('\\n'.join(sorted(sys.modules)))\n"
    (tmp_path / "probe.py").write_text(probe)
    subprocess.run([sys.executable, "probe.py", "bare.txt"], cwd=tmp_path, check=True)
    r = Repo(tmp_path / "repo")
    r.commit({"a.py": ""}, "v1")
    with session(r, f"script:{tmp_path / 'probe.py'} {tmp_path / 'runner.txt'}") as s:
        s.result("HEAD")  # fast runs only: these produce the numbers (attribution may differ)
    bare = set((tmp_path / "bare.txt").read_text().split())
    seen = set((tmp_path / "runner.txt").read_text().split())
    assert seen - bare <= {"__future__", "_tracemalloc", "gc"}, sorted(seen - bare)


def test_script_globals_are_not_retained_but_module_caches_are(tmp_path):
    """'retained' = memory that outlives the workload, not the script's own variables."""
    r = Repo(tmp_path / "repo")
    r.commit({"pkg/__init__.py": "", "pkg/cache.py": "CACHE = []\n",
              "run.py": "from pkg import cache\n"
                        "data = [bytes(100) for _ in range(50_000)]  # script global\n"
                        "cache.CACHE.append([bytes(100) for _ in range(20_000)])  # leaks\n"},
             "v1")
    with session(r, "script:run.py") as s:
        _, res = s.result("HEAD")
    unit = res["units"]["workload"]
    assert unit["peak"]["median"] > 6_000_000  # both lists were alive at the peak
    assert 2_000_000 < unit["end"]["median"] < 4_000_000  # only the cached list outlives
