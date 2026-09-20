"""CLI commands and their human-readable output, configuration, interpreter discovery and
failure paths that the other suites do not reach."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from memblame import api, artifact, cli, measure, report
from memblame.contract import validate_output

sys.path.insert(0, str(Path(__file__).parent))
from fixture_repo import make_repo  # noqa: E402
from test_edge_cases import BIG, SMALL, Repo, session  # noqa: E402

PY = ["--python", sys.executable, "--runs", "2"]


@pytest.fixture(scope="module")
def planted(tmp_path_factory):
    return make_repo(tmp_path_factory.mktemp("cli") / "repo", "planted")


def run_cli(capsys, *args: str) -> tuple[int, str, str]:
    code = cli.main(list(args))
    out = capsys.readouterr()
    return code, out.out, out.err


# ------------------------------------------------------------------ the entry points


def test_python_dash_m_memblame_is_the_extensions_entry_point():
    src = Path(__file__).resolve().parents[1] / "src"
    out = subprocess.run([sys.executable, "-m", "memblame", "--version"], capture_output=True,
                         text=True, env={**os.environ, "PYTHONPATH": str(src)}, check=True,
                         cwd=Path(__file__).parent)  # not the repo root: must use PYTHONPATH
    assert out.stdout.startswith("memblame ")


# ------------------------------------------------------------------ every command, as text


def test_run_command_text_lists_top_functions(planted, capsys):
    code, out, _ = run_cli(capsys, "run", planted.commits["direct"], "-C", str(planted.path),
                           "-w", planted.workload, *PY)
    assert code == 0
    assert "include raw payload in rows" in out
    assert "shop/parse.py::load_rows" in out


def test_range_command_text_adaptive_and_all(planted, capsys):
    base, head = planted.commits["initial"], planted.commits["changelog"]
    code, out, _ = run_cli(capsys, "range", f"{base}..{head}", "-C", str(planted.path),
                           "-w", planted.workload, *PY)
    assert code == 3  # regressions found
    assert "measured" in out and "not measured" in out
    assert "direct: shop/parse.py:4 load_rows()" in out
    assert "memory allocated at shop/parse.py:8" in out
    code, out, _ = run_cli(capsys, "range", f"{base}..{head}", "--all", "-C", str(planted.path),
                           "-w", planted.workload, *PY)
    assert code == 3 and "not measured" not in out


def test_bisect_command_text_found_and_no_regression(planted, capsys):
    code, out, _ = run_cli(capsys, "bisect", "--good", planted.commits["initial"], "--bad",
                           planted.commits["docs2"], "--metric", "peak", "--threshold", "+10MB",
                           "-C", str(planted.path), "-w", planted.workload, *PY)
    assert code == 3
    assert "Threshold crossing" in out and "include raw payload in rows" in out
    code, out, _ = run_cli(capsys, "bisect", "--good", planted.commits["initial"], "--bad",
                           planted.commits["docstring"], "-C", str(planted.path),
                           "-w", planted.workload, *PY)
    assert code == 0 and out.startswith("bisect:")


def test_bisect_unit_option_and_json(planted, capsys):
    code, out, _ = run_cli(capsys, "bisect", "--good", planted.commits["initial"], "--bad",
                           planted.commits["changelog"], "--unit", "workload", "--metric",
                           "retained", "-C", str(planted.path), "-w", planted.workload,
                           "--json", *PY)
    d = json.loads(out)
    assert code == 3 and d["metric"] == "retained" and d["unit"] == "workload"
    assert d["culprit"]["subject"] == "cache summarize results"


def test_bisect_rejects_unknown_unit_and_already_exceeded_threshold(planted, capsys):
    common = ["-C", str(planted.path), "-w", planted.workload, "--json", *PY]
    endpoints = ["--good", planted.commits["initial"], "--bad", planted.commits["changelog"]]

    code, out, err = run_cli(capsys, "bisect", *endpoints, "--unit", "not-a-unit", *common)
    assert code == 1 and "unknown unit 'not-a-unit'" in err
    assert json.loads(out)["kind"] == "error"

    code, out, err = run_cli(capsys, "bisect", *endpoints, "--threshold", "1B", *common)
    assert code == 1 and "good commit" in err and "already exceeds the threshold" in err
    assert json.loads(out)["kind"] == "error"


def test_malformed_threshold_is_rejected_before_anything_is_measured(planted, capsys, monkeypatch):
    """A typo in --threshold must not cost two full endpoint measurements first."""
    monkeypatch.setattr(api, "bisect", lambda *a, **k: pytest.fail("measured despite a bad "
                                                                  "threshold"))
    code, out, err = run_cli(capsys, "bisect", "--good", planted.commits["initial"],
                             "--bad", planted.commits["changelog"], "--threshold", "1.2.3MB",
                             "-C", str(planted.path), "-w", planted.workload, "--json", *PY)
    assert code == 1
    assert "bad threshold '1.2.3MB'" in err and "200MB" in err
    assert json.loads(out)["kind"] == "error"


def _report_header(kind):
    return {"schema": 1, "kind": kind, "repo": "/repo", "workload": "call:pkg:run",
            "python": "/python", "warnings": []}


def _report_commit(short, subject):
    return {"sha": short * 6, "short": short, "subject": subject, "author": "A. Dev"}


def _report_unit(peak, retained):
    return {"outcome": "passed", "peak": {"median": peak, "min": peak, "max": peak},
            "end": {"median": retained, "min": retained, "max": retained}}


@pytest.mark.parametrize("data,needle", [
    ({**_report_header("run"), "commit": _report_commit("aaaaaaa", "measure"),
      "result": {"valid": True, "runs": 1,
                 "units": {"workload": _report_unit(1_000_000, 100_000)}}},
     "Memory measurement"),
    ({**_report_header("diff"), "base": _report_commit("aaaaaaa", "before"),
      "head": _report_commit("bbbbbbb", "after"), "valid": True, "units": [],
      "findings": []}, "Memory comparison"),
    ({**_report_header("range"), "mode": "exhaustive", "measured": 2,
      "points": [
          {"commit": _report_commit("aaaaaaa", "before"), "measured": True,
           "valid": True, "units": {"workload": _report_unit(1_000_000, 100_000)}},
          {"commit": _report_commit("bbbbbbb", "after <unsafe>"), "measured": True,
           "valid": True, "units": {"workload": _report_unit(2_000_000, 200_000)}},
      ], "findings": []}, "Memory history"),
    ({**_report_header("bisect"), "status": "found", "unit": "workload",
      "metric": "peak", "threshold": 1_500_000, "steps": 1,
      "culprit": _report_commit("bbbbbbb", "after"), "findings": [],
      "measurements": [
          {"commit": _report_commit("aaaaaaa", "before"), "value": 1_000_000,
           "bad": False},
          {"commit": _report_commit("bbbbbbb", "after"), "value": 2_000_000,
           "bad": True},
      ]}, "Threshold crossing"),
])
def test_portable_report_renderers_cover_every_command(data, needle):
    markdown = artifact.markdown(data)
    rendered_html = artifact.html_report(data)
    assert needle in markdown and needle in rendered_html
    assert rendered_html.startswith("<!doctype html>")
    assert "Machine-readable result" in rendered_html
    assert "<unsafe>" not in rendered_html
    assert "<unsafe>" not in markdown
    if data["kind"] == "range":
        assert "<svg" in rendered_html and "line peak" in rendered_html


def test_public_result_contract_rejects_schema_and_status_breaks():
    good = {**_report_header("diff"), "measurement_status": "complete",
            "base": _report_commit("aaaaaaa", "before"),
            "head": _report_commit("bbbbbbb", "after")}
    assert validate_output(good)["kind"] == "diff"
    with pytest.raises(ValueError, match="schema"):
        validate_output({**good, "schema": 2})
    with pytest.raises(ValueError, match="measurement_status"):
        validate_output({**good, "measurement_status": "maybe"})


def test_cli_writes_markdown_and_html_reports(planted, tmp_path, capsys):
    args = ["diff", planted.commits["docstring"], planted.commits["direct"],
            "-C", str(planted.path), "-w", planted.workload, *PY]
    markdown_path = tmp_path / "reports" / "memblame.md"
    code, out, err = run_cli(capsys, *args, "--report", "md", "-o", str(markdown_path))
    assert code == 3 and out == ""
    assert "wrote MD report" in err
    assert "memory regression detected" in markdown_path.read_text()

    html_path = tmp_path / "reports" / "memblame.html"
    code, out, err = run_cli(capsys, *args, "--report", "html", "-o", str(html_path))
    assert code == 3 and out == ""
    assert "wrote HTML report" in err
    rendered = html_path.read_text()
    assert rendered.startswith("<!doctype html>")
    assert "include raw payload in rows" in rendered
    assert "shop/parse.py:4" in rendered


# ------------------------------------------------------------------ errors are clean


@pytest.mark.parametrize("args,message", [
    (["-w", "bogus"], "must start with pytest:, script: or call:"),
    (["-w", "call:a:b", "--runs", "0"], "--runs must be positive"),
    (["-w", "call:a:b", "--nframe", "-1"], "--nframe must be positive"),
    ([], "no workload"),
])
def test_bad_arguments_give_one_line_errors_and_json(planted, capsys, args, message):
    code, _, err = run_cli(capsys, "diff", "-C", str(planted.path), *args)
    assert code == 1 and message in err and "Traceback" not in err
    code, out, _ = run_cli(capsys, "diff", "-C", str(planted.path), "--json", *args)
    assert json.loads(out.splitlines()[0])["kind"] == "error"


def test_not_a_git_repository(tmp_path, capsys):
    code, _, err = run_cli(capsys, "diff", "-C", str(tmp_path), "-w", "call:a:b")
    assert code == 2 and "not inside a git repository" in err


# ------------------------------------------------------------------ configuration


def test_config_from_pyproject_is_used(tmp_path, capsys):
    pytest.importorskip("tomllib" if sys.version_info >= (3, 11) else "tomli")
    r = Repo(tmp_path / "repo")
    r.commit({"pkg/__init__.py": "", "pkg/a.py": SMALL,
              "pyproject.toml": '[tool.memblame]\nworkload = "call:pkg.a:run"\nruns = 2\n'
                                'nframe = 8\ntimeout = 60\n'}, "v1")
    r.commit({"pkg/a.py": BIG}, "v2")
    code, out, _ = run_cli(capsys, "diff", "HEAD~1", "HEAD", "-C", str(r.path), "--json",
                           "--python", sys.executable)
    d = json.loads(out)
    assert code == 3 and d["workload"] == "call:pkg.a:run"
    assert d["findings"][0]["verdict"]["function"] == "pkg/a.py::run"


def test_memblame_toml_wins_and_bad_config_is_reported(tmp_path, capsys):
    pytest.importorskip("tomllib" if sys.version_info >= (3, 11) else "tomli")
    (tmp_path / "memblame.toml").write_text('workload = "call:x:y"\nfoo = 1\n')
    (tmp_path / "pyproject.toml").write_text('[tool.memblame]\nworkload = "call:other:z"\n')
    assert cli.load_config(tmp_path) == {"workload": "call:x:y"}
    assert "unknown keys in memblame.toml: foo" in capsys.readouterr().err
    (tmp_path / "memblame.toml").write_text("workload = = broken")
    assert cli.load_config(tmp_path) == {"workload": "call:other:z"}
    assert "ignoring memblame.toml" in capsys.readouterr().err


def test_config_types_are_checked(tmp_path, capsys):
    pytest.importorskip("tomllib" if sys.version_info >= (3, 11) else "tomli")
    r = Repo(tmp_path / "repo")
    r.commit({"pyproject.toml": '[tool.memblame]\nworkload = "call:a:b"\nruns = "3"\n'}, "v1")
    code, _, err = run_cli(capsys, "diff", "-C", str(r.path))
    assert code == 1 and "config key 'runs' must be int" in err


def test_cache_inputs_are_read_from_config_and_cli(tmp_path):
    args = cli.build_parser().parse_args([
        "run", "-w", "script:bench.py", "--cache-env", "SIZE",
        "--cache-input", "../data.json",
    ])
    settings = cli.settings_from(args, {}, tmp_path)
    assert settings.cache_env == ["SIZE"]
    assert settings.cache_inputs == ["../data.json"]

    args = cli.build_parser().parse_args(["run", "-w", "script:bench.py"])
    settings = cli.settings_from(args, {"cache_env": "SIZE", "cache_inputs": ["data/"]},
                                 tmp_path)
    assert settings.cache_env == ["SIZE"]
    assert settings.cache_inputs == ["data/"]


def test_config_without_toml_parser_is_not_silently_ignored(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(cli, "_toml", lambda: None)
    (tmp_path / "pyproject.toml").write_text('[tool.memblame]\nworkload = "call:a:b"\n')
    assert cli.load_config(tmp_path) == {}
    assert "needs Python 3.11+ or `pip install tomli`" in capsys.readouterr().err
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "x"\n')  # no memblame section
    assert cli.load_config(tmp_path) == {}
    assert capsys.readouterr().err == ""


# ------------------------------------------------------------------ interpreter discovery


def _fake_python(base: Path) -> Path:
    exe = base / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    exe.parent.mkdir(parents=True)
    exe.write_text("")
    return exe


def test_find_python_prefers_explicit_then_active_env_then_repo_venv(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    monkeypatch.delenv("CONDA_PREFIX", raising=False)
    assert measure.find_python(repo) == sys.executable  # nothing found
    local = _fake_python(repo / ".venv")
    assert measure.find_python(repo) == str(local)
    conda = _fake_python(tmp_path / "conda")
    monkeypatch.setenv("CONDA_PREFIX", str(conda.parents[1]))
    assert measure.find_python(repo) == str(conda)
    active = _fake_python(tmp_path / "active")
    monkeypatch.setenv("VIRTUAL_ENV", str(active.parents[1]))
    assert measure.find_python(repo) == str(active)
    assert measure.find_python(repo, "/explicit/python") == "/explicit/python"


def test_unusable_interpreter_is_a_clear_error(tmp_path, capsys):
    r = Repo(tmp_path / "repo")
    r.commit({"a.py": ""}, "v1")
    for extra in ([], ["--no-cache"]):  # without the cache it must still fail up front
        code, _, err = run_cli(capsys, "run", "HEAD", "-C", str(r.path), "-w", "call:a:b",
                               "--python", str(tmp_path / "no-such-python"), *extra)
        assert code == 1 and "cannot run project interpreter" in err and "Traceback" not in err
    not_python = tmp_path / "not-python"
    not_python.write_text("#!/bin/sh\necho hello\n")
    not_python.chmod(0o755)
    if sys.platform != "win32":
        code, _, err = run_cli(capsys, "run", "HEAD", "-C", str(r.path), "-w", "call:a:b",
                               "--python", str(not_python), "--no-cache")
        assert code == 1 and "does not look like a Python interpreter" in err


def test_external_script_hash_only_for_files_outside_the_repo(tmp_path):
    repo = tmp_path / "repo"
    (repo / "bench").mkdir(parents=True)
    (repo / "bench" / "run.py").write_text("x = 1\n")
    outside = tmp_path / "bench.py"
    outside.write_text("x = 1\n")
    h = measure.external_script_hash
    assert h(repo, "script:bench/run.py") == ""  # relative: pinned by the commit
    assert h(repo, f"script:{repo / 'bench' / 'run.py'}") == ""  # inside the repo
    assert h(repo, "call:pkg:main") == ""
    assert len(h(repo, f"script:{outside} --n 3")) == 16
    assert h(repo, f"script:{tmp_path / 'gone.py'}") == "missing"


# ------------------------------------------------------------------ failure paths


def test_workload_that_kills_the_process_is_skipped(tmp_path):
    r = Repo(tmp_path / "repo")
    r.commit({"pkg/__init__.py": "", "pkg/a.py": SMALL}, "v1")
    r.commit({"pkg/a.py": "import os\n\ndef run():\n    os._exit(3)\n"}, "hard exit")
    r.commit({"pkg/a.py": SMALL + "\n"}, "v3")
    with session(r, "call:pkg.a:run") as s:
        out = api.range_(s, "HEAD~2", "HEAD", exhaustive=True)
    assert [p["valid"] for p in out["points"]] == [True, False, True]
    assert any("runner crashed (exit 3)" in w for w in out["warnings"])
    text = report.format_range(out)
    # An unmeasurable commit is an incomplete measurement: never a "no changes" all-clear.
    assert out["measurement_status"] == "error"
    assert "no memory-regression conclusion" in text and "SKIPPED" in text
    assert "No significant memory changes." not in text


def test_attribution_failure_keeps_the_numbers(tmp_path):
    """The deep (attribution) run can fail while the fast runs succeed."""
    r = Repo(tmp_path / "repo")
    crash_when_deep = ("import os, _tracemalloc\n\ndef run():\n"
                       "    keep = [bytes(100) for _ in range({n})]\n"
                       "    if _tracemalloc.get_traceback_limit() > 1:\n        os._exit(1)\n"
                       "    return len(keep)\n")
    r.commit({"pkg/__init__.py": "", "pkg/a.py": crash_when_deep.format(n=1000)}, "v1")
    r.commit({"pkg/a.py": crash_when_deep.format(n=100_000)}, "v2")
    with session(r, "call:pkg.a:run") as s:
        out = api.diff(s, "HEAD~1", "HEAD")
    f = out["findings"][0]
    assert f["metric"] == "peak" and f["delta"] > 5_000_000
    assert f["verdict"]["kind"] == "unattributed"
    assert any("attribution run failed" in w for w in out["warnings"])
    assert "unattributed" in report.format_diff(out)


def test_bisect_reports_an_unmeasurable_endpoint(tmp_path):
    r = Repo(tmp_path / "repo")
    r.commit({"pkg/__init__.py": "", "pkg/a.py": "import os\n\ndef run():\n    os._exit(1)\n"},
             "broken good")
    r.commit({"pkg/a.py": BIG}, "bad")
    with session(r, "call:pkg.a:run") as s:
        out = api.bisect(s, "HEAD~1", "HEAD")
    assert out["status"] == "error" and "cannot measure" in out["message"]
    assert "cannot measure" in report.format_bisect(out)


def test_outcome_change_is_shown_not_compared(tmp_path):
    r = Repo(tmp_path / "repo")
    r.commit({"pkg/__init__.py": "", "pkg/a.py": BIG}, "v1")
    r.commit({"pkg/a.py": "def run():\n    raise ValueError('now broken')\n"}, "v2")
    with session(r, "call:pkg.a:run") as s:
        out = api.diff(s, "HEAD~1", "HEAD")
    assert out["findings"] == []
    text = report.format_diff(out)
    assert "outcome changed passed -> error; memory not compared" in text
    assert "ValueError: now broken" in text


def test_relative_interpreter_paths_survive_the_temporary_checkout(tmp_path, capsys,
                                                                   monkeypatch):
    """The runner starts inside a temporary worktree, where ".venv/bin/python" does not
    exist; relative paths must be resolved before that (CLI: cwd, config: repo root)."""
    pytest.importorskip("tomllib" if sys.version_info >= (3, 11) else "tomli")
    r = Repo(tmp_path / "repo")
    link = r.path / "venvpy"
    try:
        link.symlink_to(sys.executable)
    except OSError:
        pytest.skip("symlinks not available")
    r.commit({"pkg/__init__.py": "", "pkg/a.py": SMALL, ".gitignore": "venvpy\n",
              "memblame.toml": 'workload = "call:pkg.a:run"\npython = "./venvpy"\nruns = 2\n'},
             "v1")
    code, out, err = run_cli(capsys, "run", "HEAD", "-C", str(r.path), "--no-cache")
    assert code == 0, err
    monkeypatch.chdir(r.path)
    code, out, err = run_cli(capsys, "run", "HEAD", "--python", "./venvpy", "--no-cache")
    assert code == 0, err


def test_missing_pytest_is_a_setup_error_not_a_skipped_commit(tmp_path, capsys):
    """Found in CI: the interpreter had no pytest, so every commit was 'skipped'."""
    shadow = tmp_path / "shadow"
    shadow.mkdir()
    (shadow / "pytest.py").write_text("raise ImportError(\"No module named 'pytest'\")\n")
    r = Repo(tmp_path / "repo")
    r.commit({"tests/test_a.py": "def test_a():\n    assert 1\n"}, "v1")
    with pytest.raises(api.MeasureError, match="pytest is not installed"):
        with session(r, "pytest:tests/test_a.py", extra_env={"PYTHONPATH": str(shadow)}) as s:
            api.run(s, "HEAD")


@pytest.mark.parametrize("config,message", [
    ('workload = 42', "config key 'workload' must be str"),
    ('workload = "call:a:b"\ntimeout = -1', "positive and finite"),
    ('workload = "call:a:b"\ntimeout = 0', "positive and finite"),
    ('workload = "call:a:b"\ntimeout = nan', "positive and finite"),
    ('workload = "call:a:b"\ntimeout = inf', "positive and finite"),
])
def test_invalid_config_returns_structured_error(tmp_path, capsys, config, message):
    pytest.importorskip("tomllib" if sys.version_info >= (3, 11) else "tomli")
    repo = Repo(tmp_path / "repo")
    repo.write({"memblame.toml": config})
    code, out, err = run_cli(capsys, "run", "-C", str(repo.path), "--json")
    assert code == 1 and message in err
    assert json.loads(out)["kind"] == "error"


@pytest.mark.parametrize("config", ['tool = 42', '[tool]\nmemblame = "invalid"'])
def test_config_table_shape_returns_structured_error(tmp_path, capsys, config):
    pytest.importorskip("tomllib" if sys.version_info >= (3, 11) else "tomli")
    repo = Repo(tmp_path / "repo")
    repo.write({"pyproject.toml": config})
    code, out, err = run_cli(capsys, "run", "-C", str(repo.path), "--json")
    assert code == 1 and "must be a table" in err
    assert json.loads(out)["kind"] == "error"


@pytest.mark.parametrize("value", ["nan", "inf", "0", "-1"])
def test_invalid_timeout_flag_returns_structured_error(tmp_path, capsys, value):
    repo = Repo(tmp_path / "repo")
    code, out, err = run_cli(capsys, "run", "-C", str(repo.path), "--json",
                           "-w", "call:a:b", f"--timeout={value}")
    assert code == 1 and "--timeout must be positive and finite" in err
    assert json.loads(out)["kind"] == "error"


def test_missing_repository_returns_repository_error(tmp_path, capsys):
    code, _, err = run_cli(capsys, "run", "-C", str(tmp_path / "missing"))
    assert code == 2 and "not inside a git repository" in err


@pytest.mark.parametrize("broken", ["raise ValueError('broken')\n", "import os\nos._exit(7)\n"])
def test_bisect_found_across_broken_commit_returns_regression(tmp_path, capsys, broken):
    repo = Repo(tmp_path / "repo")
    repo.commit({"bench.py": "x = bytearray(100_000)\n"}, "good")
    skipped = repo.commit({"bench.py": broken}, "broken")
    repo.commit({"bench.py": "x = bytearray(2_000_000)\n"}, "bad")
    code, out, _ = run_cli(capsys, "bisect", "--good", "HEAD~2", "--metric", "peak",
                           "--threshold", "+1MB", "-C", str(repo.path), "--json",
                           "-w", "script:bench.py", "--python", sys.executable,
                           "--runs", "1", "--no-cache")
    result = json.loads(out)
    assert result["status"] == "found"
    assert skipped in result["culprit_range"]
    assert result["measurement_status"] != "complete"
    assert code == 3
