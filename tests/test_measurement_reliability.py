"""Regression checks for revision selection, invalid samples and failure exit codes."""

import copy
import json
import shlex
import sys

import pytest
from test_edge_cases import Repo, session

from memblame import api, cli, measure


def test_absolute_repo_script_tracks_revision_and_preserves_arguments(tmp_path):
    repo = Repo(tmp_path / "repo")
    rel = "bench scripts/it's a benchmark.py"
    source = ("import sys\nassert sys.argv[1:] == ['two words', \"it's quoted\"]\n"
              "x = bytearray({})\n")
    base = repo.commit({rel: source.format(100_000)}, "small")
    head = repo.commit({rel: source.format(4_000_000)}, "large")
    repo.write({rel: source.format(9_000_000)})
    workload = "script:" + " ".join(shlex.quote(s) for s in
                                    [str(repo.path / rel), "two words", "it's quoted"])
    with session(repo, workload, cache=True) as s:
        out = api.diff(s, base, head)
    assert out["valid"], out["warnings"]
    before = out["results"]["base"]["units"]["workload"]
    after = out["results"]["head"]["units"]["workload"]
    assert before["outcome"] == after["outcome"] == "passed"
    assert before["peak"]["median"] < 1_000_000
    assert 4_000_000 < after["peak"]["median"] < 5_000_000
    with session(repo, workload, cache=True) as s:
        _, cached = s.result(base)
        assert s.measured == 0
        assert cached["units"]["workload"]["peak"] == before["peak"]


@pytest.mark.parametrize("command", ["run", "diff", "range", "bisect"])
@pytest.mark.parametrize("as_json", [False, True])
def test_crashed_workload_exits_nonzero(tmp_path, capsys, command, as_json):
    repo = Repo(tmp_path / "repo")
    repo.commit({"bench.py": "x = 1\n"}, "good")
    repo.commit({"bench.py": "import os\nos._exit(7)\n"}, "crash")
    args = {"run": ["HEAD"], "diff": ["HEAD~1", "HEAD"],
            "range": ["HEAD~1..HEAD", "--all"], "bisect": ["--good", "HEAD~1"]}[command]
    code = cli.main([command, *args, "-C", str(repo.path), "-w", "script:bench.py",
                     "--python", sys.executable, "--runs", "1", "--no-cache",
                     *(["--json"] if as_json else [])])
    captured = capsys.readouterr()
    assert code == 1
    assert "runner crashed (exit 7)" in captured.out
    assert "Traceback" not in captured.out + captured.err
    if as_json:
        assert json.loads(captured.out)["kind"] == command


@pytest.mark.parametrize("command", ["run", "diff", "range", "bisect"])
def test_python_exception_is_not_a_successful_check(tmp_path, capsys, command):
    repo = Repo(tmp_path / "repo")
    repo.commit({"bench.py": "x = 1\n"}, "good")
    repo.commit({"bench.py": "raise ValueError('broken workload')\n"}, "broken")
    args = {"run": ["HEAD"], "diff": ["HEAD~1", "HEAD"],
            "range": ["HEAD~1..HEAD"], "bisect": ["--good", "HEAD~1"]}[command]
    code = cli.main([command, *args, "-C", str(repo.path), "-w", "script:bench.py",
                     "--python", sys.executable, "--runs", "1", "--no-cache", "--json"])
    out = json.loads(capsys.readouterr().out)
    assert code == 1
    assert any("broken workload" in warning for warning in out["warnings"])


def sample():
    summary = {"coverage": 1, "total": 100_000, "functions": [], "lines": []}
    return {"python": "3.12", "executable": sys.executable, "platform": sys.platform,
            "env_problems": [], "exit_code": 0, "functions": {},
            "units": [{"name": "workload", "outcome": "passed", "peak_bytes": 100_000,
                       "end_bytes": 1000, "duration_s": 0.01,
                       "at_peak": summary, "retained": summary}]}


@pytest.mark.parametrize("change", ["outcome", "units", "exit_code", "environment"])
def test_unreliable_samples_are_not_cached(tmp_path, monkeypatch, change):
    repo = Repo(tmp_path / "repo")
    sha = repo.commit({"bench.py": "x = 1\n"}, "initial")
    first, second = sample(), sample()
    if change == "outcome":
        first["units"][0]["outcome"] = "failed"
    elif change == "units":
        second["units"] = []
    elif change == "exit_code":
        second["exit_code"] = 2
    else:
        second["env_problems"] = ["pkg imported from outside checkout"]
    samples = iter([first, second])
    monkeypatch.setattr(measure, "_run_once", lambda *a, **kw: next(samples))
    with session(repo, "script:bench.py", cache=True) as s:
        _, result = s.result(sha)
        assert not result["valid"]
        assert s.cache.get(sha) is None
    if change == "environment":
        assert result["env_problems"] == second["env_problems"]
    else:
        assert "inconsistent workload" in result["error"]
        assert result["units"] == {}  # no medians combining incompatible runs


@pytest.mark.parametrize("mode", ["poll", "hook"])
@pytest.mark.parametrize("change", ["outcome", "units", "environment"])
def test_inconsistent_attribution_is_not_merged(tmp_path, monkeypatch, mode, change):
    monkeypatch.setattr(measure, "_run_once", lambda *a, **kw: sample())
    settings = measure.Settings("script:bench.py", runs=1)
    result = measure.measure(sys.executable, tmp_path, settings, attribute=False)
    original = copy.deepcopy(result)
    deep = sample()
    if change == "outcome":
        deep["units"][0]["outcome"] = "failed"
    elif change == "units":
        deep["units"] = []
    else:
        deep["env_problems"] = ["pkg imported from outside checkout"]
    runs = [deep]
    if mode == "hook":
        poll = sample()
        poll["units"][0]["at_peak"]["coverage"] = 0.1
        runs.insert(0, poll)
    samples = iter(runs)
    monkeypatch.setattr(measure, "_run_once", lambda *a, **kw: next(samples))
    with pytest.raises(measure.MeasureError, match="attribution"):
        measure.add_attribution(sys.executable, tmp_path, settings, result)
    assert result == original


def test_no_collected_tests_are_a_failed_measurement(tmp_path, monkeypatch):
    empty = sample()
    empty.update(units=[], exit_code=5)
    monkeypatch.setattr(measure, "_run_once", lambda *a, **kw: empty)
    with pytest.raises(measure.MeasureError, match="no measurements"):
        measure.measure(sys.executable, tmp_path, measure.Settings("pytest:tests", runs=1))


@pytest.mark.parametrize("windows", [False, True])
def test_quoted_workload_arguments_round_trip(monkeypatch, windows):
    monkeypatch.setattr(measure.os, "name", "nt" if windows else "posix")
    args = ["bench scripts/it's a benchmark.py", 'tests/test_a.py::test_it[a "quote"]',
            r"C:\my dir\run.py", "", "a#b"]
    assert measure.split_args(" ".join(shlex.quote(s) for s in args)) == args
