"""Edge cases found while reviewing the code: real-world project setups that must not break
measurements or produce misleading findings."""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from memblame import api, artifact, git, measure, report
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


def test_sync_and_async_return_values_are_not_counted_as_retained(tmp_path):
    r = Repo(tmp_path / "repo")
    code = ("import asyncio\n\ndef sync():\n    return bytearray(4_000_000)\n\n"
            "async def async_():\n    return bytearray(4_000_000)\n")
    sha = r.commit({"bench.py": code}, "returns buffers")
    retained = []
    for function in ("sync", "async_"):
        with session(r, f"call:bench:{function}") as s:
            _, result = s.result(sha)
        retained.append(result["units"]["workload"]["end"]["median"])
    assert abs(retained[0] - retained[1]) < 500_000


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


def test_bisect_verification_finds_earliest_crossing_in_nonmonotonic_history(tmp_path):
    r = Repo(tmp_path / "repo")
    for index, size in enumerate((100_000, 4_000_000, 100_000, 100_000, 4_000_000)):
        r.commit({"bench.py": f"x = bytearray({size})\n# point {index}\n"}, f"point {index}")

    with session(r, "script:bench.py") as s:
        fast = api.bisect(s, "HEAD~4", "HEAD", threshold="1MB", metric="peak")
    assert fast["culprit"]["subject"] == "point 4"
    assert fast["verified"] is False and fast["monotonic"] is None
    assert any("assumes memory crosses" in warning for warning in fast["warnings"])

    with session(r, "script:bench.py") as s:
        verified = api.bisect(s, "HEAD~4", "HEAD", threshold="1MB", metric="peak",
                              verify=True)
    assert verified["culprit"]["subject"] == "point 1"
    assert verified["verified"] is True and verified["monotonic"] is False
    assert any("not monotonic" in warning for warning in verified["warnings"])


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


def _process_tree_workload(marker: Path) -> str:
    child = (f"import os,time; open({str(marker)!r}, 'w').write(str(os.getpid())); "
             "time.sleep(30)")
    return ("import subprocess,sys,time\n"
            f"subprocess.Popen([sys.executable, '-c', {child!r}])\n"
            "time.sleep(30)\n")


def _wait_for_pid(marker: Path) -> int:
    for _ in range(100):
        if marker.exists() and marker.read_text():
            return int(marker.read_text())
        time.sleep(0.05)
    raise AssertionError("workload child did not start")


def _wait_until_gone(pid: int) -> bool:
    # os.kill(pid, 0) would *terminate* the process on Windows; git._pid_alive is portable.
    for _ in range(150):
        if not git._pid_alive(pid):
            return True
        time.sleep(0.02)
    return False


@pytest.mark.skipif(os.name == "nt", reason="process groups are POSIX-only")
def test_terminating_a_group_of_dead_processes_is_not_an_error(monkeypatch):
    """macOS answers EPERM, not ESRCH, when every process left in a group is already dead.

    A workload that exits just as its timeout fires leaves exactly that behind, and the first
    signal used to let the PermissionError escape and abort the whole measurement.
    """
    sent: list[int] = []

    def killpg(pid: int, sig: int) -> None:
        sent.append(sig)
        raise PermissionError(1, "Operation not permitted")

    class Reaped:
        pid = 4242

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(measure.os, "killpg", killpg)
    measure._terminate_process_tree(Reaped())  # type: ignore[arg-type]
    # it still went on to the second, forceful signal for any stubborn descendant
    assert sent == [signal.SIGTERM, signal.SIGKILL]


def test_timeout_terminates_workload_descendants(tmp_path):
    r = Repo(tmp_path / "repo")
    marker = tmp_path / "child.pid"
    r.commit({"bench.py": _process_tree_workload(marker)}, "spawns child")
    with session(r, "script:bench.py", timeout=1) as s:
        _, result = s.result("HEAD")
    child_pid = _wait_for_pid(marker)
    assert "timed out" in result["error"]
    assert _wait_until_gone(child_pid)


@pytest.mark.skipif(os.name == "nt", reason="Windows cancellation is performed by the extension")
def test_sigterm_cancellation_terminates_workload_descendants(tmp_path):
    r = Repo(tmp_path / "repo")
    marker = tmp_path / "child.pid"
    r.commit({"bench.py": _process_tree_workload(marker)}, "spawns child")
    proc = subprocess.Popen([
        sys.executable, "-m", "memblame", "run", "-C", str(r.path),
        "-w", "script:bench.py", "--runs", "1", "--no-cache", "--python", sys.executable,
    ], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    child_pid = _wait_for_pid(marker)
    proc.terminate()
    proc.communicate(timeout=10)
    assert _wait_until_gone(child_pid)


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


def test_declared_environment_and_file_inputs_invalidate_cache(tmp_path, monkeypatch):
    r = Repo(tmp_path / "repo")
    sha = r.commit({"bench.py": "import os\nx = bytearray(int(os.environ['SIZE']))\n"}, "v1")
    data = tmp_path / "data.txt"
    data.write_text("one")
    settings = {"cache_env": ["SIZE"], "cache_inputs": [str(data)]}

    monkeypatch.setenv("SIZE", "100000")
    with session(r, "script:bench.py", cache=True, **settings) as s:
        _, first = s.result(sha)
        assert s.measured == 1
    with session(r, "script:bench.py", cache=True, **settings) as s:
        _, again = s.result(sha)
        assert s.measured == 0
        assert again["units"]["workload"]["peak"] == first["units"]["workload"]["peak"]

    monkeypatch.setenv("SIZE", "4000000")
    with session(r, "script:bench.py", cache=True, **settings) as s:
        _, changed_env = s.result(sha)
        assert s.measured == 1
        assert changed_env["units"]["workload"]["peak"]["median"] > 4_000_000

    data.write_text("two")
    with session(r, "script:bench.py", cache=True, **settings) as s:
        s.result(sha)
        assert s.measured == 1


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


@pytest.mark.skipif(os.name == "nt", reason="no mkfifo on Windows")
def test_a_declared_cache_input_that_is_not_a_regular_file_does_not_hang(tmp_path):
    """A FIFO or device never reaches EOF, so read_bytes() would never return.

    memblame hung for ever, buffer growing, with nothing printed. Reading is now skipped
    for anything that is not a regular file; inside a declared directory the is_file()
    filter already did this.
    """
    fifo = tmp_path / "pipe"
    os.mkfifo(fifo)
    regular = tmp_path / "data.bin"
    regular.write_bytes(b"payload")

    def digest(*inputs):
        return measure.workload_inputs_hash(
            tmp_path, Settings("call:a:b", cache_inputs=[str(i) for i in inputs]))

    # Would not return at all before the fix.
    assert digest(fifo)
    # Still distinguishes a FIFO from a regular file, a missing path and nothing at all.
    assert len({digest(fifo), digest(regular), digest(tmp_path / "gone"), digest()}) == 4
    # A FIFO sitting inside a declared directory is skipped, not read.
    assert digest(tmp_path)


def test_control_bytes_in_a_commit_subject_do_not_break_analysis(tmp_path):
    """Real git, not a synthetic record: git stores a subject byte-for-byte."""
    r = Repo(tmp_path / "repo")
    weird = "subject with \x1e a record separator"
    base = r.commit({"bench.py": "def run():\n    return [0] * 1000\n"}, "plain")
    head = r.commit({"bench.py": "def run():\n    return [0] * 500_000\n"}, weird)
    with session(r, "call:bench:run") as s:
        out = api.diff(s, base, head)

    assert out["measurement_status"] == "complete", out["warnings"]
    assert out["head"]["subject"] == weird
    assert any(f["delta"] > 0 for f in out["findings"])
    # The renderers must carry it through without tearing the report apart either.
    for rendered in (report.format_diff(out), artifact.markdown(out), artifact.html_report(out)):
        assert "record separator" in rendered


def test_source_lines_that_look_like_diff_headers_do_not_break_blame(tmp_path):
    """Real git output, not a synthetic diff: a deleted `-- "..."` line arrives as `--- "..."`.

    memblame used to read it as a file header, which aborted the analysis on the unterminated
    quote and blamed later hunks on a path that does not exist.
    """
    r = Repo(tmp_path / "repo")
    before = ('SQL = """\n-- "unterminated sql comment\nSELECT 1\n"""\n\n'
              "def run():\n    return [0] * 100_000\n")
    after = ('SQL = """\nSELECT 1\n"""\n\n'
             "def run():\n    return [0] * 2_000_000\n")
    base = r.commit({"bench.py": before}, "with the sql comment")
    head = r.commit({"bench.py": after}, "drop it and allocate more")
    with session(r, "call:bench:run") as s:
        out = api.diff(s, base, head)

    assert out["measurement_status"] == "complete", out["warnings"]
    peak = next(f for f in out["findings"] if f["metric"] == "peak")
    assert peak["delta"] > 0
    # Blamed on the real file, never on a path invented from the hunk body.
    assert peak["verdict"]["file"] == "bench.py"
    assert {c["file"] for c in out["changed_functions"]} == {"bench.py"}


def test_orphaned_scratch_directories_are_reclaimed(tmp_path):
    """A hard kill leaves directories `git worktree list` never mentions.

    One killed before `git worktree add` finished, and every per-run scratch directory
    (which holds the workload's uncapped stdout/stderr), are invisible to git, so without
    this sweep they accumulate in the temp directory for ever.
    """
    def scratch(name, pid=None):
        d = tmp_path / name
        d.mkdir()
        (d / "stdout.log").write_text("x" * 100)
        if pid is not None:
            (d / "pid").write_text(str(pid))
        return d

    dead_setup = scratch(f"{git.WORKTREE_PREFIX}deadsetup", 999999)  # no such process
    dead_run = scratch("mb-run-dead", 999999)
    live_setup = scratch(f"{git.WORKTREE_PREFIX}live", os.getpid())
    live_run = scratch("mb-run-live", os.getpid())
    no_pid = scratch(f"{git.WORKTREE_PREFIX}nopid")  # older memblame, or still starting up
    stranger = scratch("not-memblame", 999999)

    removed = git._remove_orphan_scratch(tmp_path)

    assert sorted(Path(r).name for r in removed) == ["mb-deadsetup", "mb-run-dead"]
    assert not dead_setup.exists() and not dead_run.exists()
    # A concurrent memblame, an older one and an unrelated directory are all left alone.
    for kept in (live_setup, live_run, no_pid, stranger):
        assert kept.exists(), kept


def test_a_killed_run_leaves_no_scratch_behind_after_the_next_run(tmp_path):
    """End to end: the next memblame reclaims what a killed one left in the temp directory."""
    r = Repo(tmp_path / "repo")
    r.commit({"a.py": SMALL}, "v1")
    orphan = Path(tempfile.mkdtemp(prefix="mb-run-"))
    (orphan / "pid").write_text("999999")
    (orphan / "stdout.log").write_text("x" * 10_000)
    try:
        with session(r, "call:a:run") as s:
            api.run(s, "HEAD")
        assert not orphan.exists()
    finally:
        shutil.rmtree(orphan, ignore_errors=True)


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


def _detached_child_code(marker: Path, seconds: int = 25) -> str:
    return (f"import os,time; open({str(marker)!r}, 'w').write(str(os.getpid())); "
            f"time.sleep({seconds})")


@pytest.mark.skipif(os.name == "nt", reason="uses POSIX sessions to escape the process group")
def test_escaped_descendant_cannot_hang_or_delay_a_timeout(tmp_path):
    """A daemonised child that keeps stdout open used to make a 1 s timeout take 25 s (and a
    child that never exits made memblame wait forever): we wait on the runner, not on EOF."""
    r = Repo(tmp_path / "repo")
    marker = tmp_path / "escaped.pid"
    bench = ("import subprocess, sys, time\n"
             f"subprocess.Popen([sys.executable, '-c', {_detached_child_code(marker)!r}],"
             " start_new_session=True)\n"
             "time.sleep(60)\n")
    r.commit({"bench.py": bench}, "escapes the process group")
    started = time.time()
    try:
        with session(r, "script:bench.py", timeout=1) as s:
            _, result = s.result("HEAD")
        assert time.time() - started < 15
        assert "timed out" in result["error"]
    finally:
        if marker.exists() and marker.read_text():
            try:
                os.kill(int(marker.read_text()), 9)
            except OSError:
                pass


@pytest.mark.skipif(os.name == "nt", reason="a finished Windows parent names no children")
def test_background_process_left_behind_by_a_finished_run_is_removed(tmp_path):
    """Measurements must not leak processes (or let them skew the next run)."""
    r = Repo(tmp_path / "repo")
    marker = tmp_path / "left.pid"
    bench = ("import os, subprocess, sys, time\n"
             # runs repeat: a marker left by the previous run would satisfy the wait below
             f"if os.path.exists({str(marker)!r}): os.remove({str(marker)!r})\n"
             f"subprocess.Popen([sys.executable, '-c', {_detached_child_code(marker, 60)!r}])\n"
             f"for _ in range(400):  # exit only once the child is provably running\n"
             f"    if os.path.exists({str(marker)!r}) and os.path.getsize({str(marker)!r}):\n"
             "        break\n"
             "    time.sleep(0.05)\n")
    r.commit({"bench.py": bench}, "spawns a background child and exits")
    with session(r, "script:bench.py") as s:
        _, result = s.result("HEAD")
    assert result["units"]["workload"]["outcome"] == "passed"
    assert _wait_until_gone(_wait_for_pid(marker))


def test_workload_cannot_block_on_stdin_or_a_full_output_pipe(tmp_path):
    r = Repo(tmp_path / "repo")
    bench = ("import sys\n"
             "sys.stdout.write('x' * 5_000_000)  # far beyond any pipe buffer\n"
             "sys.stderr.write('e' * 200_000)\n"
             "try:\n    input()\nexcept EOFError:\n    print('no stdin')\n")
    r.commit({"bench.py": bench}, "chatty and interactive")
    started = time.time()
    with session(r, "script:bench.py", timeout=30) as s:
        _, result = s.result("HEAD")
    assert time.time() - started < 25
    assert result["units"]["workload"]["outcome"] == "passed"


def _broken_middle_range(tmp_path):
    r = Repo(tmp_path / "repo")
    r.commit({"pkg/__init__.py": "", "pkg/a.py": SMALL}, "small")
    r.commit({"pkg/a.py": BIG}, "grows")
    r.commit({"pkg/a.py": "def run():\n    raise RuntimeError('wip')\n"}, "broken")
    r.commit({"pkg/a.py": BIG + "\n"}, "fixed")
    return r


def test_range_keeps_real_findings_next_to_a_broken_commit(tmp_path, capsys):
    """One unmeasurable old commit must not hide a regression found between measured ones,
    and must never read as an all-clear."""
    from memblame import artifact, cli, report

    r = _broken_middle_range(tmp_path)
    args = ["range", "HEAD~3..HEAD", "--all", "-C", str(r.path), "-w", "call:pkg.a:run",
            "--python", sys.executable, "--runs", "1", "--no-cache"]
    code = cli.main(args + ["--json"])
    out = json.loads(capsys.readouterr().out)
    assert code == 1  # incomplete: CI must not treat it as a pass...
    assert out["measurement_status"] == "incomplete" and out["incomplete_commits"] == 1
    assert [f["verdict"]["function"] for f in out["findings"]] == ["pkg/a.py::run"]
    text, md, html = report.format_range(out), artifact.markdown(out), artifact.html_report(out)
    for rendered in (text, md, html):
        assert "1 commit(s) could not be measured or did not pass" in rendered
        assert "not an all-clear" in rendered
        assert "pkg/a.py" in rendered  # ...and the regression is still shown
        assert "No significant memory changes" not in rendered


def test_two_failing_runs_are_not_compared_as_memory_data(tmp_path):
    r = Repo(tmp_path / "repo")
    body = ("def run():\n    keep = [bytes(100) for _ in range({n})]\n"
            "    raise ValueError(len(keep))\n")
    r.commit({"pkg/__init__.py": "", "pkg/a.py": body.format(n=1000)}, "fails early")
    r.commit({"pkg/a.py": body.format(n=100_000)}, "fails later, after allocating more")
    with session(r, "call:pkg.a:run") as s:
        out = api.range_(s, "HEAD~1", "HEAD", exhaustive=True)
    assert out["findings"] == []
    assert out["measurement_status"] == "incomplete"


@pytest.mark.parametrize("noprefix", [False, True])
def test_diff_paths_survive_git_quoting_and_prefix_config(tmp_path, noprefix):
    repo = Repo(tmp_path / "repo")
    names = ["a/normal.py", "b/space name.py", "café.py"]
    if os.name != "nt":
        names += ['quote"name.py', 'tab\tname.py', 'back\\slash.py', 'café".py']
    repo.commit({name: "x = 1\n" for name in names}, "before")
    repo.commit({name: "x = 2\n" for name in names}, "after")
    repo.git("config", "diff.noprefix", str(noprefix).lower())
    hunks = git.diff_hunks(repo.path, "HEAD~1", "HEAD")
    assert {h.file for h in hunks} == set(names)
    assert {h.old_file for h in hunks} == set(names)


def test_unreadable_untracked_python_file_does_not_abort_the_diff(tmp_path):
    r = Repo(tmp_path / "repo")
    r.commit({"pkg/__init__.py": "", "pkg/a.py": SMALL}, "v1")
    try:
        os.symlink(tmp_path / "does-not-exist.py", r.path / "broken.py")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available here")
    (r.path / "pkg" / "new.py").write_text("x = 1\ny = 2\n")
    hunks = git.diff_hunks(r.path, "HEAD", git.WORKTREE)
    assert [h.file for h in hunks] == ["pkg/new.py"]  # the dangling link is skipped, not fatal


def test_adaptive_range_progress_is_not_numbered_against_the_whole_range(tmp_path):
    r = Repo(tmp_path / "repo")
    r.commit({"pkg/__init__.py": "", "pkg/a.py": SMALL}, "v1")
    r.commit({"pkg/a.py": SMALL + "# note\n"}, "v2")
    r.commit({"pkg/a.py": SMALL + "# more\n"}, "v3")
    r.commit({"pkg/a.py": BIG}, "v4")
    shas = r.git("rev-list", "--reverse", "HEAD").split()

    def messages(exhaustive: bool) -> list[str]:
        seen: list[str] = []
        settings = Settings(workload="call:pkg.a:run", runs=1, python=sys.executable)
        with api.Session(r.path, settings, use_cache=False, progress=seen.append) as s:
            api.range_(s, shas[0], shas[-1], exhaustive=exhaustive)
        return [m for m in seen if "measuring" in m]

    adaptive = messages(exhaustive=False)
    assert adaptive and not any(re.match(r"\[\d+/\d+\]", m) for m in adaptive)
    assert adaptive[0].startswith("commit 1: ")
    exhaustive = messages(exhaustive=True)  # its total is known, so the numbering stays
    assert [m.split()[0] for m in exhaustive] == ["[1/4]", "[2/4]", "[3/4]", "[4/4]"]
