"""Git helpers: resolving revisions, temporary worktrees and diff hunks."""

from __future__ import annotations

import ast
import contextlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from dataclasses import asdict, dataclass
from pathlib import Path

# Pseudo-revision meaning "the files currently on disk, including uncommitted changes".
WORKTREE = "WORKTREE"

_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


class GitError(RuntimeError):
    pass


def git(repo: Path, *args: str, check: bool = True) -> str:
    proc = subprocess.run(
        # quotePath=false: non-ASCII paths come out as-is instead of "\303\251"-quoted
        ["git", "-c", "core.quotePath=false", *args], cwd=repo, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    if check and proc.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout


def repo_root(path: Path) -> Path:
    return Path(git(path, "rev-parse", "--show-toplevel").strip())


@dataclass(frozen=True)
class Commit:
    sha: str
    short: str
    author: str
    date: str
    subject: str

    def to_json(self) -> dict:
        return asdict(self)


def working_tree_commit() -> Commit:
    return Commit(WORKTREE, "working", "", "", "uncommitted changes")


_FMT = "%H%x00%h%x00%an%x00%aI%x00%s%x1e"


def _parse_commits(out: str) -> list[Commit]:
    commits = []
    for rec in out.split("\x1e"):
        rec = rec.strip("\n")
        if rec:
            sha, short, author, date, subject = rec.split("\x00", 4)
            commits.append(Commit(sha, short, author, date, subject))
    return commits


def commit_info(repo: Path, rev: str) -> Commit:
    if rev == WORKTREE:
        return working_tree_commit()
    return _parse_commits(git(repo, "show", "-s", f"--format={_FMT}", f"{rev}^{{commit}}", "--"))[0]


def commit_infos(repo: Path, shas: list[str]) -> list[Commit]:
    """Commit metadata for many SHAs with one git call (same order as `shas`)."""
    by_sha = {c.sha: c for c in _parse_commits(
        git(repo, "show", "-s", "--no-walk=unsorted", f"--format={_FMT}", *shas, "--"))}
    return [by_sha[s] for s in shas]


def resolve(repo: Path, rev: str) -> str:
    if rev == WORKTREE:
        return WORKTREE
    try:
        return git(repo, "rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}").strip()
    except GitError:
        raise GitError(f"unknown revision {rev!r}") from None


def first_parent_range(repo: Path, base: str, head: str) -> list[str]:
    """Commits from `base` (inclusive, as the baseline) to `head` along first parents."""
    base_sha, head_sha = resolve(repo, base), resolve(repo, head)
    if not is_first_parent_ancestor(repo, base_sha, head_sha):
        raise GitError(f"{base!r} is not on the first-parent history of {head!r}")
    shas = git(repo, "rev-list", "--first-parent", "--reverse",
               f"{base_sha}..{head_sha}").split()
    return [base_sha, *shas]


def is_dirty(repo: Path) -> bool:
    """Any tracked or untracked (but not ignored) working-tree change."""
    return bool(git(repo, "status", "--porcelain", "--untracked-files=normal").strip())


def is_ancestor(repo: Path, older: str, newer: str) -> bool:
    proc = subprocess.run(["git", "merge-base", "--is-ancestor", older, newer], cwd=repo,
                          capture_output=True)
    return proc.returncode == 0


def is_first_parent_ancestor(repo: Path, older: str, newer: str) -> bool:
    """Whether `older` occurs on the chain formed by repeatedly taking `newer`'s first parent."""
    if older == newer:
        return True
    return older in git(repo, "rev-list", "--first-parent", newer).split()


# --------------------------------------------------------------------------- worktrees


WORKTREE_PREFIX = "mb-"


def _pid_alive(pid: int) -> bool:
    if sys.platform == "win32":
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        code = ctypes.c_ulong()
        kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        kernel32.CloseHandle(handle)
        return code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def remove_stale_worktrees(repo: Path) -> list[str]:
    """Remove memblame worktrees whose process is gone (killed, e.g. on Windows cancel)."""
    removed = []
    tmp = Path(tempfile.gettempdir()).resolve()
    for line in git(repo, "worktree", "list", "--porcelain", check=False).splitlines():
        if not line.startswith("worktree "):
            continue
        wt = Path(line[len("worktree "):])
        base = wt.parent
        if wt.name != "wt" or not base.name.startswith(WORKTREE_PREFIX):
            continue
        if base.resolve().parent != tmp:
            continue
        try:
            pid = int((base / "pid").read_text())
        except (OSError, ValueError):
            pid = 0
        if pid and _pid_alive(pid):
            continue
        git(repo, "worktree", "remove", "--force", str(wt), check=False)
        shutil.rmtree(base, ignore_errors=True)
        removed.append(str(wt))
    if removed:
        git(repo, "worktree", "prune", check=False)
    return removed


class WorktreePool:
    """One reusable detached worktree; switching commits is much cheaper than re-creating."""

    def __init__(self, repo: Path):
        self.repo = repo
        self._dir: Path | None = None

    def checkout(self, sha: str) -> Path:
        if sha == WORKTREE:
            return self.repo
        if self._dir is None:
            remove_stale_worktrees(self.repo)
            # Short path: Windows has path-length limits and worktrees nest deep paths.
            base = Path(tempfile.mkdtemp(prefix=WORKTREE_PREFIX))
            (base / "pid").write_text(str(os.getpid()))
            self._dir = base / "wt"
            git(self.repo, "worktree", "add", "--detach", "--force", str(self._dir), sha)
        else:
            git(self._dir, "checkout", "--detach", "--force", "--quiet", sha)
            git(self._dir, "clean", "-fdxq")
        return self._dir

    def close(self) -> None:
        if self._dir is None:
            return
        git(self.repo, "worktree", "remove", "--force", str(self._dir), check=False)
        shutil.rmtree(self._dir.parent, ignore_errors=True)
        git(self.repo, "worktree", "prune", check=False)
        self._dir = None


@contextlib.contextmanager
def worktrees(repo: Path) -> Iterator[WorktreePool]:
    pool = WorktreePool(repo)
    try:
        yield pool
    finally:
        pool.close()


# --------------------------------------------------------------------------- diffs


@dataclass(frozen=True)
class Hunk:
    file: str  # path on the new side
    old_start: int
    old_len: int
    new_start: int
    new_len: int
    old_file: str = ""  # path on the old side ("" for added files)

    @property
    def new_range(self) -> tuple[int, int]:
        """Inclusive new-side line range. Pure deletions touch the lines around the cut."""
        if self.new_len == 0:
            return (self.new_start, self.new_start + 1)
        return (self.new_start, self.new_start + self.new_len - 1)

    @property
    def old_range(self) -> tuple[int, int]:
        """Inclusive old-side line range. Pure additions touch the lines around the insert."""
        if self.old_len == 0:
            return (self.old_start, self.old_start + 1)
        return (self.old_start, self.old_start + self.old_len - 1)

    def header(self) -> str:
        return f"@@ -{self.old_start},{self.old_len} +{self.new_start},{self.new_len} @@"

    def to_json(self) -> dict:
        return {**asdict(self), "header": self.header()}


def parse_hunks(diff_text: str) -> list[Hunk]:
    hunks: list[Hunk] = []
    current: str | None = None
    old = ""
    for line in diff_text.splitlines():
        if line.startswith("--- "):
            source = _diff_path(line[4:])
            old = "" if source == "/dev/null" else source.removeprefix("a/")
        elif line.startswith("+++ "):
            target = _diff_path(line[4:])
            current = old if target == "/dev/null" else target.removeprefix("b/")
        elif line.startswith("@@") and current is not None:
            m = _HUNK_RE.match(line)
            if m:
                a, b, c, d = m.groups()
                hunks.append(
                    Hunk(current, int(a), 1 if b is None else int(b), int(c),
                         1 if d is None else int(d), old)
                )
    return hunks


def _diff_path(header: str) -> str:
    """Decode Git's C-quoted paths, including octal UTF-8 bytes and control characters."""
    path = header.removesuffix("\t")
    if path.startswith('"'):
        # Git quotes backslashes and quotes using the same escapes as Python bytes.
        # Non-ASCII text may remain literal with core.quotePath=false.
        literal = path.encode("utf-8").decode("ascii", errors="backslashreplace")
        return ast.literal_eval("b" + literal).decode("utf-8", errors="replace")
    return path


def diff_hunks(repo: Path, base: str, head: str) -> list[Hunk]:
    """Changed Python hunks between two revisions (`head` may be WORKTREE)."""
    args = ["diff", "-U0", "--no-color", "--no-ext-diff", "--no-textconv",
            "--src-prefix=a/", "--dst-prefix=b/", "-M", base]
    if head != WORKTREE:
        args.append(head)
    hunks = parse_hunks(git(repo, *args, "--", "*.py"))
    if head == WORKTREE:  # untracked files count as entirely new
        untracked = git(repo, "ls-files", "-z", "--others", "--exclude-standard", "--", "*.py")
        for rel in filter(None, untracked.split("\0")):
            try:
                n = len((repo / rel).read_text(encoding="utf-8", errors="replace").splitlines())
            except OSError:
                continue  # dangling symlink or unreadable file: it cannot be in the workload
            hunks.append(Hunk(rel, 0, 0, 1, max(n, 1)))
    return hunks


def file_at(repo: Path, rev: str, rel: str) -> str | None:
    if rev == WORKTREE:
        p = repo / rel
        return p.read_text(encoding="utf-8", errors="replace") if p.exists() else None
    proc = subprocess.run(
        ["git", "show", f"{rev}:{rel}"], cwd=repo, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    return proc.stdout if proc.returncode == 0 else None
