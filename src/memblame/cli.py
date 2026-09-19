"""Command line interface: memblame run | diff | range | bisect."""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
from pathlib import Path

from . import __version__, api, git, report
from .measure import MeasureError, Settings

CONFIG_KEYS = {"workload", "runs", "nframe", "pythonpath", "python", "timeout", "threshold"}


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
            data = data.get(key, {})
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
    common.add_argument("--json", action="store_true", help="print JSON (schema 1)")

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
    return p


_CONFIG_TYPES = {"workload": str, "runs": int, "nframe": int, "python": str, "threshold": str,
                 "timeout": (int, float), "pythonpath": (str, list)}


def _check_types(config: dict) -> None:
    for key, value in config.items():
        expected = _CONFIG_TYPES[key]
        if not isinstance(value, expected) or isinstance(value, bool):
            names = " or ".join(t.__name__ for t in (expected if isinstance(expected, tuple)
                                                     else (expected,)))
            raise ValueError(f"config key {key!r} must be {names}, got {value!r}")
        if key in ("runs", "nframe") and value < 1:
            raise ValueError(f"config key {key!r} must be at least 1, got {value!r}")


def _interpreter(value: str | None, base: Path) -> str | None:
    """Relative paths ("./venv/bin/python") are made absolute against `base`: the workload
    runs inside a temporary checkout, where a relative path would not exist. Bare names
    ("python3") are left for PATH lookup."""
    if not value or os.path.isabs(value) or not ({"/", os.sep} & set(value)):
        return value
    return str((base / value).resolve())


def settings_from(args: argparse.Namespace, config: dict, repo: Path | None = None,
                  ) -> Settings:
    workload = args.workload or config.get("workload")
    if not workload:
        raise ValueError(
            "no workload. Pass -w, e.g. -w 'pytest:tests/test_big.py' or "
            "-w 'call:pkg.module:main', or set workload in [tool.memblame] in pyproject.toml"
        )
    if workload.partition(":")[0] not in ("pytest", "script", "call"):
        raise ValueError(f"bad workload {workload!r}: must start with pytest:, script: or call:")
    _check_types(config)
    pythonpath = args.pythonpath or config.get("pythonpath")
    if isinstance(pythonpath, str):
        pythonpath = [pythonpath]
    for flag in ("runs", "nframe", "timeout"):
        value = getattr(args, flag)
        if value is not None and value <= 0:
            raise ValueError(f"--{flag} must be positive, got {value}")
    return Settings(
        workload=workload,
        runs=args.runs or config.get("runs", 3),
        nframe=args.nframe or config.get("nframe", 16),
        pythonpath=pythonpath,
        python=(_interpreter(args.python, Path.cwd()) if args.python
                else _interpreter(config.get("python"), repo or Path.cwd())),
        timeout=args.timeout or config.get("timeout", 900.0),
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
    except git.GitError:
        print(f"memblame: {args.repo} is not inside a git repository", file=sys.stderr)
        return 2
    config = load_config(repo)
    try:
        settings = settings_from(args, config, repo)
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
                threshold = args.threshold or config.get("threshold")
                out = api.bisect(s, args.good, args.bad, threshold, args.unit, args.metric)
                fmt = report.format_bisect
    except (git.GitError, MeasureError, ValueError, RuntimeError) as exc:
        if args.json:
            print(json.dumps({"schema": 1, "kind": "error", "error": str(exc)}))
        print(f"memblame: error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:  # worktrees were already removed by the session's __exit__
        print("memblame: interrupted", file=sys.stderr)
        return 130
    print(json.dumps(out, indent=1) if args.json else fmt(out))
    return 3 if _has_regression(out) else 0


def _has_regression(out: dict) -> bool:
    """Exit code 3 signals a significant memory increase (useful in CI)."""
    if out.get("kind") == "bisect":
        return out.get("status") == "found"
    return any(f["delta"] > 0 for f in out.get("findings", []))


if __name__ == "__main__":
    sys.exit(main())
