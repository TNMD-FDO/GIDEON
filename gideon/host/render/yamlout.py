"""Small deterministic YAML emitter for rendered configuration."""

import json
import re
from collections.abc import Mapping, Sequence

HEADER = (
    "# Rendered by gideon render — never hand-edit (spec §3.5). "
    "Re-run gideon apply after editing /etc/gideon/site.yaml."
)


# A key is written bare only when no YAML 1.1 loader could read it as anything
# but the string itself; every value string is always double-quoted.
_BARE_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]*$")
_YAML_SPECIAL = frozenset({"true", "false", "yes", "no", "on", "off", "null", "y", "n"})


def _key(key: str) -> str:
    if _BARE_KEY.fullmatch(key) and key.lower() not in _YAML_SPECIAL:
        return key
    return _scalar(key)


def _scalar(value: object) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    raise TypeError(f"unsupported YAML value: {type(value).__name__}")


def _is_scalar(value: object) -> bool:
    return value is None or isinstance(value, (bool, int, str))


def _empty_collection(value: object) -> bool:
    return (
        isinstance(value, (Mapping, Sequence))
        and not isinstance(value, (str, bytes))
        and not value
    )


def _leaf(value: object) -> str:
    if _is_scalar(value):
        return _scalar(value)
    if isinstance(value, Mapping) and not value:
        return "{}"
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)) and not value:
        return "[]"
    raise TypeError(f"expected a scalar or empty collection: {type(value).__name__}")


def _lines(value: object, indent: int) -> list[str]:
    spaces = " " * indent
    if _is_scalar(value):
        return [_scalar(value)]
    if isinstance(value, Mapping):
        if not value:
            return ["{}"]
        output: list[str] = []
        for key, child in value.items():
            if not isinstance(key, str):
                raise TypeError(f"mapping keys must be strings: {key!r}")
            prefix = f"{spaces}{_key(key)}:"
            if _is_scalar(child) or _empty_collection(child):
                output.append(f"{prefix} {_leaf(child)}")
            else:
                output.append(prefix)
                output.extend(_lines(child, indent + 2))
        return output
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        if not value:
            return ["[]"]
        output = []
        for child in value:
            if _is_scalar(child) or _empty_collection(child):
                output.append(f"{spaces}- {_leaf(child)}")
            else:
                child_lines = _lines(child, indent + 2)
                first = child_lines[0].lstrip()
                output.append(f"{spaces}- {first}")
                output.extend(child_lines[1:])
        return output
    raise TypeError(f"unsupported YAML value: {type(value).__name__}")


def dump(document: Mapping[str, object] | Sequence[object]) -> str:
    """Serialize the restricted render document to deterministic YAML."""

    return HEADER + "\n" + "\n".join(_lines(document, 0)) + "\n"


def dump_fragment(document: Mapping[str, object] | Sequence[object]) -> str:
    """Serialize a restricted value without the rendered-file header."""

    return "\n".join(_lines(document, 0)) + "\n"
