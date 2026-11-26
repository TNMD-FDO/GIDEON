"""Build the closed-book prompt from numbered passages and the research question."""

from collections.abc import Sequence
from typing import Final

TRACER_SYSTEM: Final = "Use only the numbered passages below as sources for your answer, without relying on memory."


def messages(question: str, passages: Sequence[tuple[str, str]]) -> list[dict[str, str]]:
    """Place the ordered (caption, text) pairs before the question in one user message."""

    blocks = [
        f"[{number}] {caption}\n{text}" if caption else f"[{number}] {text}"
        for number, (caption, text) in enumerate(passages, start=1)
    ]
    blocks.append(f"Question: {question}")
    return [
        {"role": "system", "content": TRACER_SYSTEM},
        {"role": "user", "content": "\n\n".join(blocks)},
    ]
