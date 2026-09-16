"""Loading and validating the engine-verification sample artifact."""

import difflib
import hashlib
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, TypeGuard, cast

import yaml  # type: ignore[import-untyped]

from gideon.host.sysio import Host, PathLike


@dataclass(frozen=True, slots=True)
class SmokeCase:
    """The bounded open-prompt smoke case sent to the serving engine."""

    id: str
    prompt: str
    max_tokens: int


@dataclass(frozen=True, slots=True)
class NeedleCase:
    """The planted-text recall case sent at several prompt lengths."""

    id: str
    filler: str
    planted: str
    question: str
    expected: str
    depth: float


@dataclass(frozen=True, slots=True)
class StructuredCase:
    """The JSON-schema response case sent to the serving engine."""

    id: str
    prompt: str
    schema_name: str
    schema: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class FrontendCase:
    """A managed frontend turn in the engine-verification sample."""

    id: str
    prompt: str


@dataclass(frozen=True, slots=True)
class FrontendSection:
    """The positive and trip managed-turn cases in the sample."""

    positives: tuple[FrontendCase, ...]
    trip: FrontendCase


@dataclass(frozen=True, slots=True)
class Sample:
    """The validated engine-verification sample and its source fingerprint."""

    smoke: SmokeCase
    needle: NeedleCase
    structured: StructuredCase
    frontend: FrontendSection
    sha256: str


@dataclass(frozen=True, slots=True)
class SampleError:
    """A structured refusal from sample loading or validation."""

    path: str | None
    problem: str
    fix: str


@dataclass(frozen=True, slots=True)
class SampleLoadResult:
    """The validated sample, or every refusal found while loading it."""

    sample: Sample | None = None
    errors: tuple[SampleError, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.errors and self.sample is not None


class _DuplicateKeyError(yaml.YAMLError):
    """Raised when a YAML mapping repeats a key."""


class SampleLoader(yaml.SafeLoader):
    """SafeLoader that refuses duplicate keys."""


SampleLoader.yaml_implicit_resolvers = {
    initial: list(resolvers)
    for initial, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}


def _construct_mapping_without_duplicates(
    loader: SampleLoader, node: Any, deep: bool = False
) -> dict[object, object]:
    mapping: dict[object, object] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            line = key_node.start_mark.line + 1
            raise _DuplicateKeyError(f"duplicate mapping key {key!r} at line {line}")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


SampleLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_mapping_without_duplicates,
)


_FIX: Final = (
    "Edit eval/engine-verify/sample.yaml, then re-run sudo python3 -m gideon engine verify."
)
_ROOT_KEYS: Final = ("version", "needle", "structured", "smoke", "frontend")
_SMOKE_KEYS: Final = ("id", "prompt", "max_tokens")
_NEEDLE_KEYS: Final = ("id", "filler", "planted", "question", "expected", "depth")
_STRUCTURED_KEYS: Final = ("id", "prompt", "schema_name", "schema")
_FRONTEND_KEYS: Final = ("positives", "trip")
_FRONTEND_CASE_KEYS: Final = ("id", "prompt")
_FRONTEND_ID: Final[re.Pattern[str]] = re.compile(r"[a-z0-9-]+")
_SCHEMA_KEYS: Final = (
    "type",
    "properties",
    "required",
    "additionalProperties",
    "enum",
    "items",
    "minItems",
    "maxItems",
    "minimum",
    "maximum",
    "minLength",
    "maxLength",
)
_SCHEMA_TYPES: Final = ("object", "array", "string", "integer", "number", "boolean")


def _path(prefix: str, key: object) -> str:
    rendered = str(key)
    return f"{prefix}.{rendered}" if prefix else rendered


def _nearest(path: str, valid: Sequence[str]) -> str:
    return difflib.get_close_matches(path, list(valid), n=1, cutoff=0.0)[0]


def _unknown(path: str, valid: Sequence[str]) -> SampleError:
    return SampleError(
        path=path,
        problem=(
            f"Unknown key '{path}'; nearest valid key is '{_nearest(path, valid)}'."
        ),
        fix=_FIX,
    )


def _walk_unknown(
    value: object,
    keys: Sequence[str],
    prefix: str,
    errors: list[SampleError],
) -> None:
    """Append unknown-key errors for one fixed mapping level."""

    if not isinstance(value, Mapping):
        return
    for key in value:
        if not isinstance(key, str) or key not in keys:
            errors.append(_unknown(_path(prefix, key), keys))


def _error(path: str, detail: str) -> SampleError:
    return SampleError(
        path=path,
        problem=f"Invalid value for '{path}': {detail}.",
        fix=_FIX,
    )


def _kind(value: object) -> str:
    return type(value).__name__


def _required_string(
    document: Mapping[object, object],
    prefix: str,
    key: str,
    errors: list[SampleError],
) -> str | None:
    path = _path(prefix, key)
    if key not in document:
        errors.append(_error(path, "missing required key"))
        return None
    value = document[key]
    if not isinstance(value, str) or not value:
        errors.append(_error(path, f"expected a non-empty string (got {_kind(value)})"))
        return None
    return value


def _required_positive_int(
    document: Mapping[object, object],
    prefix: str,
    key: str,
    errors: list[SampleError],
) -> int | None:
    path = _path(prefix, key)
    if key not in document:
        errors.append(_error(path, "missing required key"))
        return None
    value = document[key]
    if type(value) is not int or value <= 0:
        errors.append(_error(path, f"expected a positive integer (got {_kind(value)})"))
        return None
    return value


def _validate_smoke(
    value: object, errors: list[SampleError], prefix: str = "smoke"
) -> SmokeCase | None:
    """Validate the smoke case; later case validators belong beside this one."""

    path = prefix
    if not isinstance(value, Mapping):
        errors.append(_error(path, f"expected a mapping (got {_kind(value)})"))
        return None

    _walk_unknown(value, _SMOKE_KEYS, path, errors)
    smoke = cast(Mapping[object, object], value)
    start = len(errors)
    case_id = _required_string(smoke, path, "id", errors)
    prompt = _required_string(smoke, path, "prompt", errors)
    max_tokens = _required_positive_int(smoke, path, "max_tokens", errors)
    if len(errors) != start or case_id is None or prompt is None or max_tokens is None:
        return None
    return SmokeCase(id=case_id, prompt=prompt, max_tokens=max_tokens)


def _validate_frontend_case(
    value: object, errors: list[SampleError], prefix: str
) -> FrontendCase | None:
    """Validate one managed frontend case."""

    path = prefix
    if not isinstance(value, Mapping):
        errors.append(_error(path, f"expected a mapping (got {_kind(value)})"))
        return None

    _walk_unknown(value, _FRONTEND_CASE_KEYS, path, errors)
    case = cast(Mapping[object, object], value)
    start = len(errors)
    case_id = _required_string(case, path, "id", errors)
    prompt = _required_string(case, path, "prompt", errors)
    if case_id is not None and _FRONTEND_ID.fullmatch(case_id) is None:
        errors.append(
            _error(path + ".id", "expected lowercase letters, digits, and hyphens")
        )
    if len(errors) != start or case_id is None or prompt is None:
        return None
    return FrontendCase(id=case_id, prompt=prompt)


def _validate_frontend(
    value: object, errors: list[SampleError], prefix: str = "frontend"
) -> FrontendSection | None:
    """Validate the positive and trip managed frontend cases."""

    path = prefix
    if not isinstance(value, Mapping):
        errors.append(_error(path, f"expected a mapping (got {_kind(value)})"))
        return None

    _walk_unknown(value, _FRONTEND_KEYS, path, errors)
    frontend = cast(Mapping[object, object], value)
    start = len(errors)

    positive_cases: list[tuple[str, FrontendCase]] = []
    if "positives" not in frontend:
        errors.append(_error(_path(path, "positives"), "missing required key"))
    elif not isinstance(frontend["positives"], list):
        errors.append(
            _error(
                _path(path, "positives"),
                f"expected a non-empty list (got {_kind(frontend['positives'])})",
            )
        )
    elif not frontend["positives"]:
        errors.append(_error(_path(path, "positives"), "expected a non-empty list"))
    else:
        for index, value_case in enumerate(frontend["positives"]):
            case_path = f"{_path(path, 'positives')}[{index}]"
            case = _validate_frontend_case(value_case, errors, case_path)
            if case is not None:
                positive_cases.append((case_path, case))

    if "trip" not in frontend:
        errors.append(_error(_path(path, "trip"), "missing required key"))
        trip = None
    else:
        trip = _validate_frontend_case(frontend["trip"], errors, _path(path, "trip"))

    cases = [*positive_cases]
    if trip is not None:
        cases.append((_path(path, "trip"), trip))
    seen: dict[str, str] = {}
    for case_path, case in cases:
        if case.id in seen:
            errors.append(
                _error(
                    f"{case_path}.id",
                    f"must be unique within frontend (already used at {seen[case.id]})",
                )
            )
        else:
            seen[case.id] = f"{case_path}.id"

    if len(errors) != start or trip is None or not positive_cases:
        return None
    return FrontendSection(
        positives=tuple(case for _case_path, case in positive_cases),
        trip=trip,
    )


def _validate_needle(
    value: object, errors: list[SampleError], prefix: str = "needle"
) -> NeedleCase | None:
    """Validate the needle case independently of the other case shapes."""

    path = prefix
    if not isinstance(value, Mapping):
        errors.append(_error(path, f"expected a mapping (got {_kind(value)})"))
        return None

    _walk_unknown(value, _NEEDLE_KEYS, path, errors)
    needle = cast(Mapping[object, object], value)
    start = len(errors)
    case_id = _required_string(needle, path, "id", errors)
    filler = _required_string(needle, path, "filler", errors)
    planted = _required_string(needle, path, "planted", errors)
    question = _required_string(needle, path, "question", errors)
    expected = _required_string(needle, path, "expected", errors)
    depth_path = _path(path, "depth")
    if "depth" not in needle:
        errors.append(_error(depth_path, "missing required key"))
        depth = None
    else:
        value_depth = needle["depth"]
        if type(value_depth) is not float or not 0.0 < value_depth < 1.0:
            errors.append(
                _error(depth_path, f"expected a float strictly between 0 and 1 (got {_kind(value_depth)})")
            )
            depth = None
        else:
            depth = value_depth
    if (
        len(errors) != start
        or case_id is None
        or filler is None
        or planted is None
        or question is None
        or expected is None
        or depth is None
    ):
        return None
    if expected not in planted:
        errors.append(_error(_path(path, "expected"), "must be a substring of planted"))
        return None
    return NeedleCase(
        id=case_id,
        filler=filler,
        planted=planted,
        question=question,
        expected=expected,
        depth=depth,
    )


def _schema_error(path: str, detail: str, errors: list[SampleError]) -> None:
    errors.append(_error(path, detail))


def _check_schema_node(
    schema: object,
    path: str,
    errors: list[SampleError],
    *,
    top_level: bool,
) -> None:
    if not isinstance(schema, Mapping):
        _schema_error(path, "expected a mapping", errors)
        return

    for key in schema:
        if not isinstance(key, str) or key not in _SCHEMA_KEYS:
            _schema_error(_path(path, key), "unsupported schema keyword", errors)

    schema_type = schema.get("type")
    object_keywords = ("properties", "required", "additionalProperties")
    if top_level:
        if schema_type != "object":
            _schema_error(_path(path, "type"), "top-level schema type must be object", errors)
    elif "type" in schema and (
        not isinstance(schema_type, str) or schema_type not in _SCHEMA_TYPES
    ):
        _schema_error(_path(path, "type"), "unsupported schema type", errors)
    elif schema_type != "object" and any(key in schema for key in object_keywords):
        _schema_error(_path(path, "type"), "object keywords require type object", errors)

    properties = schema.get("properties")
    if "properties" in schema:
        if not isinstance(properties, Mapping):
            _schema_error(_path(path, "properties"), "expected a mapping", errors)
        else:
            for name, child in properties.items():
                if not isinstance(name, str):
                    _schema_error(_path(_path(path, "properties"), name), "property name must be a string", errors)
                _check_schema_node(
                    child,
                    _path(_path(path, "properties"), name),
                    errors,
                    top_level=False,
                )

    required = schema.get("required")
    if "required" in schema:
        if not isinstance(required, list) or any(not isinstance(name, str) for name in required):
            _schema_error(_path(path, "required"), "expected a list of strings", errors)
        elif isinstance(properties, Mapping):
            for name in required:
                if name not in properties:
                    _schema_error(
                        _path(_path(path, "required"), name),
                        "required name is missing from properties",
                        errors,
                    )

    if "additionalProperties" in schema and schema["additionalProperties"] is not False:
        _schema_error(
            _path(path, "additionalProperties"),
            "additionalProperties must be false",
            errors,
        )

    if "items" in schema:
        items = schema["items"]
        if not isinstance(items, Mapping):
            _schema_error(_path(path, "items"), "expected a mapping", errors)
        else:
            _check_schema_node(items, _path(path, "items"), errors, top_level=False)

    if "enum" in schema and not isinstance(schema["enum"], list):
        _schema_error(_path(path, "enum"), "expected a list", errors)

    for key in ("minItems", "maxItems", "minLength", "maxLength"):
        if key in schema and (type(schema[key]) is not int or schema[key] < 0):
            _schema_error(_path(path, key), "expected a non-negative integer", errors)
    for key in ("minimum", "maximum"):
        if key in schema and (
            isinstance(schema[key], bool) or not isinstance(schema[key], (int, float))
        ):
            _schema_error(_path(path, key), "expected a number", errors)


def check_schema(schema: object, path: str, errors: list[SampleError]) -> None:
    """Append refusals for keywords and shapes outside the supported schema subset."""

    _check_schema_node(schema, path, errors, top_level=True)


def _validate_structured(
    value: object, errors: list[SampleError], prefix: str = "structured"
) -> StructuredCase | None:
    """Validate the structured case and its exact JSON-schema subset."""

    path = prefix
    if not isinstance(value, Mapping):
        errors.append(_error(path, f"expected a mapping (got {_kind(value)})"))
        return None
    _walk_unknown(value, _STRUCTURED_KEYS, path, errors)
    structured = cast(Mapping[object, object], value)
    start = len(errors)
    case_id = _required_string(structured, path, "id", errors)
    prompt = _required_string(structured, path, "prompt", errors)
    schema_name = _required_string(structured, path, "schema_name", errors)
    if "schema" not in structured:
        errors.append(_error(_path(path, "schema"), "missing required key"))
        schema = None
    elif not isinstance(structured["schema"], Mapping):
        errors.append(_error(_path(path, "schema"), "expected a mapping"))
        schema = None
    else:
        schema = cast(Mapping[str, object], structured["schema"])
        check_schema(schema, _path(path, "schema"), errors)
    if len(errors) != start or case_id is None or prompt is None or schema_name is None or schema is None:
        return None
    return StructuredCase(case_id, prompt, schema_name, schema)


ADDITIONAL_PROPERTY: Final = "<additional-property>"


def _pointer(path: str, key: object) -> str:
    rendered = str(key).replace("~", "~0").replace("/", "~1")
    return f"{path}/{rendered}"


def _is_number(value: object) -> TypeGuard[int | float]:
    """A JSON number: an int or float that is not a bool."""

    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _enum_contains(value: object, choices: list[object]) -> bool:
    """Whether *value* equals a choice: the same type, or both JSON numbers."""

    for choice in choices:
        if type(value) is type(choice) and value == choice:
            return True
        if _is_number(value) and _is_number(choice) and value == choice:
            return True
    return False


def validate(instance: object, schema: Mapping[str, object]) -> tuple[str, ...]:
    """Return JSON-pointer-like paths for every violation of *schema*.

    Every segment is a schema-known name, an array index, or the
    ``ADDITIONAL_PROPERTY`` marker, so no path carries the instance's text.
    """

    violations: list[str] = []

    def visit(value: object, node: Mapping[str, object], path: str) -> None:
        schema_type = node.get("type")
        matches = (
            schema_type is None
            or (schema_type == "object" and isinstance(value, Mapping))
            or (schema_type == "array" and isinstance(value, list))
            or (schema_type == "string" and isinstance(value, str))
            or (schema_type == "integer" and type(value) is int)
            or (schema_type == "number" and _is_number(value))
            or (schema_type == "boolean" and isinstance(value, bool))
        )
        if not matches:
            violations.append(path)
            return

        choices = node.get("enum")
        if isinstance(choices, list) and not _enum_contains(value, choices):
            violations.append(path)

        if isinstance(value, Mapping):
            properties = node.get("properties")
            if not isinstance(properties, Mapping):
                properties = {}
            required = node.get("required")
            if isinstance(required, list):
                for name in required:
                    if isinstance(name, str) and name not in value:
                        violations.append(_pointer(path, name))
            for name, child in properties.items():
                if isinstance(name, str) and name in value and isinstance(child, Mapping):
                    visit(value[name], child, _pointer(path, name))
            if node.get("additionalProperties") is False:
                # An unknown key is text the model chose: the path names its
                # presence, never the key itself (§19.4).
                for name in value:
                    if name not in properties:
                        violations.append(_pointer(path, ADDITIONAL_PROPERTY))

        if isinstance(value, list):
            minimum = node.get("minItems")
            maximum = node.get("maxItems")
            if isinstance(minimum, int) and len(value) < minimum:
                violations.append(path)
            if isinstance(maximum, int) and len(value) > maximum:
                violations.append(path)
            items = node.get("items")
            if isinstance(items, Mapping):
                for index, item in enumerate(value):
                    visit(item, items, _pointer(path, index))

        if isinstance(value, str):
            minimum = node.get("minLength")
            maximum = node.get("maxLength")
            if isinstance(minimum, int) and len(value) < minimum:
                violations.append(path)
            if isinstance(maximum, int) and len(value) > maximum:
                violations.append(path)

        if _is_number(value):
            minimum = node.get("minimum")
            maximum = node.get("maximum")
            if isinstance(minimum, (int, float)) and value < minimum:
                violations.append(path)
            if isinstance(maximum, (int, float)) and value > maximum:
                violations.append(path)

    visit(instance, schema, "")
    return tuple(violations)


def _validate_document(
    document: object,
) -> tuple[
    NeedleCase | None,
    StructuredCase | None,
    SmokeCase | None,
    FrontendSection | None,
    tuple[SampleError, ...],
]:
    if not isinstance(document, Mapping):
        return None, None, None, None, (
            _error("document", f"expected a mapping (got {_kind(document)})"),
        )

    errors: list[SampleError] = []
    _walk_unknown(document, _ROOT_KEYS, "", errors)
    root = cast(Mapping[object, object], document)

    if "version" not in root:
        errors.append(_error("version", "missing required key"))
    elif type(root["version"]) is not int or root["version"] != 1:
        errors.append(_error("version", f"expected integer 1 (got {_kind(root['version'])})"))

    if "needle" not in root:
        errors.append(_error("needle", "missing required key"))
        needle = None
    else:
        needle = _validate_needle(root["needle"], errors, "needle")

    if "structured" not in root:
        errors.append(_error("structured", "missing required key"))
        structured = None
    else:
        structured = _validate_structured(root["structured"], errors, "structured")

    if "smoke" not in root:
        errors.append(_error("smoke", "missing required key"))
        smoke = None
    else:
        smoke = _validate_smoke(root["smoke"], errors, "smoke")

    if "frontend" not in root:
        errors.append(_error("frontend", "missing required key"))
        frontend = None
    else:
        frontend = _validate_frontend(root["frontend"], errors, "frontend")

    if (
        errors
        or needle is None
        or structured is None
        or smoke is None
        or frontend is None
    ):
        return None, None, None, None, tuple(errors)
    return needle, structured, smoke, frontend, ()


def render_errors(errors: Sequence[SampleError]) -> str:
    """Render one refusal per line, with each corrective action last."""

    return "\n".join(
        f"{' '.join(error.problem.splitlines())} Fix: {error.fix}" for error in errors
    )


def load_sample(path: PathLike, *, host: Host) -> SampleLoadResult:
    """Read and validate an engine-verification sample through *host*."""

    try:
        text = host.read_text(path)
    except FileNotFoundError:
        return SampleLoadResult(
            errors=(SampleError(None, f"sample file is missing: {path}", _FIX),)
        )
    except UnicodeDecodeError:
        return SampleLoadResult(
            errors=(SampleError(None, f"sample file is not valid UTF-8: {path}", _FIX),)
        )
    except OSError as exc:
        return SampleLoadResult(
            errors=(SampleError(None, f"sample file is unreadable: {path} ({exc})", _FIX),)
        )

    try:
        document = yaml.load(text, Loader=SampleLoader)
    except _DuplicateKeyError as exc:
        return SampleLoadResult(errors=(SampleError(None, f"sample has a {exc}", _FIX),))
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        line = getattr(mark, "line", None)
        location = f" at line {line + 1}" if isinstance(line, int) else ""
        return SampleLoadResult(
            errors=(
                SampleError(
                    None,
                    f"sample has a YAML parse error{location}: {exc}",
                    _FIX,
                ),
            )
        )

    needle, structured, smoke, frontend, errors = _validate_document(document)
    if errors:
        return SampleLoadResult(errors=errors)
    assert (
        needle is not None
        and structured is not None
        and smoke is not None
        and frontend is not None
    )
    return SampleLoadResult(
        sample=Sample(
            smoke=smoke,
            needle=needle,
            structured=structured,
            frontend=frontend,
            sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        )
    )


def blocks_for(target_tokens: int, per_block_tokens: int, overhead_tokens: int) -> int:
    """Return the smallest positive block count for a token target estimate."""

    if per_block_tokens <= 0:
        raise ValueError("per_block_tokens must be positive")
    return max(1, math.ceil((target_tokens - overhead_tokens) / per_block_tokens))


def build_needle_prompt(case: NeedleCase, blocks: int) -> list[dict[str, str]]:
    """Build a numbered, depth-planted chat prompt for *case*."""

    block_count = max(1, blocks)
    paragraphs = [
        f"Paragraph {number}. {case.filler}" for number in range(1, block_count + 1)
    ]
    planted_after = max(1, min(block_count, round(case.depth * block_count)))
    paragraphs.insert(planted_after, case.planted)
    paragraphs.append(case.question)
    return [{"role": "user", "content": "\n".join(paragraphs)}]
