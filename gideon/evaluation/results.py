"""Shared result types for evaluation runners."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass

from gideon.host.sysio import Host, PathLike

type JSONValue = None | bool | int | float | str | list[JSONValue] | Mapping[str, JSONValue]


@dataclass(frozen=True, slots=True)
class CaseResult:
    """One content-free case result, with judge-derived values kept in ``judge``.

    Everything except ``judge`` is content-free, and nothing derived from a
    judge's score may sit outside ``judge``.
    """

    case_id: str
    repeat: int
    verdict: str
    metrics: Mapping[str, JSONValue]
    judge: Mapping[str, JSONValue] | None = None
    latency_ms: float | None = None

    def __repr__(self) -> str:
        """Keep the judge's reason out of diagnostic representations.

        ``judge`` is the one field that can carry model-written text, so the
        generated repr would otherwise print the reason that ``judge.Verdict``
        takes care to redact.
        """

        judge = None if self.judge is None else "<redacted>"
        return (
            "CaseResult("
            f"case_id={self.case_id!r}, repeat={self.repeat!r}, "
            f"verdict={self.verdict!r}, metrics={self.metrics!r}, "
            f"judge={judge}, latency_ms={self.latency_ms!r})"
        )


@dataclass(frozen=True, slots=True)
class SliceResult:
    """The code-computed gate verdict, report, and per-case results."""

    verdict: bool
    report: str
    results: tuple[CaseResult, ...]


@dataclass(frozen=True, slots=True)
class RunContext:
    """The host seams and runner settings for one evaluation slice."""

    host: Host
    rendered_dir: PathLike
    served_model_name: str | None
    judge_prompt_id: str | None
    repeats: int
    progress: Callable[[str], None]
