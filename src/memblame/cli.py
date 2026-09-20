"""Command line interface: memblame run | diff | range | bisect."""

from __future__ import annotations

import argparse
import json
import math
import os
import signal
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from . import __version__, api, artifact, git, report
from .contract import error_output, validate_output
from .measure import MeasureError, Settings, validate_workload

CONFIG_KEYS = {"workload", "runs", "nframe", "pythonpath", "python", "timeout", "threshold",
               "cache_env", "cache_inputs"}


def _toml():
    try:
        import tomllib  # Python 3.11+

        return tomllib
    except ModuleNotFoundError:
        try:
            import tomli  # the same parser, installable on 3.9/3.10

            return tomli
        except ModuleNotFoundError:
            return None


def load_config(repo: Path) -> dict:
    """[tool.memblame] in pyproject.toml, or memblame.toml at the repo root."""
    toml = _toml()
    for name, table in (("memblame.toml", None), ("pyproject.toml", ("tool", "memblame"))):
        path = repo / name
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if toml is None:
            if table is None or "[tool.memblame]" in text:
                print(f"memblame: ignoring {name}: reading config needs Python 3.11+ or "
                      "`pip install tomli`; pass options on the command line instead",
                      file=sys.stderr)
            continue
        try:
            data = toml.loads(text)
        except toml.TOMLDecodeError as exc:
            print(f"memblame: ignoring {name}: {exc}", file=sys.stderr)
            continue
        for key in table or ():
            if not isinstance(data, dict):
                raise ValueError(f"config in {name}: {'.'.join(table or ())} must be a table")
            data = data.get(key, {})
        if not isinstance(data, dict):
            raise ValueError(f"config in {name}: {'.'.join(table or ())} must be a table")
        if data:
            unknown = sorted(set(data) - CONFIG_KEYS)
            if unknown:
                print(f"memblame: ignoring unknown keys in {name}: {', '.join(unknown)}",
                      file=sys.stderr)
            return {k: v for k, v in data.items() if k in CONFIG_KEYS}
    return {}


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-w", "--workload",
                        help="call:pkg.mod:func | script:path [args] | pytest:<node ids>")
    common.add_argument("-C", "--repo", default=".", help="repository path (default: .)")
    common.add_argument("--runs", type=int, help="runs per commit (default 3)")
    common.add_argument("--nframe", type=int, help="traceback depth (default 16)")
    common.add_argument("--pythonpath", action="append",
                        help="dir (relative to repo) to import the project from; repeatable")
    common.add_argument("--python", help="project interpreter (default: .venv or current)")
    common.add_argument("--timeout", type=float, help="seconds per run (default 900)")
    common.add_argument("--no-cache", action="store_true", help="ignore and don't write cache")
    common.add_argument("--cache-env", action="append", metavar="NAME",
                        help="environment variable that invalidates cached measurements; "
                             "repeatable")
    common.add_argument("--cache-input", action="append", metavar="PATH",
                        help="file or directory that invalidates cached measurements; repeatable")
    output = common.add_mutually_exclusive_group()
    output.add_argument("--json", action="store_true", help="print JSON (schema 1)")
    output.add_argument("--report", choices=["md", "markdown", "html"],
                        help="render a portable Markdown or self-contained HTML report")
    common.add_argument("-o", "--output", metavar="PATH",
                        help="write the selected output to PATH instead of stdout")

    p = argparse.ArgumentParser(
        prog="memblame",
        description="git blame for memory: find the commit and function that made your "
                    "Python code use more memory.",
    )
    p.add_argument("--version", action="version", version=f"memblame {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    r = sub.add_parser("run", parents=[common], help="measure one revision")
    r.add_argument("rev", nargs="?", default=git.WORKTREE,
                   help="revision (default: working tree incl. uncommitted changes)")

    d = sub.add_parser("diff", parents=[common], help="compare two revisions")
    d.add_argument("base", nargs="?", default="HEAD", help="base revision (default HEAD)")
    d.add_argument("head", nargs="?", default=git.WORKTREE,
                   help="head revision (default: working tree incl. uncommitted changes)")

    g = sub.add_parser("range", parents=[common], help="timeline over a commit range")
    g.add_argument("range", help="BASE..HEAD, e.g. main~20..main")
    g.add_argument("--all", action="store_true",
                   help="measure every commit (default: adaptive, only subdivide where "
                        "memory changed)")

    b = sub.add_parser("bisect", parents=[common], help="find the commit that crossed a limit")
    b.add_argument("--good", required=True)
    b.add_argument("--bad", default="HEAD")
    b.add_argument("--threshold", help="e.g. 200MB, +20MB, +10%% (default: noise band)")
    b.add_argument("--unit", help="unit (e.g. pytest node id) to track")
    b.add_argument("--metric", choices=["peak", "retained"])
    b.add_argument("--verify", action="store_true",
                   help="measure every candidate to verify the earliest threshold crossing")
    return p


_CONFIG_TYPES: dict[str, type | tuple[type, ...]] = {
    "workload": str, "runs": int, "nframe": int, "python": str, "threshold": str,
    "timeout": (int, float), "pythonpath": (str, list),
    "cache_env": (str, list), "cache_inputs": (str, list)}


def _check_types(config: dict) -> None:
    for key, raw in config.items():
        expected = _CONFIG_TYPES[key]
        if not isinstance(raw, expected) or isinstance(raw, bool):
            names = " or ".join(t.__name__ for t in (expected if isinstance(expected, tuple)
                                                     else (expected,)))
            raise ValueError(f"config key {key!r} must be {names}, got {raw!r}")
        # isinstance() against a variable class only narrows to object; the check above has
        # already established the real type.
        value: Any = raw
        if key in ("runs", "nframe") and value < 1:
            raise ValueError(f"config key {key!r} must be at least 1, got {value!r}")
        if key == "timeout" and (not math.isfinite(value) or value <= 0):
            raise ValueError(f"config key 'timeout' must be positive and finite, got {value!r}")
        if key in ("pythonpath", "cache_env", "cache_inputs") and isinstance(value, list):
            if not all(isinstance(item, str) for item in value):
                raise ValueError(f"config key {key!r} must contain only strings, got {value!r}")


def _interpreter(value: str | None, base: Path) -> str | None:
    """Relative paths ("./venv/bin/python") are made absolute against `base`: the workload
    runs inside a temporary checkout, where a relative path would not exist. Bare names
    ("python3") are left for PATH lookup."""
    if not value or os.path.isabs(value) or not ({"/", os.sep} & set(value)):
        return value
    return str((base / value).resolve())


def settings_from(args: argparse.Namespace, config: dict, repo: Path | None = None,
                  ) -> Settings:
    _check_types(config)
    workload = args.workload or config.get("workload")
    if not workload:
        raise ValueError(
            "no workload. Pass -w, e.g. -w 'pytest:tests/test_big.py' or "
            "-w 'call:pkg.module:main', or set workload in [tool.memblame] in pyproject.toml"
        )
    validate_workload(workload)
    pythonpath = args.pythonpath or config.get("pythonpath")
    if isinstance(pythonpath, str):
        pythonpath = [pythonpath]
    cache_env = args.cache_env if args.cache_env is not None else config.get("cache_env", [])
    cache_inputs = (args.cache_input if args.cache_input is not None
                    else config.get("cache_inputs", []))
    if isinstance(cache_env, str):
        cache_env = [cache_env]
    if isinstance(cache_inputs, str):
        cache_inputs = [cache_inputs]
    for flag in ("runs", "nframe", "timeout"):
        value = getattr(args, flag)
        if value is not None and (value <= 0 or (flag == "timeout" and not math.isfinite(value))):
            raise ValueError(f"--{flag} must be positive and finite, got {value}")
    return Settings(
        workload=workload,
        runs=args.runs or config.get("runs", 3),
        nframe=args.nframe or config.get("nframe", 16),
        pythonpath=pythonpath,
        python=(_interpreter(args.python, Path.cwd()) if args.python
                else _interpreter(config.get("python"), repo or Path.cwd())),
        timeout=args.timeout or config.get("timeout", 900.0),
        cache_env=cache_env,
        cache_inputs=cache_inputs,
    )


def _exit_on_sigterm() -> None:
    """Turn SIGTERM (e.g. an editor cancelling us) into SystemExit so worktrees get removed."""
    try:
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    except ValueError:  # not in the main thread (embedded use)
        pass


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _exit_on_sigterm()
    try:
        repo = git.repo_root(Path(args.repo))
    except (git.GitError, OSError):
        message = f"{args.repo} is not inside a git repository"
        print(f"memblame: {message}", file=sys.stderr)
        if args.json:
            # Same destination as a successful result, so `-o result.json` always holds JSON.
            _emit(json.dumps(error_output(message)), args.output, "JSON")
        return 2
    try:
        config = load_config(repo)
        settings = settings_from(args, config, repo)
        threshold = None
        if args.command == "bisect":
            threshold = args.threshold or config.get("threshold")
            if threshold:
                # Before the session: a typo must not cost two full endpoint measurements.
                api.check_threshold(threshold)
        with api.Session(repo, settings, use_cache=not args.no_cache) as s:
            if args.command == "run":
                out, fmt = api.run(s, args.rev), report.format_run
            elif args.command == "diff":
                out, fmt = api.diff(s, args.base, args.head), report.format_diff
            elif args.command == "range":
                base, sep, head = args.range.partition("..")
                if not sep:
                    raise ValueError("range must look like BASE..HEAD")
                out = api.range_(s, base, head or "HEAD", exhaustive=args.all)
                fmt = report.format_range
            else:
                out = api.bisect(s, args.good, args.bad, threshold, args.unit, args.metric,
                                 verify=args.verify)
                fmt = report.format_bisect
    except (git.GitError, MeasureError, ValueError, RuntimeError, OSError) as exc:
        print(f"memblame: error: {exc}", file=sys.stderr)
        if args.json:
            # Same destination as a successful result, so `-o result.json` always holds JSON.
            _emit(json.dumps(error_output(str(exc))), args.output, "JSON")
        return 1
    except KeyboardInterrupt:  # worktrees were already removed by the session's __exit__
        print("memblame: interrupted", file=sys.stderr)
        return 130
    out = validate_output(out)
    if args.json:
        rendered, label = json.dumps(out, indent=1), "JSON"
    elif args.report:
        rendered, label = artifact.render(out, args.report), args.report.upper()
    else:
        rendered, label = fmt(out), "text"
    if not _emit(rendered, args.output, label):
        return 1
    if _has_measurement_failure(out):
        return 1
    return 3 if _has_regression(out) else 0


def _emit(rendered: str, output: str | None, label: str) -> bool:
    """Print `rendered`, or write it to `output`. False if the file could not be written."""
    if not output:
        print(rendered)
        return True
    try:
        target = artifact.write(rendered, output)
    except OSError as exc:
        print(f"memblame: error: could not write report to {output}: {exc}", file=sys.stderr)
        return False
    print(f"memblame: wrote {label} report to {target}", file=sys.stderr)
    return True


def _has_measurement_failure(out: Mapping[str, Any]) -> bool:
    """A missing/failed measurement must not look like a successful regression check."""
    # A found bisect result can contain skipped intermediate commits. Its passing
    # endpoints still establish a regression, with uncertainty recorded in the report.
    if out.get("kind") == "bisect" and out.get("status") == "found":
        return False
    return out["measurement_status"] != "complete"


def _has_regression(out: Mapping[str, Any]) -> bool:
    """Exit code 3 signals a significant memory increase (useful in CI)."""
    if out.get("kind") == "bisect":
        return out.get("status") == "found"
    return any(f["delta"] > 0 for f in out.get("findings", []))


if __name__ == "__main__":
    sys.exit(main())
