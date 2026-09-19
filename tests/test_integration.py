"""End-to-end tests against generated fixture repos (see fixture_repo.py). Slow-ish: each
measurement spawns the project interpreter several times."""

from __future__ import annotations

import json
import math
import subprocess
import sys
from pathlib import Path

import pytest

from memblame import api, cli, git
from memblame.measure import Settings

sys.path.insert(0, str(Path(__file__).parent))
from fixture_repo import make_repo  # noqa: E402


@pytest.fixture(scope="session")
def planted(tmp_path_factory):
    return make_repo(tmp_path_factory.mktemp("planted") / "repo", "planted")


@pytest.fixture(scope="session")
def clean(tmp_path_factory):
    return make_repo(tmp_path_factory.mktemp("clean") / "repo", "clean")


def session(fx, workload=None, runs=2, cache=True, **kw) -> api.Session:
    settings = Settings(workload=workload or fx.workload, runs=runs, python=sys.executable, **kw)
    return api.Session(fx.path, settings, use_cache=cache, progress=lambda m: None)


def finding(out: dict, metric: str) -> dict | None:
    return next((f for f in out["findings"] if f["metric"] == metric), None)


def parent(fx, label: str) -> str:
    return fx.commits[fx.order[fx.order.index(label) - 1]]


def test_diff_blames_direct_regression(planted):
    with session(planted) as s:
        out = api.diff(s, parent(planted, "direct"), planted.commits["direct"])
    f = finding(out, "peak")
    assert f and f["delta"] > 20_000_000
    v = f["verdict"]
    assert v["kind"] == "direct"
    assert v["function"] == "shop/parse.py::load_rows"
    assert v["hunks"] and v["hunks"][0]["file"] == "shop/parse.py"
    assert v["hot_lines"][0]["file"] == "shop/parse.py"
    assert finding(out, "retained") is None


def test_diff_blames_retention_on_the_changed_caller(planted):
    """Memory is allocated in unchanged load_rows(); the change that keeps it is summarize()."""
    with session(planted) as s:
        out = api.diff(s, parent(planted, "retention"), planted.commits["retention"])
    f = finding(out, "retained")
    assert f and f["delta"] > 20_000_000
    assert f["verdict"]["kind"] == "direct"
    assert f["verdict"]["function"] == "shop/report.py::summarize"
    assert f["verdict"]["allocated_at"][0]["file"] == "shop/parse.py"
    assert finding(out, "peak") is None  # peak did not change: the rows existed before too


def test_range_finds_exactly_the_planted_commits_and_caches(planted):
    base, head = planted.commits["initial"], planted.commits["changelog"]
    with session(planted) as s:
        first = api.range_(s, base, head)
        fresh = s.measured
    with session(planted) as s:
        second = api.range_(s, base, head)
        assert s.measured == 0  # everything came from the cache
    assert fresh >= 1
    flagged = {(f["commit"], f["metric"]) for f in first["findings"]}
    assert flagged == {(planted.commits["direct"], "peak"),
                       (planted.commits["retention"], "retained")}
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)
    assert len(first["points"]) == len(planted.order)


def test_clean_history_has_no_findings(clean):
    with session(clean, runs=3) as s:
        out = api.range_(s, clean.commits["initial"], clean.commits["changelog"])
    assert out["findings"] == []


def test_noise_same_commit_stays_inside_band(clean):
    with session(clean, runs=3, cache=False) as s:
        out = api.diff(s, clean.commits["initial"], clean.commits["initial"])
    for unit in out["units"]:
        for m in unit["metrics"]:
            assert not m["significant"], m
            assert abs(m["delta"]) < m["band"] / 4


def test_bisect_finds_planted_commit_in_log_steps(planted):
    with session(planted) as s:
        out = api.bisect(s, planted.commits["initial"], planted.commits["docs2"],
                         threshold="+10MB")
    assert out["status"] == "found"
    assert out["culprit"]["sha"] == planted.commits["direct"]
    assert out["metric"] == "peak"
    assert out["steps"] <= math.ceil(math.log2(out["candidates"] + 1))
    assert out["findings"][0]["verdict"]["function"] == "shop/parse.py::load_rows"


def test_bisect_reports_no_regression_on_clean_history(clean):
    with session(clean) as s:
        out = api.bisect(s, clean.commits["initial"], clean.commits["changelog"])
    assert out["status"] == "no_regression"


def test_pytest_workload_measures_each_test(planted):
    with session(planted, workload="pytest:tests/test_app.py") as s:
        out = api.diff(s, parent(planted, "direct"), planted.commits["direct"])
    units = {u["name"]: u for u in out["units"]}
    assert set(units) == {"tests/test_app.py::test_pipeline", "tests/test_app.py::test_small"}
    flagged = {f["unit"] for f in out["findings"]}
    assert flagged == {"tests/test_app.py::test_pipeline"}
    assert out["findings"][0]["verdict"]["function"] == "shop/parse.py::load_rows"


def test_working_tree_changes_are_measured(tmp_path):
    fx = make_repo(tmp_path / "repo", "clean")
    parse = fx.path / "shop" / "parse.py"
    parse.write_text(parse.read_text().replace("i * 3))", "i * 3, [0] * 20))"))
    with session(fx) as s:
        out = api.diff(s, "HEAD", git.WORKTREE)
    assert out["head"]["sha"] == git.WORKTREE
    f = finding(out, "peak")
    assert f and f["verdict"]["kind"] == "direct"
    assert f["verdict"]["function"] == "shop/parse.py::load_rows"
    assert not (fx.path / ".memblame" / "cache").exists() or not any(
        p.name.startswith("WORKTREE") for p in (fx.path / ".memblame" / "cache").iterdir())


def test_editable_install_elsewhere_is_invalid_environment(tmp_path):
    """Imports resolving outside the checkout must be refused, not measured."""
    fx = make_repo(tmp_path / "repo", "planted", layout="src")
    # Simulate `pip install -e .` of the main checkout: its src/ is importable, and the user
    # forgot to tell memblame about the src/ layout (pythonpath ".").
    env = {"PYTHONPATH": str(fx.path / "src")}
    with session(fx, pythonpath=["."], extra_env=env) as s:
        out = api.diff(s, fx.commits["docstring"], fx.commits["direct"])
    assert out["valid"] is False
    assert any("INVALID ENVIRONMENT" in w for w in out["warnings"])
    assert "findings" not in out
    # With the right setting (the default auto-detects src/) it works.
    with session(fx, extra_env=env) as s:
        out = api.diff(s, fx.commits["docstring"], fx.commits["direct"])
    assert out["valid"] is True
    assert finding(out, "peak")["verdict"]["function"] == "src/shop/parse.py::load_rows"


def test_worktrees_are_cleaned_up(planted):
    with session(planted, cache=False) as s:
        api.run(s, planted.commits["docs"])
    listing = git.git(planted.path, "worktree", "list")
    assert len(listing.strip().splitlines()) == 1


def test_cli_json_and_exit_codes(planted, capsys):
    base, head = parent(planted, "direct"), planted.commits["direct"]
    code = cli.main(["diff", base, head, "-C", str(planted.path), "-w", planted.workload,
                     "--runs", "2", "--python", sys.executable, "--json"])
    out = json.loads(capsys.readouterr().out)
    assert code == 3  # regression found
    assert out["schema"] == 1 and out["kind"] == "diff"
    code = cli.main(["diff", planted.commits["initial"], planted.commits["docs"], "-C",
                     str(planted.path), "-w", planted.workload, "--runs", "2", "--python",
                     sys.executable])
    assert code == 0
    assert "no significant change" in capsys.readouterr().out


def test_workload_error_is_reported(planted):
    with session(planted, workload="call:shop.app:does_not_exist") as s:
        out = api.run(s, planted.commits["initial"])
    unit = out["result"]["units"]["workload"]
    assert unit["outcome"] == "error"
    assert "does_not_exist" in unit["error"]


def test_fixture_script_prints_history(tmp_path):
    out = subprocess.run([sys.executable, str(Path(__file__).parent / "fixture_repo.py"),
                          str(tmp_path / "r")], capture_output=True, text=True, check=True)
    assert "include raw payload in rows" in out.stdout
