"""Fast unit tests (no subprocesses, no git)."""

import textwrap
import tracemalloc

import pytest

from memblame import api, blame, git
from memblame.runner import Attributor, _grouped_traces, innermost_scope, scopes_from_source


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
    root = __file__.rsplit("/", 2)[0]
    fast = Attributor(root, set()).summarize(snap.traces._traces, 1)

    def public(_raw):
        for stat in snap.statistics("traceback"):
            frames = tuple((f.filename, f.lineno) for f in reversed(stat.traceback))
            yield frames, stat.size, stat.traceback.total_nframe

    monkeypatch.setattr("memblame.runner._grouped_traces", public)
    slow = Attributor(root, set()).summarize([], 1)
    assert fast == slow and keep and fast["total"] > 1_000_000
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


def test_parse_threshold_rejects_garbage():
    with pytest.raises(ValueError):
        api.parse_threshold("lots", 0)


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
