"""Fast unit tests (no subprocesses, no git)."""

import textwrap
import tracemalloc
from pathlib import Path
from types import ModuleType

import pytest

from memblame import api, blame, git
from memblame.runner import (
    Attributor,
    _grouped_traces,
    _scopes_of,
    check_environment,
    innermost_scope,
    scopes_from_source,
)


def _alloc_inner():
    return [0] * 50_000


def _alloc_outer():
    return _alloc_inner()


def test_assumption_traceback_order_oldest_first():
    """PROJECT.md section 8.1: Traceback is oldest -> most recent, so tb[-1] is the allocator."""
    tracemalloc.start(10)
    try:
        keep = _alloc_outer()
        snap = tracemalloc.take_snapshot()
    finally:
        tracemalloc.stop()
    trace = max(snap.traces, key=lambda t: t.size)
    inner_line = _alloc_inner.__code__.co_firstlineno + 1
    assert trace.traceback[-1].lineno == inner_line
    assert len(keep) == 50_000


def test_assumption_snapshot_does_not_inflate_traced_memory():
    """Snapshotting at the peak must not inflate the numbers we measure.

    Only the small Snapshot wrapper objects are traced (~0.7 KB); the copied trace table is
    not, so the overhead must stay constant however many traces exist.
    """
    tracemalloc.start(10)
    try:
        data = [str(i) for i in range(50_000)]
        before = tracemalloc.get_traced_memory()
        snap = tracemalloc.take_snapshot()
        after = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert len(snap.traces) >= 50_000 and data
    assert after[0] - before[0] < 4096
    assert after[1] - before[1] < 4096


def test_fast_trace_grouping_matches_public_api(monkeypatch):
    tracemalloc.start(10)
    try:
        keep = [(f"x{i}", bytes(50)) for i in range(20_000)]
        snap = tracemalloc.take_snapshot()
    finally:
        tracemalloc.stop()
    root = str(Path(__file__).resolve().parents[1])
    fast = Attributor(root, set()).summarize(snap.traces._traces, 1)

    def public(_raw):
        for stat in snap.statistics("traceback"):
            frames = tuple((f.filename, f.lineno) for f in reversed(stat.traceback))
            yield frames, stat.size, stat.traceback.total_nframe

    monkeypatch.setattr("memblame.runner._grouped_traces", public)
    slow = Attributor(root, set()).summarize([], 1)
    # statistics("traceback") drops total_nframe, so "truncated" is only known on the fast path
    fast.pop("truncated"), slow.pop("truncated")
    assert fast == slow and keep and fast["total"] > 1_000_000
    assert fast["functions"], "the test's own frames must be attributed to the project"
    assert list(_grouped_traces(snap.traces._traces))


SOURCE = textwrap.dedent('''\
    import functools

    X = [1] * 10


    @functools.lru_cache
    def cached(n):
        return n


    class Loader:
        size = 3

        def load(self):
            def helper():
                return 1
            return helper()

        async def aload(self):
            return 2
''')


def test_scopes_and_innermost():
    scopes = scopes_from_source(SOURCE)
    names = {s[3]: s for s in scopes}
    assert set(names) == {"cached", "Loader", "Loader.load", "Loader.load.<locals>.helper",
                          "Loader.aload"}
    assert names["cached"][:3] == (7, 6, 8)  # def line, decorator line, end
    assert innermost_scope(scopes, 3) is None  # module level
    assert innermost_scope(scopes, 12)[3] == "Loader"  # class body
    assert innermost_scope(scopes, 16)[3] == "Loader.load.<locals>.helper"
    assert innermost_scope(scopes, 17)[3] == "Loader.load"
    assert innermost_scope(scopes, 20)[3] == "Loader.aload"
    assert scopes_from_source("def broken(:\n") == []


def test_attributor_ignores_pseudo_files(tmp_path):
    a = Attributor(str(tmp_path), set())
    assert a.project_rel("<frozen importlib._bootstrap>") is None
    assert a.project_rel("<string>") is None
    (tmp_path / "m.py").write_text("x = 1\n")
    assert a.project_rel(str(tmp_path / "m.py")) == "m.py"
    (tmp_path / ".venv").mkdir()
    assert a.project_rel(str(tmp_path / ".venv" / "lib.py")) is None


def test_environment_check_handles_shared_namespace_packages(tmp_path, monkeypatch):
    source = tmp_path / "src" / "shared_namespace"
    source.mkdir(parents=True)
    (source / "local.py").write_text("x = 1\n")
    local = ModuleType("shared_namespace.local")
    local.__file__ = "/outside/checkout/shared_namespace/local.py"
    external = ModuleType("shared_namespace.external")
    external.__file__ = "/outside/checkout/shared_namespace/external.py"
    monkeypatch.setitem(__import__("sys").modules, "shared_namespace.local", local)
    monkeypatch.setitem(__import__("sys").modules, "shared_namespace.external", external)

    problems = check_environment(str(tmp_path), [str(tmp_path / "src")])
    assert problems == [
        "shared_namespace.local imported from /outside/checkout/shared_namespace/local.py"
    ]


DIFF = """\
diff --git a/pkg/a.py b/pkg/a.py
--- a/pkg/a.py
+++ b/pkg/a.py
@@ -7 +7,2 @@ def load_rows(n):
-        x
+        y
+        z
@@ -20,3 +21,0 @@ def other():
diff --git a/old.py b/old.py
deleted file mode 100644
--- a/old.py
+++ /dev/null
@@ -1,2 +0,0 @@
"""


def test_source_too_deeply_nested_to_parse_yields_no_scopes():
    """Generated code (a chain of thousands of `+`) can be too deep for the parser.

    That raised RecursionError out of scopes_from_source, which aborted the whole analysis
    after both commits had already been measured. Unparsable means no scopes, as for a
    syntax error. (The source has no definitions, so [] is right whether or not the parse
    gave out: 3.12's parser raises, 3.9's and 3.14's succeed.)
    """
    generated = "TOTAL = " + "+".join(["1"] * 60_000) + "\n"
    assert scopes_from_source(generated) == []
    # A syntax error already behaved this way; keep them consistent.
    assert scopes_from_source("def (:\n") == []


def test_a_deep_tree_keeps_its_scopes():
    """The walk over a tree is iterative. Recursing over thousands of levels hit the recursion
    limit (1 000) and threw away every scope in the file, though the file had parsed fine.

    The tree is built by hand rather than parsed: how deep the parser will go is a property
    of the Python build and the platform (3.12 on Windows refuses a chain that macOS accepts),
    and this tests the walk, not the parser.
    """
    import ast

    tree = ast.parse("def after():\n    pass\n")
    deep = ast.Constant(value=1)
    for _ in range(5_000):
        deep = ast.BinOp(left=deep, op=ast.Add(), right=ast.Constant(value=1))
    tree.body.insert(0, ast.Expr(value=deep))
    assert [scope[3] for scope in _scopes_of(tree)] == ["after"]


def test_parsing_deep_source_does_not_depend_on_the_callers_stack(tmp_path):
    """On Python 3.9 `ast.parse` recurses in C without a depth check, so a long enough chain
    overflows the C stack and kills the interpreter -- no `except` can catch that. A stack is
    8 MB on Linux and macOS but 1 MB on Windows, where CI died with `Windows fatal exception:
    stack overflow` on 3.9. Reproduce that stack here on every platform: a caller thread
    with 1 MB, parsing 60 000 terms. Only the process surviving matters, so run it in one.
    """
    import os
    import subprocess
    import sys

    child = textwrap.dedent("""
        import threading
        from memblame.runner import scopes_from_source

        threading.stack_size(1 << 20)  # what Windows gives its main thread
        out = []

        def work():
            src = "TOTAL = " + "+".join(["1"] * 60_000) + "\\ndef after():\\n    return TOTAL\\n"
            out.append([scope[3] for scope in scopes_from_source(src)])

        thread = threading.Thread(target=work)
        thread.start()
        thread.join()
        print(out[0])
    """)
    script = tmp_path / "deep_parse.py"  # a file, not -c: no command-line quoting to trust
    script.write_text(child, encoding="utf-8")
    src_dir = Path(__file__).resolve().parents[1] / "src"
    inherited = os.environ.get("PYTHONPATH", "")
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(filter(None, [str(src_dir), inherited]))}
    proc = subprocess.run([sys.executable, str(script)], capture_output=True, text=True,
                          env=env, timeout=120)
    assert proc.returncode == 0, (proc.returncode, proc.stderr[-500:])
    # 3.12's parser gives up (RecursionError -> no scopes); 3.9's and 3.14's succeed.
    assert proc.stdout.strip() in ("[]", "['after']"), proc.stdout


@pytest.mark.parametrize("subject", [
    "plain subject",
    "holds \x1e a record separator",   # the byte this format used to end records with
    "holds \x1c \x1d \x85 too",         # other things str.splitlines() breaks on
    "holds \u2028 a line separator",
    "holds \x00 a nul",                # last field, so maxsplit keeps it whole
])
def test_commit_metadata_survives_control_bytes_in_the_subject(subject):
    """Only a newline is impossible in these fields; a subject may hold any other byte.

    Parsing records on \x1e (or with str.splitlines(), which also breaks on \x1c-\x1e,
    \x85 and U+2028/9) tore such a commit apart and failed with a bare unpack error.
    """
    record = "\x00".join(["a" * 40, "aaaaaaa", "An Author", "2026-01-01T00:00:00+00:00",
                          subject])
    commits = git._parse_commits(record + "\n")
    assert len(commits) == 1
    assert commits[0].subject == subject
    assert commits[0].author == "An Author"


def test_unparsable_commit_metadata_names_the_record():
    with pytest.raises(git.GitError, match="could not parse commit metadata"):
        git._parse_commits("not\x00enough\x00fields\n")


BODY_LOOKS_LIKE_A_HEADER = "\n".join([
    "diff --git a/bench.py b/bench.py",
    "--- a/bench.py",
    "+++ b/bench.py",
    "@@ -2,2 +2,2 @@",
    # Two deleted/added source lines that happen to read as a file-header pair. This file
    # is full of them: the DIFF fixture below is exactly such a Python string.
    '--- "unterminated sql comment',
    "+++ new marker",
    "@@ -9 +9 @@ def run():",
    "-    return [0] * 100000",
    "+    return [0] * 200000",
]) + "\n"


def test_hunk_body_is_never_mistaken_for_a_file_header():
    """Inside a hunk every line carries a -/+ prefix, so source lines starting with `-- `
    and `++ ` arrive as `--- ` and `+++ `. Reading those as file headers blamed later hunks
    on a nonexistent path, and an unterminated quote aborted the analysis outright."""
    hunks = git.parse_hunks(BODY_LOOKS_LIKE_A_HEADER)
    assert [(h.file, h.new_range) for h in hunks] == [("bench.py", (2, 3)), ("bench.py", (9, 9))]


@pytest.mark.parametrize("header", ['"a/unterminated.py', '"a/\\777.py"', '"'])
def test_unparsable_quoted_path_is_used_verbatim(header):
    """A name that is not a well-formed quoted path must never abort an analysis."""
    assert isinstance(git._diff_path(header), str)


def test_parse_hunks():
    hunks = git.parse_hunks(DIFF)
    assert [(h.file, h.new_range) for h in hunks] == [
        ("pkg/a.py", (7, 8)), ("pkg/a.py", (21, 22)), ("old.py", (0, 1))]
    assert [(h.old_file, h.old_range) for h in hunks] == [
        ("pkg/a.py", (7, 7)), ("pkg/a.py", (20, 22)), ("old.py", (1, 2))]
    assert hunks[0].header() == "@@ -7,1 +7,2 @@"


def test_noise_band():
    a = {"median": 100_000_000, "min": 99_000_000, "max": 101_000_000}
    b = {"median": 100_000_000, "min": 100_000_000, "max": 100_000_000}
    assert blame.noise_band(a, b) == 4_000_000  # 2 x spread beats 2% of peak
    tiny = {"median": 1000, "min": 1000, "max": 1000}
    assert blame.noise_band(tiny, tiny) == blame.MIN_BAND


@pytest.mark.parametrize("text,good,expected", [
    ("200MB", 50, 200_000_000),
    ("+20MB", 100_000_000, 120_000_000),
    ("+10%", 100_000_000, 110_000_000),
    ("1GiB", 0, 2**30),
    ("123", 0, 123),
])
def test_parse_threshold(text, good, expected):
    assert api.parse_threshold(text, good) == expected


@pytest.mark.parametrize("text", ["lots", "", "1.2.3MB", ".", "-5MB", "1e3", "nanMB", "infGB"])
def test_parse_threshold_rejects_garbage(text):
    """Every rejection must name the input and show an example, never leak a float() error."""
    with pytest.raises(ValueError, match="bad threshold"):
        api.parse_threshold(text, 0)
    with pytest.raises(ValueError, match="bad threshold"):
        api.check_threshold(text)


def test_check_threshold_accepts_what_parse_threshold_accepts():
    for text in ("200MB", "+20MB", "+10%", "1GiB", "123"):
        api.check_threshold(text)  # no exception


def _bisect_result(values):
    def stats(value):
        return {"median": value, "min": value, "max": value, "samples": [value]}

    return {"units": {
        name: {"peak": stats(peak), "end": stats(retained), "outcome": "passed"}
        for name, (peak, retained) in values.items()
    }}


def test_explicit_bisect_threshold_selects_the_unit_that_crosses_it():
    good = _bisect_result({"large-relative": (100_000_000, 1_000_000),
                           "crosses": (190_000_000, 1_000_000)})
    bad = _bisect_result({"large-relative": (150_000_000, 1_000_000),
                          "crosses": (210_000_000, 1_000_000)})

    target = api._pick_target(good, bad, None, "peak", "200MB")

    assert target[1:] == ("crosses", "peak", 200_000_000)


def test_explicit_bisect_threshold_can_be_smaller_than_the_noise_band():
    good = _bisect_result({"workload": (100_000, 10_000)})
    bad = _bisect_result({"workload": (110_000, 10_000)})

    target = api._pick_target(good, bad, "workload", "peak", "+1KB")

    assert target[1:] == ("workload", "peak", 101_000)


def test_property_getter_and_setter_share_one_range(tmp_path):
    src = textwrap.dedent('''\
        class State:
            @property
            def src(self):
                return self._src

            @src.setter
            def src(self, value):
                self._src = value
                self.codes = tuple(ord(c) for c in value)
    ''')
    (tmp_path / "m.py").write_text(src)
    a = Attributor(str(tmp_path), set())
    fid, _ = a.frame(str(tmp_path / "m.py"), 9)  # inside the setter
    info = a.functions[fid]
    assert info["qualname"] == "State.src"
    assert (info["start"], info["end"]) == (2, 9)  # getter decorator .. setter end


def test_split_args_keeps_windows_paths(monkeypatch):
    from memblame import measure

    assert measure.split_args("run.py --n '1 2'") == ["run.py", "--n", "1 2"]
    monkeypatch.setattr(measure.os, "name", "nt")
    assert measure.split_args(r'C:\bench\run.py --out "C:\my dir\x.txt"') == [
        r"C:\bench\run.py", "--out", r"C:\my dir\x.txt"]


def test_project_module_scan_skips_virtualenvs_and_duplicate_directories(tmp_path):
    from memblame.runner import _project_modules

    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("")
    (tmp_path / "pkg" / "mod.py").write_text("")
    env = tmp_path / "customenv"  # not named .venv/venv/env: recognised by pyvenv.cfg
    (env / "Lib" / "libs").mkdir(parents=True)
    (env / "pyvenv.cfg").write_text("home = /usr/bin\n")
    (env / "Lib" / "libs" / "vendored.py").write_text("")
    names = _project_modules([str(tmp_path), str(tmp_path), str(tmp_path / ".")])
    assert {"pkg", "pkg.mod"} <= names
    assert not any(n.startswith("customenv") for n in names)


def test_extension_version_matches_package():
    """The VSIX bundles this engine, so the two version strings must not drift."""
    import json

    import memblame

    manifest = Path(__file__).parent.parent / "vscode-ext" / "package.json"
    if not manifest.exists():  # the sdist ships tests but not the extension
        pytest.skip("vscode-ext/package.json not present")
    assert json.loads(manifest.read_text(encoding="utf-8"))["version"] == memblame.__version__


def test_package_ships_the_py_typed_marker():
    root = Path(__file__).resolve().parents[1]
    assert (root / "src" / "memblame" / "py.typed").is_file()
    assert "py.typed" in (root / "pyproject.toml").read_text()
