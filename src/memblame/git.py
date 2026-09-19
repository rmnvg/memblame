"""Git helpers: resolving revisions, temporary worktrees and diff hunks."""

from __future__ import annotations

import contextlib
import re
import shutil
import subprocess
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
        ["git", *args], cwd=repo, capture_output=True, text=True, encoding="utf-8",
        errors="replace",
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


def commit_info(repo: Path, rev: str) -> Commit:
    if rev == WORKTREE:
        return working_tree_commit()
    fmt = "%H%x00%h%x00%an%x00%aI%x00%s"
    out = git(repo, "show", "-s", f"--format={fmt}", f"{rev}^{{commit}}", "--")
    sha, short, author, date, subject = out.rstrip("\n").split("\x00", 4)
    return Commit(sha, short, author, date, subject)


def resolve(repo: Path, rev: str) -> str:
    if rev == WORKTREE:
        return WORKTREE
    try:
        return git(repo, "rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}").strip()
    except GitError:
        raise GitError(f"unknown revision {rev!r}") from None


def first_parent_range(repo: Path, base: str, head: str) -> list[str]:
    """Commits from `base` (inclusive, as the baseline) to `head` along first parents."""
    shas = git(repo, "rev-list", "--first-parent", "--reverse", f"{base}..{head}").split()
    return [resolve(repo, base), *shas]


def is_dirty(repo: Path) -> bool:
    return bool(git(repo, "status", "--porcelain", "--untracked-files=no").strip())


# --------------------------------------------------------------------------- worktrees


class WorktreePool:
    """One reusable detached worktree; switching commits is much cheaper than re-creating."""

    def __init__(self, repo: Path):
        self.repo = repo
        self._dir: Path | None = None

    def checkout(self, sha: str) -> Path:
        if sha == WORKTREE:
            return self.repo
        if self._dir is None:
            # Short path: Windows has path-length limits and worktrees nest deep paths.
            self._dir = Path(tempfile.mkdtemp(prefix="mb-")) / "wt"
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

    @property
    def new_range(self) -> tuple[int, int]:
        """Inclusive new-side line range. Pure deletions touch the lines around the cut."""
        if self.new_len == 0:
            return (self.new_start, self.new_start + 1)
        return (self.new_start, self.new_start + self.new_len - 1)

    def header(self) -> str:
        return f"@@ -{self.old_start},{self.old_len} +{self.new_start},{self.new_len} @@"

    def to_json(self) -> dict:
        return {**asdict(self), "header": self.header()}


def parse_hunks(diff_text: str) -> list[Hunk]:
    hunks: list[Hunk] = []
    current: str | None = None
    for line in diff_text.splitlines():
        if line.startswith("+++ "):
            target = line[4:].strip()
            current = None if target == "/dev/null" else target.removeprefix("b/")
        elif line.startswith("@@") and current is not None:
            m = _HUNK_RE.match(line)
            if m:
                a, b, c, d = m.groups()
                hunks.append(
                    Hunk(current, int(a), 1 if b is None else int(b), int(c),
                         1 if d is None else int(d))
                )
    return hunks


def diff_hunks(repo: Path, base: str, head: str) -> list[Hunk]:
    """Changed Python hunks between two revisions (`head` may be WORKTREE)."""
    args = ["diff", "-U0", "--no-color", "--no-ext-diff", "-M", base]
    if head != WORKTREE:
        args.append(head)
    hunks = parse_hunks(git(repo, *args, "--", "*.py"))
    if head == WORKTREE:  # untracked files count as entirely new
        for rel in git(repo, "ls-files", "--others", "--exclude-standard", "--", "*.py").split():
            n = len((repo / rel).read_text(encoding="utf-8", errors="replace").splitlines())
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
