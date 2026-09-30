"""Load a committed challenger and its registered configuration subject."""

import difflib
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path
from typing import Any, Final, cast

import yaml  # type: ignore[import-untyped]

from gideon.evaluation import judge, slices
from gideon.evaluation.results import RunContext
from gideon.host.sysio import Host, PathLike, RealHost

CHALLENGER_PATH: Final[Path] = Path("config/challenger.yaml")
_FIX: Final[str] = (
    f"Edit {CHALLENGER_PATH}; consult docs/runbooks/release-files.md §8."
)
SCHEMA_VERSION: Final[int] = 1
_ROOT_KEYS: Final = ("version", "challenger")
_ENTRY_KEYS: Final = ("name", "subject", "release", "challenger", "set")
_NAME_PATTERN: Final = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_SET_PATTERN: Final = re.compile(r"^([0-9]{4}-[0-9]{2}-[0-9]{2}) (\S+)$")

OVERRIDE_KEY: Final[str] = "challenger"
NAME_FIELD: Final[str] = "name"
SUBJECT_FIELD: Final[str] = "subject"
SIDE_FIELD: Final[str] = "side"
VALUE_FIELD: Final[str] = "value"
PAIRS_FIELD: Final[str] = "pairs"
RELEASE_SIDE: Final[str] = "release"
CHALLENGER_SIDE: Final[str] = "challenger"


@dataclass(frozen=True, slots=True)
class ChallengerEntry:
    """One named experiment with its two values and setting reference."""

    name: str
    subject: str
    release: str
    challenger: str
    set_date: date
    set_reference: str


@dataclass(frozen=True, slots=True)
class ChallengerConfig:
    """A validated document, including a clean state with no challenger set."""

    version: int
    challenger: ChallengerEntry | None


@dataclass(frozen=True, slots=True)
class ChallengerFinding:
    """A content-free key path, violated rule, and corrective action."""

    key_path: str
    problem: str
    fix: str


@dataclass(frozen=True, slots=True)
class ChallengerLoadResult:
    """The validated configuration, or the findings that refused it."""

    config: ChallengerConfig | None = None
    document: Mapping[str, object] | None = None
    findings: tuple[ChallengerFinding, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.findings and self.config is not None


@dataclass(frozen=True, slots=True)
class ChallengerSubject:
    """A registered slice and the checks and context change for its values."""

    name: str
    slice_name: str
    check: Callable[[str, str, str], tuple[ChallengerFinding, ...]]
    change: Callable[[RunContext, str], RunContext]


def _finding(path: str, rule: str) -> ChallengerFinding:
    return ChallengerFinding(path, rule, _FIX)


def _check_judge_prompt(
    slice_name: str, release: str, challenger: str
) -> tuple[ChallengerFinding, ...]:
    findings: list[ChallengerFinding] = []
    release_prompt = judge.PROMPT_REGISTRY.get(release)
    challenger_prompt = judge.PROMPT_REGISTRY.get(challenger)
    if release_prompt is None:
        findings.append(_finding("challenger.release", "expected a registered judge prompt"))
    if challenger_prompt is None:
        findings.append(
            _finding("challenger.challenger", "expected a registered judge prompt")
        )
    if release == challenger:
        findings.append(_finding("challenger.challenger", "must differ from release"))
    if (
        release_prompt is not None
        and challenger_prompt is not None
        and (
            release_prompt.slots != challenger_prompt.slots
            or release_prompt.schema_name != challenger_prompt.schema_name
        )
    ):
        findings.append(
            _finding(
                "challenger.challenger",
                "both prompts must share slots and schema name",
            )
        )
    slice_spec = slices.SLICE_RUNNERS.get(slice_name)
    if slice_spec is None or slice_spec.judge_prompt != release:
        findings.append(
            _finding("challenger.release", "must name the slice's registered judge prompt")
        )
    return tuple(findings)


def _change_judge_prompt(context: RunContext, value: str) -> RunContext:
    return replace(context, judge_prompt_id=value)


SUBJECTS: Final[tuple[ChallengerSubject, ...]] = (
    ChallengerSubject("judge-prompt", "judge-triples", _check_judge_prompt, _change_judge_prompt),
)


class _DuplicateKeyError(yaml.YAMLError):
    """A repeated YAML key, with no source value in its message."""


class _ChallengerLoader(yaml.SafeLoader):
    """SafeLoader that refuses duplicate mapping keys."""


_ChallengerLoader.yaml_implicit_resolvers = {
    initial: list(resolvers)
    for initial, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}


def _construct_mapping(
    loader: _ChallengerLoader, node: Any, deep: bool = False
) -> dict[object, object]:
    mapping: dict[object, object] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            repeated = key in mapping
        except TypeError as exc:
            raise yaml.YAMLError("mapping key must be scalar") from exc
        if repeated:
            raise _DuplicateKeyError("duplicate mapping key")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_ChallengerLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_mapping,
)


def _nearest(word: str, valid: Sequence[str]) -> str:
    return difflib.get_close_matches(word, list(valid), n=1, cutoff=0.0)[0]


def _unknown_keys(
    value: Mapping[Any, object],
    keys: Sequence[str],
    prefix: str,
    findings: list[ChallengerFinding],
) -> None:
    for key in value:
        if isinstance(key, str) and key in keys:
            continue
        name = key if isinstance(key, str) else "<non-string>"
        path = f"{prefix}.{name}" if prefix else name
        findings.append(
            _finding(path, f"unknown key; nearest valid key is '{_nearest(name, keys)}'")
        )


def _required_string(
    entry: Mapping[str, object], key: str, findings: list[ChallengerFinding]
) -> str | None:
    path = f"challenger.{key}"
    if key not in entry:
        findings.append(_finding(path, "missing required key"))
        return None
    value = entry[key]
    if not isinstance(value, str) or not value:
        findings.append(_finding(path, "expected a non-empty string"))
        return None
    return value


def _valid_set(value: str) -> bool:
    match = _SET_PATTERN.fullmatch(value)
    if match is None:
        return False
    try:
        date.fromisoformat(match.group(1))
    except ValueError:
        return False
    return True


def validate_challenger(document: Mapping[str, object]) -> list[ChallengerFinding]:
    """Report every shape, grammar, and subject finding in a document."""

    findings: list[ChallengerFinding] = []
    _unknown_keys(document, _ROOT_KEYS, "", findings)
    if "version" not in document:
        findings.append(_finding("version", "missing required key"))
    elif (
        not isinstance(document["version"], int)
        or isinstance(document["version"], bool)
        or document["version"] != SCHEMA_VERSION
    ):
        findings.append(_finding("version", "expected schema version 1"))

    if "challenger" not in document:
        findings.append(_finding("challenger", "missing required key"))
        return findings
    value = document["challenger"]
    if value is None:
        return findings
    if not isinstance(value, Mapping):
        findings.append(_finding("challenger", "expected a mapping or null"))
        return findings
    entry = cast(Mapping[str, object], value)
    _unknown_keys(entry, _ENTRY_KEYS, "challenger", findings)
    name = _required_string(entry, "name", findings)
    if name is not None and _NAME_PATTERN.fullmatch(name) is None:
        findings.append(_finding("challenger.name", "expected lowercase letters, digits, and hyphens"))

    subject_name = _required_string(entry, "subject", findings)
    subject = next((item for item in SUBJECTS if item.name == subject_name), None)
    if subject_name is not None and subject is None:
        findings.append(
            _finding(
                "challenger.subject",
                f"unknown subject; nearest valid subject is '{_nearest(subject_name, tuple(item.name for item in SUBJECTS))}'",
            )
        )

    release = _required_string(entry, "release", findings)
    candidate = _required_string(entry, "challenger", findings)
    set_value = _required_string(entry, "set", findings)
    if set_value is not None and not _valid_set(set_value):
        findings.append(
            _finding("challenger.set", "expected a valid ISO date, a space, and a tag or pull request")
        )
    if subject is not None and release is not None and candidate is not None:
        findings.extend(subject.check(subject.slice_name, release, candidate))
    return findings


def _construct(document: Mapping[str, object]) -> ChallengerConfig:
    value = document["challenger"]
    if value is None:
        return ChallengerConfig(SCHEMA_VERSION, None)
    entry = cast(Mapping[str, str], value)
    set_date, set_reference = entry["set"].split(" ", 1)
    return ChallengerConfig(
        SCHEMA_VERSION,
        ChallengerEntry(
            name=entry["name"],
            subject=entry["subject"],
            release=entry["release"],
            challenger=entry["challenger"],
            set_date=date.fromisoformat(set_date),
            set_reference=set_reference,
        ),
    )


def render_findings(findings: Sequence[ChallengerFinding]) -> str:
    """Render each finding with its key path and fix on one line."""

    return "\n".join(
        f"{finding.key_path}: {finding.problem}. Fix: {finding.fix}"
        for finding in findings
    )


def read_challenger(
    path: PathLike, *, host: Host | None = None
) -> ChallengerLoadResult:
    """Read a challenger file through the host and parse it without construction."""

    io = host or RealHost()
    try:
        text = io.read_text(path)
    except FileNotFoundError:
        return ChallengerLoadResult(findings=(_finding("$", "file is missing"),))
    except PermissionError:
        return ChallengerLoadResult(findings=(_finding("$", "file is unreadable"),))
    except UnicodeDecodeError:
        return ChallengerLoadResult(findings=(_finding("$", "file is not valid UTF-8"),))
    except OSError:
        return ChallengerLoadResult(findings=(_finding("$", "file is unreadable"),))

    try:
        document = yaml.load(text, Loader=_ChallengerLoader)
    except _DuplicateKeyError:
        return ChallengerLoadResult(findings=(_finding("$", "duplicate mapping key"),))
    except yaml.YAMLError:
        return ChallengerLoadResult(findings=(_finding("$", "YAML parse error"),))
    if document is None:
        return ChallengerLoadResult(findings=(_finding("$", "file is empty"),))
    if not isinstance(document, Mapping):
        return ChallengerLoadResult(findings=(_finding("$", "root must be a mapping"),))
    return ChallengerLoadResult(document=cast(Mapping[str, object], document))


def load_challenger(
    path: PathLike, *, host: Host | None = None
) -> ChallengerLoadResult:
    """Read and validate a committed challenger, constructing only a clean one."""

    result = read_challenger(path, host=host)
    if result.findings or result.document is None:
        return result
    findings = validate_challenger(result.document)
    if findings:
        return ChallengerLoadResult(document=result.document, findings=tuple(findings))
    return ChallengerLoadResult(config=_construct(result.document), document=result.document)
