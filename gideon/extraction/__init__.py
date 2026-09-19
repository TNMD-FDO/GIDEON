"""The standard-library-only exact-object extraction contract."""

from gideon.extraction.contract import (
    KEYED_TYPES,
    OBJECT_TYPES,
    SECTION_TYPES,
    ExactObject,
    ObjectType,
    key_violations,
    keys_equal,
    ordering_violations,
    span_violations,
)
from gideon.extraction.grammar import GRAMMAR_VERSION, extract

__all__ = [
    "ExactObject",
    "GRAMMAR_VERSION",
    "KEYED_TYPES",
    "OBJECT_TYPES",
    "SECTION_TYPES",
    "ObjectType",
    "key_violations",
    "keys_equal",
    "ordering_violations",
    "span_violations",
    "extract",
]
