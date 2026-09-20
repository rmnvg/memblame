"""Typed schema-1 public result contract and lightweight runtime validation."""

from __future__ import annotations

from typing import Any, Literal, TypedDict

MeasurementStatus = Literal["complete", "incomplete", "error"]
ResultKind = Literal["run", "diff", "range", "bisect"]


class CommitJSON(TypedDict, total=False):
    sha: str
    short: str
    author: str
    date: str
    subject: str


class StatsJSON(TypedDict, total=False):
    median: int
    min: int
    max: int
    samples: list[int]


class UnitJSON(TypedDict, total=False):
    outcome: str
    error: str | None
    peak: StatsJSON
    end: StatsJSON
    duration_s: float


class FindingJSON(TypedDict, total=False):
    unit: str
    metric: str
    delta: int
    base: int
    head: int
    band: int
    verdict: dict[str, Any]
    functions: list[dict[str, Any]]
    commit: str
    parent: str


class PublicResult(TypedDict, total=False):
    schema: int
    kind: ResultKind
    repo: str
    workload: str
    python: str
    settings: dict[str, Any]
    measurement_status: MeasurementStatus
    warnings: list[str]
    notes: list[str]
    findings: list[FindingJSON]
    commit: CommitJSON
    base: CommitJSON
    head: CommitJSON
    result: dict[str, Any]
    results: dict[str, Any]
    units: Any
    points: list[dict[str, Any]]
    status: str
    message: str
    culprit: CommitJSON
    parent: CommitJSON


def validate_output(data: dict[str, Any]) -> PublicResult:
    """Reject an accidental contract break before JSON or a human report is emitted."""
    if data.get("schema") != 1:
        raise ValueError(f"unsupported result schema {data.get('schema')!r}; expected 1")
    kind = data.get("kind")
    if kind not in {"run", "diff", "range", "bisect"}:
        raise ValueError(f"invalid result kind {kind!r}")
    for key in ("repo", "workload", "python"):
        if not isinstance(data.get(key), str):
            raise ValueError(f"result field {key!r} must be a string")
    status = data.get("measurement_status")
    if status not in {"complete", "incomplete", "error"}:
        raise ValueError(f"invalid measurement_status {status!r}")
    required = {
        "run": ("commit", "result"),
        "diff": ("base", "head"),
        "range": ("points", "findings"),
        "bisect": ("status",),
    }[kind]
    missing = [key for key in required if key not in data]
    if missing:
        raise ValueError(f"{kind} result is missing: {', '.join(missing)}")
    return data  # type: ignore[return-value] - validated structural TypedDict
